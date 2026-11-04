import os
import json
import base64
import asyncio
import logging
import time
import re
import sys
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon import __version__ as telethon_version
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
from telegram_worker.permanent_delivery_errors import PermanentFailureLedger, Throttle
from telegram_worker.provider_profiles import is_promo_text
from telegram_worker.routes import ROUTES
from telegram_worker import owner_forwarding_policy as owner_policy
from telegram_worker.stats_reporter import WeeklyStats
from telegram_worker.state_store import open_message_map
from telegram_worker.engine.runtime import restyle_for_route
from telegram_worker.engine.mirror import EditReconciler, Mirror

from telegram_worker import liveness
from telegram_worker.log_setup import configure_logging

configure_logging("imperium-telegram-worker")
log = logging.getLogger("imperium-worker")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
DRY_RUN = os.environ.get("DRY_RUN", "0").strip() == "1"
FORWARD_EDITED_MESSAGES = (
    os.environ.get("FORWARD_EDITED_MESSAGES", "1").strip() == "1"
)  # mapped-message edits only
ENABLE_ROUTE_FALLBACK_ALL_MESSAGES = os.environ.get("ENABLE_ROUTE_FALLBACK_ALL_MESSAGES", "1").strip() == "1"
COPY_RETRY_ATTEMPTS = int(os.environ.get("COPY_RETRY_ATTEMPTS", "2"))
NEW_MIRROR_DEBUG_CHATS_RAW = os.environ.get(
    "NEW_MIRROR_DEBUG_CHATS",
    "-1003812195730,-1003371106919,-1003651353503,-1003087047858,-1002817163788,-1003770573057",
).strip()

NEW_MIRROR_DEBUG_CHATS = {int(x) for x in re.split(r"[,\s]+", NEW_MIRROR_DEBUG_CHATS_RAW) if x.strip()}


NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW = os.environ.get("NEW_MIRROR_BACKFILL_ONLY_CHATS", "").strip()
NEW_MIRROR_BACKFILL_ONLY_CHATS = (
    {int(x) for x in re.split(r"[,\s]+", NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW) if x.strip()}
    if NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW
    else set()
)

NEW_MIRROR_DEBUG_DEST_TOPICS_RAW = os.environ.get("NEW_MIRROR_DEBUG_DEST_TOPICS", "").strip()
NEW_MIRROR_DEBUG_DEST_TOPICS = (
    {int(x) for x in re.split(r"[,\s]+", NEW_MIRROR_DEBUG_DEST_TOPICS_RAW) if x.strip()}
    if NEW_MIRROR_DEBUG_DEST_TOPICS_RAW
    else set()
)

NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW = os.environ.get("NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS", "").strip()
NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS = (
    {int(x) for x in re.split(r"[,\s]+", NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW) if x.strip()}
    if NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW
    else set()
)

# Dedicated state for live-only private routes.

# A live route is NEVER allowed to process messages until it has
# successfully snapshotted the current Telegram newest message ID.


STRICT_ROUTE_TITLE_CHECK = os.environ.get("STRICT_ROUTE_TITLE_CHECK", "0").strip() == "1"
MIRROR_DELETED_MESSAGES = os.environ.get("MIRROR_DELETED_MESSAGES", "1").strip() == "1"

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
owner_policy.configure(DATA_DIR)
ROUTES[:] = [r for r in ROUTES if not owner_policy.is_stopped(r)]
SESSION_BASE = DATA_DIR / "session"
SESSION_FILE = DATA_DIR / "session.session"
MESSAGE_MAP_FILE = DATA_DIR / "message_map.json"
DEDUP_FILE = DATA_DIR / "dedupe_map.json"

CROSS_SOURCE_DEDUP_DEST_TOPICS_RAW = os.environ.get("CROSS_SOURCE_DEDUP_DEST_TOPICS", "1927").strip()
CROSS_SOURCE_DEDUP_DEST_TOPICS = {
    int(x) for x in re.split(r"[,\s]+", CROSS_SOURCE_DEDUP_DEST_TOPICS_RAW) if x.strip()
}

BLOCKED_DEST_CHAT = int(os.environ.get("BLOCKED_DEST_CHAT", "-1003918958200"))
BLOCKED_DEST_TOPICS_RAW = os.environ.get("BLOCKED_DEST_TOPICS", "1").strip()
BLOCKED_DEST_TOPICS = {int(x) for x in re.split(r"[,\s]+", BLOCKED_DEST_TOPICS_RAW) if x.strip()}


IGNORED_SOURCE_CHATS_RAW = os.environ.get("IGNORED_SOURCE_CHATS", "-1003726286301").strip()
IGNORED_SOURCE_CHATS = {int(x) for x in re.split(r"[,\\s]+", IGNORED_SOURCE_CHATS_RAW) if x.strip()}

BLOCKED_SENDER_IDS_RAW = os.environ.get("BLOCKED_SENDER_IDS", "7556281143").strip()
BLOCKED_SENDER_IDS = {int(x) for x in re.split(r"[,\s]+", BLOCKED_SENDER_IDS_RAW) if x.strip()}

BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW = os.environ.get("BLOCKED_SENDER_CLEANUP_DEST_TOPICS", "28840").strip()
BLOCKED_SENDER_CLEANUP_DEST_TOPICS = (
    {int(x) for x in re.split(r"[,\\s]+", BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW) if x.strip()}
    if BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW
    else set()
)

SOURCE_CHATS = sorted(
    {r["source_chat"] for r in ROUTES if int(r.get("source_chat")) not in IGNORED_SOURCE_CHATS}
)
stats = WeeklyStats(DATA_DIR)
route_title_status = {}


def _clean_b64(value: str) -> str:
    return "".join((value or "").split()).strip()


def combined_login_blob():
    # Prefer chunked fresh session variables over old direct SESSION_B64.
    count_raw = os.environ.get("SESSION_B64_CHUNKS", "").strip()
    if count_raw:
        try:
            count = int(count_raw)
        except ValueError:
            raise RuntimeError(f"Invalid SESSION_B64_CHUNKS value: {count_raw}")
        chunks = []
        for i in range(1, count + 1):
            chunk = _clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
            if not chunk:
                raise RuntimeError(f"SESSION_B64_CHUNKS={count} but SESSION_B64_{i} is missing")
            chunks.append(chunk)
        return "".join(chunks), f"{count} chunks fixed-count"

    chunks = []
    i = 1
    while True:
        chunk = _clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
        if not chunk:
            break
        chunks.append(chunk)
        i += 1

    if chunks:
        return "".join(chunks), f"{len(chunks)} chunks"

    direct = _clean_b64(os.environ.get("SESSION_B64", ""))
    if direct:
        return direct, "SESSION_B64"

    return "", "none"


def write_login_file():
    blob, source = combined_login_blob()
    if not blob:
        raise RuntimeError("No Telegram login data found in Railway variables.")
    raw = base64.b64decode(blob)
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_bytes(raw)
    log.info(f"Telegram login file written from {source}: {SESSION_FILE} bytes={len(raw)}")


def load_map():
    if not MESSAGE_MAP_FILE.exists():
        return {}
    try:
        return json.loads(MESSAGE_MAP_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        backup = MESSAGE_MAP_FILE.with_name(f"message_map.corrupt-{int(time.time())}.json")
        try:
            backup.write_bytes(MESSAGE_MAP_FILE.read_bytes())
        except Exception:
            pass
        log.error(f"[MESSAGE MAP UNREADABLE] {type(exc).__name__}: {exc} copy_kept={backup.name}")
        return {}


def save_map():
    # Every write is already durable in SQLite; this refreshes the JSON copy
    # kept for rollbacks (at most every 5 minutes, off the event loop).
    message_map.snapshot_to(MESSAGE_MAP_FILE)


def load_dedupe():
    if not DEDUP_FILE.exists():
        return {}
    try:
        return json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mapped_ids_from_value(value):
    """
    Message map compatibility:
    old format = int
    reinforced format = list[int]
    accidental dict/list values are also handled safely.
    """
    if not value:
        return []

    if isinstance(value, list):
        out = []
        for x in value:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out

    if isinstance(value, dict):
        out = []
        for x in value.values():
            try:
                out.append(int(x))
            except Exception:
                pass
        return out

    try:
        return [int(value)]
    except Exception:
        return []


def existing_destination_ids(message, route):
    key = map_key(route["source_chat"], message.id, route["dest_chat"], route["dest_topic"])
    return mapped_ids_from_value(message_map.get(key))


write_login_file()
message_map = open_message_map(DATA_DIR, MESSAGE_MAP_FILE, load_legacy=load_map)
content_dedupe_map = load_dedupe()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)
DELIVERY_ENGINE = None  # telegram_worker.engine.runtime.DeliveryEngine, set in main()
EMOJI_REGISTRY = None  # set by runtime_guard once Saved Messages are read
owner_policy.install_send_guard(client, log)


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []


# BEGIN USERNAME_MENTION_FILTER_V2

FILTER_USERNAME_MENTIONS = os.environ.get("FILTER_USERNAME_MENTIONS", "1").strip() == "1"


USERNAME_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])" r"@[A-Za-z][A-Za-z0-9_]{4,31}" r"(?![A-Za-z0-9_])")


def has_username_mention(message):
    """
    Block an actual Telegram-style @username.

    Detection has two layers:

    1. Telegram MessageEntityMention when Telegram provides it.

    2. Safe text fallback for standalone Telegram-style handles
       when Telegram sends the handle as plain text.

    The fallback deliberately requires the @ not to be preceded
    by an email/local-part character, so addresses such as
    test@example.com remain allowed.
    """
    if not FILTER_USERNAME_MENTIONS:
        return False
    entities = getattr(message, "entities", None) or []
    for entity in entities:
        if type(entity).__name__ == "MessageEntityMention":
            return True
    text = text_of(message)
    if not text:
        return False
    return bool(USERNAME_HANDLE_RE.search(text))


def log_username_filter_main(messages, context, route=None, source_chat=None, source_topic=None):
    messages = list(messages or [])
    ids = [int(getattr(message, "id", 0) or 0) for message in messages]
    route_name = route.get("name") if isinstance(route, dict) else None
    log.warning(
        f"[USERNAME MENTION FILTERED] account=MAIN context={context} source={source_chat}_{source_topic} ids={ids} route={route_name} SENT=False FILTER=EntityOrStandaloneUsername"
    )


# END USERNAME_MENTION_FILTER_V2

# BEGIN GLOBAL_CONTENT_FILTER_V1

FILTER_LINKS = os.environ.get("FILTER_LINKS", "1").strip() == "1"

URL_SCHEME_RE = re.compile(r"(?i)(?<![A-Za-z0-9._%+\-@])(?:https?://|tg://|www\.|t\.me/|telegram\.me/)\S+")
BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+\-@])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
    r"(?::\d{2,5})?"
    r"(?:/[^\s<>()]*)?"
)

_original_has_username_mention_v2 = has_username_mention


def has_link(message):
    if not FILTER_LINKS:
        return False

    for entity in getattr(message, "entities", None) or []:
        entity_name = type(entity).__name__

        if entity_name == "MessageEntityUrl":
            return True

        if entity_name == "MessageEntityTextUrl":
            target = str(getattr(entity, "url", "") or "")
            if target.lower().startswith("mailto:"):
                continue
            return True

    text = text_of(message)
    if not text:
        return False

    return bool(URL_SCHEME_RE.search(text) or BARE_DOMAIN_RE.search(text))


def has_username_mention(message):
    return _original_has_username_mention_v2(message) or has_link(message)


def unit_has_username_mention(messages):
    return any(has_username_mention(message) for message in (messages or []))


# END GLOBAL_CONTENT_FILTER_V1


def sender_ids_for_message(message):
    ids = set()

    for attr in ("sender_id", "from_id"):
        obj = getattr(message, attr, None)
        if isinstance(obj, int):
            ids.add(int(obj))
        else:
            for sub in ("user_id", "channel_id", "chat_id"):
                val = getattr(obj, sub, None)
                if val:
                    try:
                        ids.add(int(val))
                    except Exception:
                        pass

    fwd = getattr(message, "fwd_from", None)
    if fwd:
        for attr in ("from_id", "saved_from_peer"):
            obj = getattr(fwd, attr, None)
            if isinstance(obj, int):
                ids.add(int(obj))
            else:
                for sub in ("user_id", "channel_id", "chat_id"):
                    val = getattr(obj, sub, None)
                    if val:
                        try:
                            ids.add(int(val))
                        except Exception:
                            pass

    return ids


# BEGIN PRIVATE_LOOP_GUARD_V1


def _marked_peer_id(peer):
    """
    Convert Telethon PeerChannel / PeerChat IDs into the same
    marked format used by ROUTES, e.g. -1003252087470.
    """
    if peer is None:
        return None
    if isinstance(peer, int):
        return int(peer)
    channel_id = getattr(peer, "channel_id", None)
    if channel_id:
        try:
            return int("-100" + str(abs(int(channel_id))))
        except Exception:
            pass
    chat_id = getattr(peer, "chat_id", None)
    if chat_id:
        try:
            value = int(chat_id)
            return -abs(value)
        except Exception:
            pass
    user_id = getattr(peer, "user_id", None)
    if user_id:
        try:
            return int(user_id)
        except Exception:
            pass
    return None


def forward_origin_chat_ids(message):
    """
    IDs contained in Telegram's actual ForwardHeader.

    This detects:
        A forwards B
        B forwards A

    without relying only on matching message text.
    """
    out = set()
    fwd = getattr(message, "fwd_from", None)
    if not fwd:
        return out
    for attr in ("from_id", "saved_from_peer"):
        peer = getattr(fwd, attr, None)
        marked = _marked_peer_id(peer)
        if marked is not None:
            out.add(marked)
    return out


# END PRIVATE_LOOP_GUARD_V1


# BEGIN HARD_BLOCK_2521699926_FROM_VIP_V1


# END HARD_BLOCK_2521699926_FROM_VIP_V1


def should_hard_block_sender(message, context="", route=None):
    ids = sender_ids_for_message(message)
    matched = sorted(ids & BLOCKED_SENDER_IDS)
    if not matched:
        return False
    route_name = route.get("name") if isinstance(route, dict) else None
    route_dest = f"{route.get('dest_chat')}_{route.get('dest_topic')}" if isinstance(route, dict) else None
    log.warning(
        f"[blocked sender hard stop] blocked_ids={matched} all_sender_ids={sorted(ids)} msg={getattr(message, 'id', None)} context={context} route={route_name} dest={route_dest}"
    )
    return True


def known_source_topics_for_chat(chat_id):
    return {
        int(r["source_topic"])
        for r in ROUTES
        if r["source_chat"] == chat_id and r.get("source_topic") is not None
    }


def unique_routes_for_source_chat(chat_id):
    """
    Fallback for groups where many source topics all go to ONE destination topic.
    If Telegram gives bad/missing topic metadata for photo/video replies, we can still forward.
    """
    source_routes = [r for r in ROUTES if r["source_chat"] == chat_id]
    if not source_routes:
        return []

    unique_dests = {(r["dest_chat"], r["dest_topic"]) for r in source_routes}
    if len(unique_dests) != 1:
        return []

    first = dict(source_routes[0])
    first["source_topic"] = None
    first["name"] = first.get("name", "fallback") + " MEDIA FALLBACK"
    return [first]


def topic_of(message, chat_id=None):
    """
    Robust Telegram forum topic detection.

    Important fix:
    For photo/video replies, reply_to_msg_id may be the message being replied to,
    NOT the forum topic id. Only trust it when it matches a known route topic.
    """
    known_topics = known_source_topics_for_chat(chat_id) if chat_id is not None else set()

    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass

    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_top_id", "top_msg_id"):
            value = getattr(reply, attr, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass

    direct_reply_id = getattr(message, "reply_to_msg_id", None)
    if direct_reply_id:
        try:
            direct_reply_id = int(direct_reply_id)
            # Only treat reply_to_msg_id as topic if it is actually one of our route topics.
            if not known_topics or direct_reply_id in known_topics:
                return direct_reply_id
        except Exception:
            pass

    if reply:
        reply_msg_id = getattr(reply, "reply_to_msg_id", None)
        if reply_msg_id:
            try:
                reply_msg_id = int(reply_msg_id)
                if not known_topics or reply_msg_id in known_topics:
                    return reply_msg_id
            except Exception:
                pass

    return None


def same_source_and_destination(route, topic_id):
    return (
        route["source_chat"] == route["dest_chat"]
        and route.get("source_topic") == route.get("dest_topic")
        and topic_id == route.get("dest_topic")
    )


# BEGIN VIP5 COPY HARD GUARD V1
def vip5_retired_destination(route):
    try:
        return int(route.get("dest_chat") or 0) == -1003726286301 and int(route.get("dest_topic") or 0) == 5
    except (ValueError, TypeError, AttributeError):
        return False


# END VIP5 COPY HARD GUARD V1


def is_blocked_destination(route):
    if vip5_retired_destination(route):
        return True
    try:
        return (
            int(route.get("dest_chat")) == BLOCKED_DEST_CHAT
            and int(route.get("dest_topic")) in BLOCKED_DEST_TOPICS
        )
    except Exception:
        return False


def routes_for(chat_id, topic_id, message=None):
    if int(chat_id) == -1004367822325 and topic_id == 7250:
        log.warning("[RELAY7250 PERMANENT OUTBOUND BLOCK] handler=True")
        return []
    try:
        if int(chat_id) in IGNORED_SOURCE_CHATS:
            log.warning(
                f"[ignored source chat] source={chat_id}_{topic_id} msg={getattr(message, 'id', None)}"
            )
            return []
    except Exception:
        pass

    found = []
    for route in ROUTES:
        if route.get("owner_managed") or owner_policy.is_stopped(route):
            continue
        if route["source_chat"] != chat_id:
            continue

        if is_blocked_destination(route):
            log.warning(
                f"[blocked destination] route={route.get('name')} "
                f"source={route.get('source_chat')}_{route.get('source_topic')} "
                f"blocked_dest={route.get('dest_chat')}_{route.get('dest_topic')}"
            )
            continue

        if route["source_topic"] is not None and route["source_topic"] != topic_id:
            continue
        if same_source_and_destination(route, topic_id):
            log.warning(f"[self-route skipped] {route['name']} {chat_id}_{topic_id}")
            continue

        if (
            STRICT_ROUTE_TITLE_CHECK
            and route.get("verify_title")
            and not route_title_status.get(route_identity(route), False)
        ):
            log.warning(
                f"[route title strict skip] route={route['name']} "
                f"source={route['source_chat']}_{route.get('source_topic')} "
                f"dest={route['dest_chat']}_{route['dest_topic']}"
            )
            continue

        found.append(route)

    if found:
        return found

    # Route fallback:
    # Telegram sometimes gives missing/wrong topic metadata for forum messages.
    # If every route from that source chat goes to ONE destination, forward there
    # instead of silently dropping the message. This is safe for one-destination source groups.
    if message is not None and ENABLE_ROUTE_FALLBACK_ALL_MESSAGES:
        fallback = [r for r in unique_routes_for_source_chat(chat_id) if not is_blocked_destination(r)]
        if fallback:
            kind = "media" if is_real_media(message) else "text"
            log.warning(
                f"[route fallback:{kind}] source={chat_id}_{topic_id} "
                f"msg={getattr(message, 'id', None)} grouped={getattr(message, 'grouped_id', None)} "
                f"reply_ids={reply_source_ids(message)} -> "
                f"dest={fallback[0]['dest_chat']}_{fallback[0]['dest_topic']}"
            )
            return fallback

    return found


def is_real_media(message):
    if not getattr(message, "media", None):
        return False
    return not isinstance(message.media, MessageMediaWebPage)


def map_key(source_chat, source_msg_id, dest_chat, dest_topic):
    return f"{source_chat}:{source_msg_id}:{dest_chat}:{dest_topic}"


def reply_source_ids(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return []

    ids = []
    reply_msg_id = getattr(reply, "reply_to_msg_id", None)
    top_id = getattr(reply, "reply_to_top_id", None)

    for value in (reply_msg_id, top_id):
        if value and value not in ids:
            ids.append(value)

    return ids


def remember_message(src_msg, dst_msg, route, save=True):
    if not src_msg or not dst_msg:
        return

    key = map_key(route["source_chat"], src_msg.id, route["dest_chat"], route["dest_topic"])

    if isinstance(dst_msg, list):
        ids = []
        for item in dst_msg:
            mid = getattr(item, "id", None)
            if mid:
                ids.append(int(mid))

        if ids:
            message_map[key] = ids[0] if len(ids) == 1 else ids
            if save:
                save_map()
        return

    mid = getattr(dst_msg, "id", None)
    if mid:
        message_map[key] = int(mid)
        if save:
            save_map()


# BEGIN MAIN_MAPPED_IN_PLACE_EDITS_V1
# Edits and deletions are mirrored by engine/mirror.py (shared with ALT):
# mapped copies only, edited in place, cascaded to copies of copies.
MAPPED_EDIT_RECONCILE_FILE = DATA_DIR / "mapped_edit_reconcile_v2.json"
MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS = int(os.environ.get("MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS", "30"))
MAPPED_EDIT_RECONCILE_FETCH_LIMIT = int(os.environ.get("MAPPED_EDIT_RECONCILE_FETCH_LIMIT", "40"))
RELAY508_TO_VIP7 = (-1004367822325, 508, -1003726286301, 7)


def exact_downstream_routes(source_chat, source_topic):
    """Routes reading exactly this chat and topic (a whole-chat route only matches topic None)."""
    return [
        r
        for r in ROUTES
        if int(r["source_chat"]) == int(source_chat) and r.get("source_topic") == source_topic
    ]


def edit_blocked(chat_id, message):
    """Edits the live rules would not deliver are not passed on."""
    if has_username_mention(message):
        log_username_filter_main([message], "edited_event", source_chat=chat_id, source_topic=None)
        return True
    if should_hard_block_sender(message, "edit_handler"):
        return True
    topic_id = topic_of(message, chat_id)
    if is_promo_text(text_of(message), topic_id):
        log.info(f"[promo blocked incoming] msg={getattr(message, 'id', None)} topic={topic_id}")
        return True
    return False


async def render_for_route(route, message):
    """An edited post as the route publishes it (restyled for VIP 7)."""
    message = restyle_for_route(message, route, EMOJI_REGISTRY)
    return text_of(message), entities_of(message)


MIRROR = Mirror(
    "MAIN",
    client,
    message_map,
    render_for_route,
    blocked=edit_blocked,
    routes_from=exact_downstream_routes,
    save=save_map,
    cascade_depth=4,
)


async def relay508_mapped_edit_reconcile_loop():
    """Backup for missed edit events on Relay508 -> VIP7 (the restyled route)."""
    route = next(
        (
            r
            for r in ROUTES
            if (r["source_chat"], r.get("source_topic"), r["dest_chat"], r["dest_topic"]) == RELAY508_TO_VIP7
        ),
        None,
    )
    if route is None:
        log.warning("[MAPPED EDIT RECONCILER OFF] Relay508 To VIP7 is not a live route")
        return

    async def newest(limit):
        messages = await get_route_poll_messages({**route, "strict_source_topic": True}, limit) or []
        return [m for m in messages if topic_of(m, route["source_chat"]) == route["source_topic"]]

    await EditReconciler(
        MIRROR,
        route,
        newest,
        MAPPED_EDIT_RECONCILE_FILE,
        interval=MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS,
        limit=MAPPED_EDIT_RECONCILE_FETCH_LIMIT,
    ).run()


# END MAIN_MAPPED_IN_PLACE_EDITS_V1


async def get_route_poll_messages(route, limit):
    if owner_policy.is_stopped(route):
        return []
    chat_id = int(route["source_chat"])
    source_topic = route.get("source_topic")
    limit = max(1, int(limit))
    if source_topic:
        try:
            msgs = await client.get_messages(chat_id, limit=limit, reply_to=int(source_topic))
            if msgs is not None:
                try:
                    count = len(msgs)
                except Exception:
                    count = "unknown"
                log.debug(
                    "[new mirror topic poll fetch] route=%s source=%s_%s limit=%s count=%s",
                    route["name"],
                    chat_id,
                    source_topic,
                    limit,
                    count,
                )
                return msgs
        except Exception as exc:
            reason = owner_policy.permanent_access_error(exc)
            if reason:
                owner_policy.stop_route(route, reason, log)
                return []
            if route.get("strict_source_topic"):
                log.exception(
                    f"[STRICT SOURCE TOPIC FETCH FAILED] route={route.get('name')} source={chat_id}_{source_topic}. NO WHOLE-CHAT FALLBACK ALLOWED: {type(exc).__name__}: {exc}"
                )
                raise
            log.warning(
                f"[new mirror topic poll fetch failed] route={route.get('name')} source={chat_id}_{source_topic}: {type(exc).__name__}: {exc}. Falling back to chat latest."
            )
    try:
        return await client.get_messages(chat_id, limit=limit)
    except Exception as exc:
        reason = owner_policy.permanent_access_error(exc)
        if reason:
            owner_policy.stop_route(route, reason, log)
            return []
        raise


# BEGIN FAST_VIP_ROUTER_CONFIG_V1

FAST_VIP_CHAT = -1003726286301


def is_fast_vip_route(route):
    """
    Every live-only route whose source OR destination is
    the VIP belongs to the dedicated priority router.
    """
    if not isinstance(route, dict):
        return False
    if not route.get("live_only") or route.get("owner_managed"):
        return False
    try:
        source_chat = int(route.get("source_chat", 0))
    except Exception:
        source_chat = 0
    try:
        dest_chat = int(route.get("dest_chat", 0))
    except Exception:
        dest_chat = 0
    return source_chat == FAST_VIP_CHAT or dest_chat == FAST_VIP_CHAT


def fast_vip_routes():

    return [route for route in ROUTES if is_fast_vip_route(route)]


# END FAST_VIP_ROUTER_CONFIG_V1


# BEGIN FASTVIP PERMANENT DESTINATION ERRORS V1
# Live FASTVIP delivery stops re-sending a message whose destination refuses
# this account (see permanent_delivery_errors.py). Each NEW message still gets
# one attempt, so a route recovers by itself once permission is restored.
FASTVIP_PERMANENT_FAILURES = PermanentFailureLedger(DATA_DIR / "fastvip_permanent_failures_v1.json")
FASTVIP_IMPAIRED_ALERTS = Throttle(6 * 3600)
# END FASTVIP PERMANENT DESTINATION ERRORS V1


async def fast_vip_deliver_route(route, unit, skip_permanent_errors=False):
    """Deliver one live VIP post on one route through the delivery engine.

    True when handled (sent, already copied or rightly skipped), False to let
    the watchdog retry. skip_permanent_errors marks live delivery: a destination
    that refuses this account for good is then recorded once and not retried.
    """
    return await DELIVERY_ENGINE.deliver(route, unit, live=skip_permanent_errors)


# BEGIN ENGINE_INTAKE_V1
# Live VIP routes reach the delivery engine through engine/intake.py: Telegram
# pushes first, a checkpointed watchdog behind them, posts that arrived during
# a restart delivered in order, and posts older than FASTVIP_STALE_SECONDS
# reported to the owner instead of sent.
FASTVIP_WATCHDOG_SECONDS = max(2.0, float(os.environ.get("FASTVIP2_WATCHDOG_SECONDS", "30")))
FASTVIP_STALE_SECONDS = 600
INTAKE = None


def intake_pending(route, unit):
    """Would this route still send the post? (Stale posts are only reported then.)"""
    from telegram_worker.engine.model import Send
    from telegram_worker.engine.runtime import engine_route

    DELIVERY_ENGINE.planner.context["emoji_registry"] = EMOJI_REGISTRY
    plan = DELIVERY_ENGINE.planner.plan(engine_route(route), unit, route, check_ledger=True)
    return isinstance(plan, Send)


async def intake_deliver(route, unit, reason):
    return await fast_vip_deliver_route(route, unit, skip_permanent_errors=True)


async def start_engine_intake():
    from telegram_worker.engine.intake import (
        Checkpoints,
        Intake,
        first_start_checkpoint,
        group_by_source,
        last_delivered,
    )

    global INTAKE
    for _ in range(60):  # VIP7 restyling needs the emoji registry; it is ready within a second or two
        if EMOJI_REGISTRY is not None:
            break
        await asyncio.sleep(0.25)

    async def first_checkpoint(source, routes):
        return await first_start_checkpoint(client, source, [last_delivered(message_map, r) for r in routes])

    INTAKE = Intake(
        "MAIN",
        client,
        group_by_source(fast_vip_routes()),
        intake_deliver,
        Checkpoints(message_map.db),
        topic_of,
        first_checkpoint,
        pending=intake_pending,
        watchdog_seconds=FASTVIP_WATCHDOG_SECONDS,
        stale_after=FASTVIP_STALE_SECONDS,
    )
    return INTAKE.start()


# END ENGINE_INTAKE_V1


@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def on_message_edited(event):
    """A source post was edited: update its copies in place (engine/mirror.py)."""
    if not FORWARD_EDITED_MESSAGES:
        return
    try:
        message, chat_id = event.message, event.chat_id
        await MIRROR.on_edit(chat_id, message, routes_for(chat_id, topic_of(message, chat_id), message))
    except Exception as exc:
        log.exception(f"[handler crash:on_message_edited] chat={getattr(event, 'chat_id', None)}: {exc}")
        alert_crash("imperium-telegram-worker:on_message_edited", exc)


@client.on(events.MessageDeleted(chats=SOURCE_CHATS))
async def on_message_deleted(event):
    if not MIRROR_DELETED_MESSAGES:
        return
    try:
        deleted_ids = list(getattr(event, "deleted_ids", []) or []) or [
            i for i in [getattr(event, "deleted_id", None)] if i
        ]
        await MIRROR.on_delete(getattr(event, "chat_id", None), deleted_ids)
    except Exception as exc:
        log.exception(f"[handler crash:on_message_deleted] chat={getattr(event, 'chat_id', None)}: {exc}")
        alert_crash("imperium-telegram-worker:on_message_deleted", exc)


def channel_function(name):
    return getattr(functions.channels, name, None)


# STRICT EXACT MIRROR TITLE CHECKER V2


def route_identity(route):
    return f"{route.get('source_chat')}:{route.get('source_topic')}:{route.get('dest_chat')}:{route.get('dest_topic')}"


def title_from_entity(entity):
    for attr in ("title", "first_name", "username"):
        value = getattr(entity, attr, None)
        if value:
            return str(value).strip()
    return ""


async def get_forum_topic_title(chat_id, topic_id):
    try:
        req_cls = channel_function("GetForumTopicsByIDRequest")
        if req_cls:
            res = await client(req_cls(channel=int(chat_id), topics=[int(topic_id)]))
            topics = getattr(res, "topics", None) or []
            if topics:
                title = getattr(topics[0], "title", None)
                if title:
                    return str(title).strip()
    except Exception as exc:
        log.info(f"[topic title api fallback] chat={chat_id} topic={topic_id}: {type(exc).__name__}: {exc}")
    try:
        msg = await client.get_messages(int(chat_id), ids=int(topic_id))
        if msg:
            action = getattr(msg, "action", None)
            for attr in ("title", "new_title"):
                value = getattr(action, attr, None)
                if value:
                    return str(value).strip()
            raw = text_of(msg)
            if raw:
                return raw.splitlines()[0].strip()
    except Exception as exc:
        log.warning(f"[topic title fetch failed] chat={chat_id} topic={topic_id}: {exc}")
    return ""


WARM_ENTITY_CACHE_ON_START = os.environ.get("WARM_ENTITY_CACHE_ON_START", "1").strip() == "1"


async def warm_entity_cache_once():
    """Populate the Telethon entity cache from the account's dialog list.

    A session restored from SESSION_B64 chunks starts with an empty entity
    cache, so client.get_entity(-100...) raises "Could not find the input
    entity for PeerChannel(...)" for every channel the worker has not yet seen
    a live event from. Walking the dialogs once at startup resolves the access
    hashes for everything the account is actually a member of.

    Anything still unresolved after this is genuinely inaccessible (left,
    banned, or deleted) rather than merely uncached, and is logged by ID so
    dead routes can be identified with confidence.
    """
    if not WARM_ENTITY_CACHE_ON_START:
        log.warning("[ENTITY CACHE WARMUP] disabled")
        return
    wanted = set()
    for route in ROUTES:
        for key in ("source_chat", "dest_chat"):
            try:
                wanted.add(int(route[key]))
            except Exception:
                continue
    dialogs = 0
    try:
        async for _dialog in client.iter_dialogs():
            dialogs += 1
    except FloodWaitError as exc:
        log.warning(f"[ENTITY CACHE WARMUP FLOODWAIT] wait={exc.seconds}s; continuing without a full warmup")
        return
    except Exception as exc:
        log.warning(f"[ENTITY CACHE WARMUP FAILED SAFE] {type(exc).__name__}: {exc}")
        return
    resolved = 0
    unresolved = []
    for chat_id in sorted(wanted):
        try:
            await client.get_input_entity(chat_id)
            resolved += 1
        except Exception as exc:
            unresolved.append((chat_id, type(exc).__name__))
    log.warning(
        f"[ENTITY CACHE WARMUP DONE] dialogs={dialogs} route_peers={len(wanted)} resolved={resolved} unresolved={len(unresolved)}"
    )
    for chat_id, error_name in unresolved:
        log.warning(f"[ENTITY STILL UNRESOLVED] chat={chat_id} {error_name}")


async def main():
    await start_runtime_guard("imperium-telegram-worker", log)
    liveness.LoopWatchdog("ExposedFX MAIN", DATA_DIR, log).start()
    liveness.start_heartbeat("ExposedFX MAIN", client, log)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks."
        )

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    await warm_entity_cache_once()

    owner_module = __import__("sys").modules[__name__]
    from telegram_worker.engine.runtime import install_engine

    install_engine(owner_module)
    from telegram_worker.samgtrades_relay import run_with_retry as run_wholechat

    owner_module.WHOLECHAT_4281603170_TO_3743381585_TASK = asyncio.create_task(
        run_wholechat(owner_module, log)
    )
    from telegram_worker.owner_routes import run_owner_requested_routes

    owner_module.OWNER_REQUESTED_ROUTES_TASK = asyncio.create_task(run_owner_requested_routes(owner_module))
    from telegram_worker.owner_formatted_route import run_owner_formatted_route

    owner_module.OWNER_FORMATTED_ROUTE_TASK = asyncio.create_task(run_owner_formatted_route(owner_module))
    from telegram_worker.health_monitor import install_health_monitor

    owner_module.HEALTH_MONITOR = install_health_monitor("ExposedFX MAIN", client, DATA_DIR)
    from telegram_worker.destination_directory import log_destination_directory

    owner_module.DESTINATION_DIRECTORY_TASK = asyncio.create_task(log_destination_directory(owner_module))
    from telegram_worker.engine.audit import run_replay_audit

    owner_module.ENGINE_AUDIT_TASK = asyncio.create_task(
        run_replay_audit(owner_module, DELIVERY_ENGINE.planner)
    )
    log.warning(
        "[MAIN READY] "
        f"routes={len(ROUTES)} source_chats={SOURCE_CHATS} "
        f"message_map_entries={len(message_map)} dry_run={DRY_RUN} "
        f"edits={FORWARD_EDITED_MESSAGES} deletes={MIRROR_DELETED_MESSAGES} "
        f"copy_retry_attempts={COPY_RETRY_ATTEMPTS} blocked_senders={sorted(BLOCKED_SENDER_IDS)} "
        f"telethon={telethon_version} python={sys.version.split()[0]}"
    )
    asyncio.create_task(relay508_mapped_edit_reconcile_loop())
    await start_engine_intake()
    asyncio.create_task(stats.loop(client))
    log.info("Weekly stats reporter running for Sunday 00:00 Europe/Malta")
    await client.run_until_disconnected()
    liveness.exit_disconnected("ExposedFX MAIN", DATA_DIR, log)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log.exception(f"[worker fatal crash] {type(exc).__name__}: {exc}")
        alert_crash("imperium-telegram-worker:fatal", exc)
        raise

import os
import json
import base64
import asyncio
import logging
import time
import re
import hashlib
import sys
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon import __version__ as telethon_version
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
from telegram_worker.permanent_delivery_errors import (
    PermanentFailureLedger,
    Throttle,
)
from telegram_worker.provider_profiles import is_promo_text
from telegram_worker.routes import ROUTES
from telegram_worker import owner_forwarding_policy as owner_policy
from telegram_worker.stats_reporter import WeeklyStats
from telegram_worker.engine.model import entity_keys

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

NEW_MIRROR_DEBUG_CHATS = {
    int(x)
    for x in re.split(r"[,\s]+", NEW_MIRROR_DEBUG_CHATS_RAW)
    if x.strip()
}


NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW = os.environ.get("NEW_MIRROR_BACKFILL_ONLY_CHATS", "").strip()
NEW_MIRROR_BACKFILL_ONLY_CHATS = {
    int(x)
    for x in re.split(r"[,\s]+", NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW)
    if x.strip()
} if NEW_MIRROR_BACKFILL_ONLY_CHATS_RAW else set()

NEW_MIRROR_DEBUG_DEST_TOPICS_RAW = os.environ.get("NEW_MIRROR_DEBUG_DEST_TOPICS", "").strip()
NEW_MIRROR_DEBUG_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\s]+", NEW_MIRROR_DEBUG_DEST_TOPICS_RAW)
    if x.strip()
} if NEW_MIRROR_DEBUG_DEST_TOPICS_RAW else set()

NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW = os.environ.get("NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS", "").strip()
NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\s]+", NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW)
    if x.strip()
} if NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS_RAW else set()

# Dedicated state for live-only private routes.

# A live route is NEVER allowed to process messages until it has
# successfully snapshotted the current Telegram newest message ID.


# BEGIN VIP_TOPIC_MOVE_STATE_V1

VIP_TOPIC_MOVE_ENABLED = (
    os.environ.get(
        "ALLOW_VIP_TOPIC_MOVE_LAST50_V1",
        "0",
    ).strip()
    == "1"
)

VIP_TOPIC_MOVE_NEW_CHAT = -1003726286301

VIP_TOPIC_MOVE_MAP = {
    2786: 5,
    5655: 11,
    5551: 7,
    5947: 13,
    6813: 9,
}

# While a topic is migrating, outbound routes from that new VIP
# topic are temporarily paused.
#
# Inbound provider -> NEW VIP routes continue normally.
VIP_TOPIC_MOVE_ACTIVE_TOPICS = (
    set(VIP_TOPIC_MOVE_MAP.values())
    if VIP_TOPIC_MOVE_ENABLED
    else set()
)

# Exact NEW VIP message IDs created by history migration.
#
# When outbound routing resumes, these IDs are skipped while
# genuine new/live messages remain eligible.
VIP_TOPIC_MOVE_SUPPRESS_IDS = set()

# END VIP_TOPIC_MOVE_STATE_V1


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
    int(x)
    for x in re.split(r"[,\s]+", CROSS_SOURCE_DEDUP_DEST_TOPICS_RAW)
    if x.strip()
}

BLOCKED_DEST_CHAT = int(os.environ.get("BLOCKED_DEST_CHAT", "-1003918958200"))
BLOCKED_DEST_TOPICS_RAW = os.environ.get("BLOCKED_DEST_TOPICS", "1").strip()
BLOCKED_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\s]+", BLOCKED_DEST_TOPICS_RAW)
    if x.strip()
}


IGNORED_SOURCE_CHATS_RAW = os.environ.get("IGNORED_SOURCE_CHATS", "-1003726286301").strip()
IGNORED_SOURCE_CHATS = {
    int(x)
    for x in re.split(r"[,\\s]+", IGNORED_SOURCE_CHATS_RAW)
    if x.strip()
}

BLOCKED_SENDER_IDS_RAW = os.environ.get("BLOCKED_SENDER_IDS", "7556281143").strip()
BLOCKED_SENDER_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", BLOCKED_SENDER_IDS_RAW)
    if x.strip()
}

BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW = os.environ.get("BLOCKED_SENDER_CLEANUP_DEST_TOPICS", "28840").strip()
BLOCKED_SENDER_CLEANUP_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\\s]+", BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW)
    if x.strip()
} if BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW else set()

SOURCE_CHATS = sorted({r["source_chat"] for r in ROUTES if int(r.get("source_chat")) not in IGNORED_SOURCE_CHATS})
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
    tmp = MESSAGE_MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(message_map), encoding="utf-8")
    tmp.replace(MESSAGE_MAP_FILE)


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
message_map = load_map()
content_dedupe_map = load_dedupe()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)
DELIVERY_ENGINE = None  # telegram_worker.engine.runtime.DeliveryEngine, set in main()
owner_policy.install_send_guard(client, log)


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []


# BEGIN USERNAME_MENTION_FILTER_V2

FILTER_USERNAME_MENTIONS = (
    os.environ.get(
        "FILTER_USERNAME_MENTIONS",
        "1",
    ).strip()
    == "1"
)


USERNAME_HANDLE_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"@[A-Za-z][A-Za-z0-9_]{4,31}"
    r"(?![A-Za-z0-9_])"
)


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
    entities = getattr(message, 'entities', None) or []
    for entity in entities:
        if type(entity).__name__ == 'MessageEntityMention':
            return True
    text = text_of(message)
    if not text:
        return False
    return bool(USERNAME_HANDLE_RE.search(text))


def log_username_filter_main(messages, context, route=None, source_chat=None, source_topic=None):
    messages = list(messages or [])
    ids = [int(getattr(message, 'id', 0) or 0) for message in messages]
    route_name = route.get('name') if isinstance(route, dict) else None
    log.warning(f'[USERNAME MENTION FILTERED] account=MAIN context={context} source={source_chat}_{source_topic} ids={ids} route={route_name} SENT=False FILTER=EntityOrStandaloneUsername')


# END USERNAME_MENTION_FILTER_V2

# BEGIN GLOBAL_CONTENT_FILTER_V1

FILTER_LINKS = (
    os.environ.get("FILTER_LINKS", "1").strip() == "1"
)

URL_SCHEME_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+\-@])(?:https?://|tg://|www\.|t\.me/|telegram\.me/)\S+"
)
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

    for entity in (getattr(message, "entities", None) or []):
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

    return bool(
        URL_SCHEME_RE.search(text)
        or BARE_DOMAIN_RE.search(text)
    )

def has_username_mention(message):
    return (
        _original_has_username_mention_v2(message)
        or has_link(message)
    )

def unit_has_username_mention(messages):
    return any(
        has_username_mention(message)
        for message in (messages or [])
    )

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
    channel_id = getattr(peer, 'channel_id', None)
    if channel_id:
        try:
            return int('-100' + str(abs(int(channel_id))))
        except Exception:
            pass
    chat_id = getattr(peer, 'chat_id', None)
    if chat_id:
        try:
            value = int(chat_id)
            return -abs(value)
        except Exception:
            pass
    user_id = getattr(peer, 'user_id', None)
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
    fwd = getattr(message, 'fwd_from', None)
    if not fwd:
        return out
    for attr in ('from_id', 'saved_from_peer'):
        peer = getattr(fwd, attr, None)
        marked = _marked_peer_id(peer)
        if marked is not None:
            out.add(marked)
    return out



# END PRIVATE_LOOP_GUARD_V1


# BEGIN HARD_BLOCK_2521699926_FROM_VIP_V1




# END HARD_BLOCK_2521699926_FROM_VIP_V1




def should_hard_block_sender(message, context='', route=None):
    ids = sender_ids_for_message(message)
    matched = sorted(ids & BLOCKED_SENDER_IDS)
    if not matched:
        return False
    route_name = route.get('name') if isinstance(route, dict) else None
    route_dest = f"{route.get('dest_chat')}_{route.get('dest_topic')}" if isinstance(route, dict) else None
    log.warning(f"[blocked sender hard stop] blocked_ids={matched} all_sender_ids={sorted(ids)} msg={getattr(message, 'id', None)} context={context} route={route_name} dest={route_dest}")
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
        return (int(route.get("dest_chat") or 0) == -1003726286301
                and int(route.get("dest_topic") or 0) == 5)
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
            log.warning(f"[ignored source chat] source={chat_id}_{topic_id} msg={getattr(message, 'id', None)}")
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

        if STRICT_ROUTE_TITLE_CHECK and route.get("verify_title") and not route_title_status.get(route_identity(route), False):
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


async def delete_mapped_destination_for_source(source_chat, source_msg_id):
    """
    Delete mirrored destination copies when the original source message is deleted.
    Works with old map values: int
    Works with new map values: list[int]
    """
    if not MIRROR_DELETED_MESSAGES:
        return False
    source_chat = str(source_chat)
    source_msg_id = str(source_msg_id)
    targets = {}
    keys_to_remove = []
    for key, value in list(message_map.items()):
        try:
            parts = str(key).split(':')
            if len(parts) != 4:
                continue
            mapped_source_chat, mapped_source_msg_id, dest_chat, dest_topic = parts
            if mapped_source_chat != source_chat or mapped_source_msg_id != source_msg_id:
                continue
            ids = mapped_ids_from_value(value)
            if not ids:
                continue
            dest_chat_int = int(dest_chat)
            targets.setdefault(dest_chat_int, [])
            for mid in ids:
                if mid not in targets[dest_chat_int]:
                    targets[dest_chat_int].append(mid)
            keys_to_remove.append(key)
        except Exception as exc:
            log.warning(f'[delete mirror scan failed] key={key}: {exc}')
    if not targets:
        log.info(f'[delete mirror] no mapped destination for source={source_chat}:{source_msg_id}')
        return False
    deleted_any = False
    for dest_chat, ids in targets.items():
        try:
            await client.delete_messages(dest_chat, ids)
            log.info(f'[delete mirror] deleted dest={dest_chat} msgs={ids} for source={source_chat}:{source_msg_id}')
            deleted_any = True
        except Exception as exc:
            log.warning(f'[delete mirror failed] dest={dest_chat} ids={ids} source={source_chat}:{source_msg_id}: {exc}')
    if deleted_any:
        for key in keys_to_remove:
            message_map.pop(key, None)
        save_map()
    return deleted_any


























# BEGIN MAIN_MAPPED_IN_PLACE_EDITS_V1

MAPPED_EDIT_RECONCILE_FILE = DATA_DIR / "mapped_edit_reconcile_v2.json"
MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS = int(
    os.environ.get("MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS", "30")
)
MAPPED_EDIT_RECONCILE_FETCH_LIMIT = int(
    os.environ.get("MAPPED_EDIT_RECONCILE_FETCH_LIMIT", "40")
)


def load_mapped_edit_reconcile_state():
    if not MAPPED_EDIT_RECONCILE_FILE.exists():
        return {}
    try:
        value = json.loads(
            MAPPED_EDIT_RECONCILE_FILE.read_text(encoding="utf-8")
        )
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_mapped_edit_reconcile_state(state):
    temp = MAPPED_EDIT_RECONCILE_FILE.with_suffix(".tmp")
    temp.write_text(
        json.dumps(state, separators=(",", ":")),
        encoding="utf-8",
    )
    temp.replace(MAPPED_EDIT_RECONCILE_FILE)


mapped_edit_reconcile_state = load_mapped_edit_reconcile_state()


def mapped_edit_fingerprint(message):
    edit_date = getattr(message, "edit_date", None)
    if edit_date is None:
        return None

    try:
        edit_stamp = edit_date.isoformat()
    except Exception:
        edit_stamp = str(edit_date)

    payload = "|".join([
        str(getattr(message, "id", 0) or 0),
        edit_stamp,
        text_of(message),
        repr(entity_keys(entities_of(message))),
    ])

    return hashlib.sha256(
        payload.encode("utf-8", errors="ignore")
    ).hexdigest()


def exact_downstream_routes(source_chat, source_topic):
    result = []

    for candidate in ROUTES:
        try:
            if int(candidate.get("source_chat", 0)) != int(source_chat):
                continue

            candidate_topic = candidate.get("source_topic")

            if source_topic is None:
                if candidate_topic is not None:
                    continue
            else:
                if candidate_topic is None or int(candidate_topic) != int(source_topic):
                    continue

            result.append(candidate)

        except Exception:
            continue

    return result


async def edit_existing_destination_in_place(
    message,
    route,
    cascade_depth=0,
    visited=None,
):
    # MAPPED-ONLY edit propagation:
    # never create from edit, never delete/resend, cascade mapped-only.
    if visited is None:
        visited = set()

    source_message_id = int(getattr(message, "id", 0) or 0)

    visit_key = (
        int(route["source_chat"]),
        source_message_id,
        int(route["dest_chat"]),
        int(route["dest_topic"]),
    )

    if visit_key in visited:
        return True

    visited.add(visit_key)

    destination_ids = existing_destination_ids(message, route)

    if not destination_ids:
        log.info(
            "[mapped edit skipped:no destination] "
            f"route={route['name']} source_msg={source_message_id}"
        )
        return False

    if len(destination_ids) != 1:
        log.warning(
            "[MAPPED EDIT MULTI-ID SKIPPED SAFE] "
            f"route={route['name']} source_msg={source_message_id} "
            f"dest_ids={destination_ids}"
        )
        return False

    destination_id = int(destination_ids[0])
    new_text = text_of(message)
    new_entities = entities_of(message)

    try:
        await client.edit_message(
            route["dest_chat"],
            destination_id,
            new_text or "",
            formatting_entities=(new_entities if new_text else None),
            parse_mode=None,
            link_preview=True,
        )

        log.warning(
            "[EDIT PROPAGATED IN PLACE] "
            f"depth={cascade_depth} route={route['name']} "
            f"source_msg={source_message_id} dest_msg={destination_id} "
            f"dest={route['dest_chat']}_{route['dest_topic']}"
        )

    except FloodWaitError:
        # Never swallow a flood wait here. Callers (the Relay508 reconcile loop
        # and the edited-message handler) have their own backoff, and they can
        # only reach it if this propagates. Converting it to "return False"
        # made the reconciler retry the same batch every few seconds, which
        # kept the account permanently rate limited.
        raise

    except Exception as exc:
        if "not modified" in str(exc).lower():
            log.info(
                "[EDIT ALREADY CURRENT] "
                f"route={route['name']} source_msg={source_message_id} "
                f"dest_msg={destination_id}"
            )
        else:
            log.warning(
                "[EDIT PROPAGATION FAILED SAFE] "
                f"depth={cascade_depth} route={route['name']} "
                f"source_msg={source_message_id} dest_msg={destination_id} "
                f"{type(exc).__name__}: {exc}"
            )
            return False

    if cascade_depth >= 4:
        return True

    downstream_routes = exact_downstream_routes(
        route["dest_chat"],
        route["dest_topic"],
    )

    if not downstream_routes:
        return True

    try:
        downstream_source = await client.get_messages(
            route["dest_chat"],
            ids=destination_id,
        )
    except Exception as exc:
        log.warning(
            "[EDIT CASCADE SOURCE FETCH FAILED] "
            f"chat={route['dest_chat']} msg={destination_id} "
            f"{type(exc).__name__}: {exc}"
        )
        return True

    if not downstream_source:
        return True

    for downstream_route in downstream_routes:
        if not existing_destination_ids(
            downstream_source,
            downstream_route,
        ):
            log.info(
                "[EDIT CASCADE UNMAPPED SKIPPED] "
                f"route={downstream_route['name']} source_msg={destination_id}"
            )
            continue

        await edit_existing_destination_in_place(
            downstream_source,
            downstream_route,
            cascade_depth=cascade_depth + 1,
            visited=visited,
        )

    return True


def relay508_to_vip7_edit_route():
    matches = [route for route in ROUTES if int(route.get('source_chat', 0)) == -1004367822325 and int(route.get('source_topic', 0) or 0) == 508 and (int(route.get('dest_chat', 0)) == -1003726286301) and (int(route.get('dest_topic', 0)) == 7) and bool(route.get('live_only')) and bool(route.get('strict_source_topic'))]
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one strict live Relay508 To VIP7 route; found={len(matches)}')
    return matches[0]


async def reconcile_relay508_mapped_edits_once(route):
    # Backup for missed/delayed MessageEdited updates.
    # Only edited source messages with exact mappings are considered.
    probe = dict(route)
    probe["strict_source_topic"] = True

    messages = list(
        await get_route_poll_messages(
            probe,
            MAPPED_EDIT_RECONCILE_FETCH_LIMIT,
        )
        or []
    )

    reconciled = 0

    for message in messages:
        try:
            if (
                topic_of(message, int(route["source_chat"]))
                != int(route["source_topic"])
            ):
                continue

            fingerprint = mapped_edit_fingerprint(message)

            if fingerprint is None:
                continue

            state_key = str(int(message.id))

            if mapped_edit_reconcile_state.get(state_key) == fingerprint:
                continue

            if has_username_mention(message):
                mapped_edit_reconcile_state[state_key] = fingerprint
                save_mapped_edit_reconcile_state(mapped_edit_reconcile_state)

                log.warning(
                    "[EDIT RECONCILE FILTERED SAFE] "
                    f"source_msg={message.id} destination_unchanged=True"
                )
                continue

            destination_ids = existing_destination_ids(message, route)

            if len(destination_ids) != 1:
                continue

            destination_id = int(destination_ids[0])

            destination = await client.get_messages(
                route["dest_chat"],
                ids=destination_id,
            )

            if not destination:
                # Deleted at the destination: remember this source version so
                # it is not re-fetched every cycle. A later source edit
                # changes the fingerprint and is checked once more.
                log.warning(
                    "[EDIT RECONCILE DEST MISSING] "
                    f"source_msg={message.id} dest_msg={destination_id} RECHECK=next_source_edit"
                )
                mapped_edit_reconcile_state[state_key] = fingerprint
                save_mapped_edit_reconcile_state(mapped_edit_reconcile_state)
                continue

            source_text = text_of(message)
            destination_text = text_of(destination)

            source_entities = entity_keys(entities_of(message))
            destination_entities = entity_keys(entities_of(destination))

            if (
                source_text != destination_text
                or source_entities != destination_entities
            ):
                success = await edit_existing_destination_in_place(
                    message,
                    route,
                )

                if not success:
                    continue

                reconciled += 1

                log.warning(
                    "[RELAY508 EDIT RECONCILED] "
                    f"source_msg={message.id} dest_msg={destination_id}"
                )

            mapped_edit_reconcile_state[state_key] = fingerprint
            save_mapped_edit_reconcile_state(mapped_edit_reconcile_state)

        except FloodWaitError:
            raise

        except Exception as exc:
            log.warning(
                "[EDIT RECONCILE ITEM FAILED SAFE] "
                f"source_msg={getattr(message, 'id', None)} "
                f"{type(exc).__name__}: {exc}"
            )

    return reconciled


async def relay508_mapped_edit_reconcile_loop():
    try:
        route = relay508_to_vip7_edit_route()
    except Exception as exc:
        log.exception(f'[MAPPED EDIT RECONCILER CONFIG FAILED] {type(exc).__name__}: {exc}')
        return
    log.warning(f'[MAPPED EDIT RECONCILER READY] route=Relay508 To VIP7 mapped_only=True cascade=True interval={MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS}s fetch_limit={MAPPED_EDIT_RECONCILE_FETCH_LIMIT}')
    while True:
        try:
            count = await reconcile_relay508_mapped_edits_once(route)
            if count:
                log.warning(f'[MAPPED EDIT RECONCILE PASS] updated={count}')
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds) + 1)
            log.warning(f'[MAPPED EDIT RECONCILER FLOODWAIT] wait={wait_for}s')
            await asyncio.sleep(wait_for)
        except Exception as exc:
            log.exception(f'[MAPPED EDIT RECONCILER FAILED SAFE] {type(exc).__name__}: {exc}')
        await asyncio.sleep(MAPPED_EDIT_RECONCILE_INTERVAL_SECONDS)

# END MAIN_MAPPED_IN_PLACE_EDITS_V1














async def get_route_poll_messages(route, limit):
    if owner_policy.is_stopped(route):
        return []
    chat_id = int(route['source_chat'])
    source_topic = route.get('source_topic')
    limit = max(1, int(limit))
    if source_topic:
        try:
            msgs = await client.get_messages(chat_id, limit=limit, reply_to=int(source_topic))
            if msgs is not None:
                try:
                    count = len(msgs)
                except Exception:
                    count = 'unknown'
                log.debug('[new mirror topic poll fetch] route=%s source=%s_%s limit=%s count=%s', route['name'], chat_id, source_topic, limit, count)
                return msgs
        except Exception as exc:
            reason = owner_policy.permanent_access_error(exc)
            if reason:
                owner_policy.stop_route(route, reason, log)
                return []
            if route.get('strict_source_topic'):
                log.exception(f"[STRICT SOURCE TOPIC FETCH FAILED] route={route.get('name')} source={chat_id}_{source_topic}. NO WHOLE-CHAT FALLBACK ALLOWED: {type(exc).__name__}: {exc}")
                raise
            log.warning(f"[new mirror topic poll fetch failed] route={route.get('name')} source={chat_id}_{source_topic}: {type(exc).__name__}: {exc}. Falling back to chat latest.")
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


# Maximum simultaneous Telegram history fetches used by
# the VIP priority router.
FAST_VIP_FETCH_CONCURRENCY = max(
    1,
    int(
        os.environ.get(
            "FAST_VIP_FETCH_CONCURRENCY",
            "6",
        )
    ),
)

FAST_VIP_FETCH_SEMAPHORE = asyncio.Semaphore(
    FAST_VIP_FETCH_CONCURRENCY
)


def is_fast_vip_route(route):
    """
    Every live-only route whose source OR destination is
    the VIP belongs to the dedicated priority router.
    """
    if not isinstance(route, dict):
        return False
    if not route.get('live_only') or route.get('owner_managed'):
        return False
    try:
        source_chat = int(route.get('source_chat', 0))
    except Exception:
        source_chat = 0
    try:
        dest_chat = int(route.get('dest_chat', 0))
    except Exception:
        dest_chat = 0
    return source_chat == FAST_VIP_CHAT or dest_chat == FAST_VIP_CHAT


def fast_vip_routes():

    return [
        route
        for route in ROUTES
        if is_fast_vip_route(route)
    ]


def fast_vip_source_key(route):

    return (
        int(route["source_chat"]),
        (
            int(route["source_topic"])
            if route.get("source_topic") is not None
            else None
        ),
    )


def fast_vip_route_groups():
    """
    Group destinations by SOURCE.

    Important for outbound VIP fan-out:

        VIP topic 11
              |
              +--> Jamie
              +--> Market Slayers

    Topic 11 is fetched ONCE, then copied concurrently
    to both destinations.
    """
    groups = {}
    for route in fast_vip_routes():
        key = fast_vip_source_key(route)
        groups.setdefault(key, []).append(route)
    return groups


# ============================================================
# SPECIAL SAFE DEDUPE FOR THE TWO PROVIDERS FEEDING VIP 11
#
# Old behaviour:
#     A forwards B -> loop guard can discard it immediately.
#
# New behaviour:
#     first exact ORIGINAL Telegram post seen wins.
#
# Where Telegram exposes ForwardHeader.channel_post:
#
#     direct A message      = (A, original_msg_id)
#     B forwarding A       = (A, original_msg_id)
#
# They therefore become one canonical event regardless of
# which provider copy arrives first.
# ============================================================

FAST_VIP_PAIR_SOURCES = {
    -1002438454194,
    -1003252087470,
}

FAST_VIP_PAIR_DEST_TOPIC = 11

FAST_VIP_PAIR_SEEN = {}
FAST_VIP_PAIR_LOCK = asyncio.Lock()


def is_fast_vip_pair_route(route):
    try:
        return int(route.get('source_chat', 0)) in FAST_VIP_PAIR_SOURCES and route.get('source_topic') is None and (int(route.get('dest_chat', 0)) == FAST_VIP_CHAT) and (int(route.get('dest_topic', 0)) == FAST_VIP_PAIR_DEST_TOPIC)
    except Exception:
        return False


def fast_vip_pair_canonical_key(
    message,
    route,
):

    if not is_fast_vip_pair_route(
        route
    ):
        return None

    source_chat = int(
        route["source_chat"]
    )

    source_id = int(
        getattr(
            message,
            "id",
            0,
        )
        or 0
    )

    fwd = getattr(
        message,
        "fwd_from",
        None,
    )

    if fwd:

        try:

            origins = (
                forward_origin_chat_ids(
                    message
                )
                & FAST_VIP_PAIR_SOURCES
            )

        except Exception:

            origins = set()


        original_post = getattr(
            fwd,
            "channel_post",
            None,
        )


        if (
            len(origins) == 1
            and original_post
        ):

            original_chat = next(
                iter(origins)
            )

            return (
                "telegram-original",
                int(original_chat),
                int(original_post),
            )


    # Direct source message.
    return (
        "telegram-original",
        source_chat,
        source_id,
    )


async def fast_vip_pair_reserve(
    key,
):

    if key is None:
        return True


    async with FAST_VIP_PAIR_LOCK:

        now = time.time()


        # Keep one hour of exact Telegram-original IDs.
        expired = [
            candidate
            for candidate, timestamp
            in FAST_VIP_PAIR_SEEN.items()
            if (
                now
                - float(timestamp)
            ) > 3600
        ]


        for candidate in expired:

            FAST_VIP_PAIR_SEEN.pop(
                candidate,
                None,
            )


        if key in FAST_VIP_PAIR_SEEN:

            return False


        FAST_VIP_PAIR_SEEN[key] = now

        return True


async def fast_vip_pair_release(
    key,
):

    if key is None:
        return


    async with FAST_VIP_PAIR_LOCK:

        FAST_VIP_PAIR_SEEN.pop(
            key,
            None,
        )




# END FAST_VIP_ROUTER_CONFIG_V1


# BEGIN FAST_VIP_ROUTER_V1

async def fast_vip_build_units(newer):
    units = []
    used_ids = set()
    for message in newer:
        mid = int(getattr(message, 'id', 0) or 0)
        if mid in used_ids:
            continue
        grouped_id = getattr(message, 'grouped_id', None)
        if grouped_id:
            unit = [candidate for candidate in newer if getattr(candidate, 'grouped_id', None) == grouped_id]
            unit.sort(key=lambda candidate: int(getattr(candidate, 'id', 0) or 0))
        else:
            unit = [message]
        for item in unit:
            used_ids.add(int(getattr(item, 'id', 0) or 0))
        units.append(unit)
    return units


# BEGIN FASTVIP PERMANENT DESTINATION ERRORS V1
# Live FASTVIP delivery stops re-sending a message whose destination refuses
# this account (see permanent_delivery_errors.py). Each NEW message still gets
# one attempt, so a route recovers by itself once permission is restored.
FASTVIP_PERMANENT_FAILURES = PermanentFailureLedger(
    DATA_DIR / "fastvip_permanent_failures_v1.json"
)
FASTVIP_IMPAIRED_ALERTS = Throttle(6 * 3600)
# END FASTVIP PERMANENT DESTINATION ERRORS V1


async def fast_vip_deliver_route(route, unit, skip_permanent_errors=False):
    """Deliver one FASTVIP2 post on one route through the delivery engine.

    True when handled (sent, already copied or rightly skipped), False to let
    the watchdog retry. skip_permanent_errors marks live delivery: a destination
    that refuses this account for good is then recorded once and not retried.
    """
    return await DELIVERY_ENGINE.deliver(route, unit, live=skip_permanent_errors)


# END FAST_VIP_ROUTER_V1


# BEGIN FAST_VIP_EVENT_ROUTER_V2

# ============================================================
# FASTVIP EVENT ROUTER V2
#
# PRIMARY:
#   Telegram updates pushed directly to this process.
#
# FALLBACK:
#   Separate polling watchdog.
#
# The watchdog NEVER owns the event queue and NEVER blocks
# NewMessage / Album delivery.
# ============================================================

FASTVIP2_WATCHDOG_SECONDS = max(
    2.0,
    float(
        os.environ.get(
            "FASTVIP2_WATCHDOG_SECONDS",
            "5",
        )
    ),
)

FASTVIP2_WATCHDOG_LIMIT = max(
    50,
    int(
        os.environ.get(
            "FASTVIP2_WATCHDOG_LIMIT",
            "300",
        )
    ),
)

FASTVIP2_WATCHDOG_CONCURRENCY = max(
    1,
    int(
        os.environ.get(
            "FASTVIP2_WATCHDOG_CONCURRENCY",
            "3",
        )
    ),
)


FASTVIP2_GROUPS = fast_vip_route_groups()

FASTVIP2_SOURCE_CHATS = sorted({
    int(source_chat)
    for source_chat, source_topic
    in FASTVIP2_GROUPS.keys()
})


FASTVIP2_QUEUES = {}

# Units already sitting in an event queue.
FASTVIP2_QUEUED = set()

# Units successfully handled during this process lifetime.
FASTVIP2_HANDLED = {}

# Startup baseline for WATCHDOG ONLY.
# Event updates do not depend on this baseline.
FASTVIP2_WATCHDOG_BASELINES = {}

FASTVIP2_WATCHDOG_SEMAPHORE = asyncio.Semaphore(
    FASTVIP2_WATCHDOG_CONCURRENCY
)


def fastvip2_queue_for(source_key):
    queue = FASTVIP2_QUEUES.get(source_key)
    if queue is None:
        queue = asyncio.Queue()
        FASTVIP2_QUEUES[source_key] = queue
    return queue


def fastvip2_unit_ids(unit):
    return tuple((int(getattr(item, 'id', 0) or 0) for item in unit))


def fastvip2_token(
    source_key,
    unit,
):

    return (
        source_key,
        fastvip2_unit_ids(
            unit
        ),
    )


def fastvip2_prune_handled():
    now = time.time()
    expired = [token for token, timestamp in FASTVIP2_HANDLED.items() if now - float(timestamp) > 7200]
    for token in expired:
        FASTVIP2_HANDLED.pop(token, None)


def fastvip2_routes_fully_mapped(routes, unit):
    for route in routes:
        for message in unit:
            if not existing_destination_ids(message, route):
                return False
    return True


def fastvip2_matching_groups(
    chat_id,
    message,
):

    try:

        chat_id = int(
            chat_id
        )

    except Exception:

        return []


    try:

        topic_id = topic_of(
            message,
            chat_id,
        )

    except Exception:

        topic_id = None


    matches = []


    for source_key, routes in FASTVIP2_GROUPS.items():

        source_chat, source_topic = source_key


        if int(source_chat) != chat_id:
            continue


        # Whole-chat source route.
        if source_topic is None:

            matches.append(
                (
                    source_key,
                    routes,
                )
            )

            continue


        try:

            same_topic = (
                int(source_topic)
                == int(topic_id)
            )

        except Exception:

            same_topic = False


        if same_topic:

            matches.append(
                (
                    source_key,
                    routes,
                )
            )


    return matches


async def fastvip2_enqueue(
    source_key,
    routes,
    unit,
    reason,
):

    unit = list(
        unit or []
    )


    if not unit:
        return False


    unit.sort(
        key=lambda message: int(
            getattr(
                message,
                "id",
                0,
            )
            or 0
        )
    )


    token = fastvip2_token(
        source_key,
        unit,
    )


    if unit_has_username_mention(
        unit
    ):

        FASTVIP2_HANDLED[
            token
        ] = time.time()

        log_username_filter_main(
            unit,
            "fastvip2",
            source_chat=source_key[0],
            source_topic=source_key[1],
        )

        return False


    fastvip2_prune_handled()


    if token in FASTVIP2_HANDLED:

        return False


    if token in FASTVIP2_QUEUED:

        return False


    # Message map is the durable first duplicate barrier.
    if fastvip2_routes_fully_mapped(
        routes,
        unit,
    ):

        FASTVIP2_HANDLED[
            token
        ] = time.time()

        return False


    FASTVIP2_QUEUED.add(
        token
    )


    queued_at = time.monotonic()


    await fastvip2_queue_for(
        source_key
    ).put(
        (
            routes,
            unit,
            reason,
            token,
            queued_at,
        )
    )


    log.info(
        "[FASTVIP2 QUEUED] "
        f"source={source_key[0]}_"
        f"{source_key[1]} "
        f"ids={list(fastvip2_unit_ids(unit))} "
        f"reason={reason}"
    )


    return True


async def fastvip2_process_unit(
    source_key,
    routes,
    unit,
    reason,
):

    ids = list(
        fastvip2_unit_ids(
            unit
        )
    )


    if not ids:
        return True

    # BEGIN VIP7_REBUILD_FASTVIP_GATE_V1
    vip7_rebuild_gate = vip7_rebuild_should_hold_or_suppress(
        source_key,
        unit,
    )

    if vip7_rebuild_gate is not None:
        return bool(vip7_rebuild_gate)
    # END VIP7_REBUILD_FASTVIP_GATE_V1


    first = unit[0]


    # ========================================================
    # Preserve old VIP migration suppression semantics.
    # ========================================================

    if (
        int(source_key[0])
        == VIP_TOPIC_MOVE_NEW_CHAT
    ):

        try:

            source_topic = (
                int(source_key[1])
                if source_key[1] is not None
                else None
            )

        except Exception:

            source_topic = None


        if (
            source_topic
            in VIP_TOPIC_MOVE_ACTIVE_TOPICS
        ):

            log.warning(
                "[FASTVIP2 MIGRATION PAUSED] "
                f"source={source_key[0]}_"
                f"{source_key[1]} "
                f"ids={ids}"
            )

            return False


        migration_flags = [
            message_id
            in VIP_TOPIC_MOVE_SUPPRESS_IDS
            for message_id in ids
        ]


        if migration_flags and all(
            migration_flags
        ):

            log.warning(
                "[FASTVIP2 MIGRATION HISTORY SUPPRESSED] "
                f"source={source_key[0]}_"
                f"{source_key[1]} "
                f"ids={ids}"
            )

            return True


        if any(
            migration_flags
        ):

            log.error(
                "[FASTVIP2 MIXED MIGRATION UNIT] "
                f"source={source_key[0]}_"
                f"{source_key[1]} "
                f"ids={ids} "
                f"flags={migration_flags}"
            )

            return False


    # ========================================================
    # Special exact first-copy-wins rule for provider pair.
    # ========================================================

    pair_key = None
    pair_reserved = False


    if (
        len(routes) == 1
        and is_fast_vip_pair_route(
            routes[0]
        )
    ):

        pair_key = (
            fast_vip_pair_canonical_key(
                first,
                routes[0],
            )
        )


        pair_reserved = (
            await fast_vip_pair_reserve(
                pair_key
            )
        )


        if not pair_reserved:

            log.warning(
                "[FASTVIP2 PAIR EXACT DUPLICATE SKIPPED] "
                f"source={source_key[0]}_"
                f"{source_key[1]} "
                f"ids={ids} "
                f"canonical={pair_key}"
            )

            return True


    # ========================================================
    # Fan out all destinations concurrently.
    # ========================================================

    results = await asyncio.gather(
        *[
            fast_vip_deliver_route(
                route,
                unit,
                skip_permanent_errors=True,
            )
            for route in routes
        ],
        return_exceptions=False,
    )


    success = all(
        result is True
        for result in results
    )


    if (
        not success
        and pair_reserved
    ):

        await fast_vip_pair_release(
            pair_key
        )


    if success:

        log.info(
            "[FASTVIP2 UNIT SUCCESS] "
            f"source={source_key[0]}_"
            f"{source_key[1]} "
            f"ids={ids} "
            f"reason={reason}"
        )


    return success


async def fastvip2_source_worker(
    source_key,
):

    queue = fastvip2_queue_for(
        source_key
    )


    log.warning(
        "[FASTVIP2 EVENT SOURCE READY] "
        f"source={source_key[0]}_"
        f"{source_key[1]} "
        "PRIMARY=TELEGRAM_PUSH"
    )


    while True:

        (
            routes,
            unit,
            reason,
            token,
            queued_at,
        ) = await queue.get()


        try:

            success = await fastvip2_process_unit(
                source_key,
                routes,
                unit,
                reason,
            )


            ids = list(
                fastvip2_unit_ids(
                    unit
                )
            )


            if success:

                FASTVIP2_HANDLED[
                    token
                ] = time.time()


                queue_ms = (
                    time.monotonic()
                    - queued_at
                ) * 1000.0


                telegram_age_ms = None


                first = unit[0]

                message_date = getattr(
                    first,
                    "date",
                    None,
                )


                if message_date is not None:

                    try:

                        telegram_age_ms = max(
                            0.0,
                            (
                                time.time()
                                - float(
                                    message_date.timestamp()
                                )
                            )
                            * 1000.0,
                        )

                    except Exception:

                        telegram_age_ms = None


                log.warning(
                    "[FASTVIP2 EVENT COMMITTED] "
                    f"source={source_key[0]}_"
                    f"{source_key[1]} "
                    f"ids={ids} "
                    f"reason={reason} "
                    f"queue_ms={queue_ms:.1f} "
                    f"telegram_age_ms="
                    f"{telegram_age_ms if telegram_age_ms is not None else 'unknown'}"
                )


            else:

                # Do NOT hold newer trading messages behind this one.
                #
                # Watchdog will retry the failed/missed unit separately.
                log.error(
                    "[FASTVIP2 EVENT DELIVERY FAILED - WATCHDOG RECOVERY] "
                    f"source={source_key[0]}_"
                    f"{source_key[1]} "
                    f"ids={ids}"
                )


        except Exception as exc:

            log.exception(
                "[FASTVIP2 EVENT WORKER ERROR] "
                f"source={source_key[0]}_"
                f"{source_key[1]} "
                f"{type(exc).__name__}: {exc}"
            )


        finally:

            FASTVIP2_QUEUED.discard(
                token
            )

            queue.task_done()


async def fastvip2_watchdog(
    source_key,
    routes,
):

    source_chat, source_topic = source_key


    # Make every exact-topic watchdog fail closed.
    # Never fall back to whole-chat history.
    probe_route = dict(
        routes[0]
    )


    if source_topic is not None:

        probe_route[
            "strict_source_topic"
        ] = True


    baseline = None


    # ========================================================
    # SAFE WATCHDOG BASELINE.
    #
    # The watchdog never forwards anything from this snapshot.
    # ========================================================

    while baseline is None:

        try:

            async with FASTVIP2_WATCHDOG_SEMAPHORE:

                messages = await get_route_poll_messages(
                    probe_route,
                    50,
                )


            messages = list(
                messages or []
            )


            baseline = max(
                [
                    int(
                        getattr(
                            message,
                            "id",
                            0,
                        )
                        or 0
                    )
                    for message in messages
                ]
                or [0]
            )


            FASTVIP2_WATCHDOG_BASELINES[
                source_key
            ] = baseline


            log.warning(
                "[FASTVIP2 WATCHDOG BASELINE] "
                f"source={source_chat}_"
                f"{source_topic} "
                f"baseline={baseline} "
                "COPIED_HISTORY=0"
            )


        except Exception as exc:

            log.warning(
                "[FASTVIP2 WATCHDOG BASELINE FAILED] "
                f"source={source_chat}_"
                f"{source_topic} "
                f"{type(exc).__name__}: {exc}"
            )


            await asyncio.sleep(
                2
            )


    while True:

        await asyncio.sleep(
            FASTVIP2_WATCHDOG_SECONDS
        )


        try:

            async with FASTVIP2_WATCHDOG_SEMAPHORE:

                messages = await get_route_poll_messages(
                    probe_route,
                    FASTVIP2_WATCHDOG_LIMIT,
                )


            messages = list(
                messages or []
            )


            newer = [
                message
                for message in messages
                if int(
                    getattr(
                        message,
                        "id",
                        0,
                    )
                    or 0
                ) > baseline
                and not getattr(message, "action", None)
            ]


            newer.sort(
                key=lambda message: int(
                    getattr(
                        message,
                        "id",
                        0,
                    )
                    or 0
                )
            )


            units = await fast_vip_build_units(
                newer
            )


            recovered = 0


            for unit in units:

                token = fastvip2_token(
                    source_key,
                    unit,
                )


                if token in FASTVIP2_HANDLED:
                    continue


                if token in FASTVIP2_QUEUED:
                    continue


                if fastvip2_routes_fully_mapped(
                    routes,
                    unit,
                ):

                    FASTVIP2_HANDLED[
                        token
                    ] = time.time()

                    continue


                queued = await fastvip2_enqueue(
                    source_key,
                    routes,
                    unit,
                    "watchdog_recovery",
                )


                if queued:

                    recovered += 1


            if recovered:

                log.warning(
                    "[FASTVIP2 WATCHDOG RECOVERY QUEUED] "
                    f"source={source_chat}_"
                    f"{source_topic} "
                    f"units={recovered}"
                )


        except FloodWaitError as exc:

            log.warning(
                "[FASTVIP2 WATCHDOG FLOODWAIT] "
                f"source={source_chat}_"
                f"{source_topic} "
                f"wait={int(exc.seconds)}s "
                "LIVE_EVENTS_UNAFFECTED=True"
            )


            await asyncio.sleep(
                max(
                    1,
                    int(exc.seconds),
                )
            )


        except Exception as exc:

            log.warning(
                "[FASTVIP2 WATCHDOG ERROR] "
                f"source={source_chat}_"
                f"{source_topic} "
                f"{type(exc).__name__}: {exc} "
                "LIVE_EVENTS_UNAFFECTED=True"
            )


# ============================================================
# LIVE TELEGRAM UPDATE HANDLERS
# ============================================================

@client.on(
    events.NewMessage(
        chats=FASTVIP2_SOURCE_CHATS
    )
)
async def fastvip2_new_message_handler(
    event,
):

    try:

        message = event.message


        # Albums are handled once by events.Album below.
        if getattr(
            message,
            "grouped_id",
            None,
        ):

            return


        matches = fastvip2_matching_groups(
            event.chat_id,
            message,
        )


        for source_key, routes in matches:

            await fastvip2_enqueue(
                source_key,
                routes,
                [message],
                "telegram_newmessage",
            )


    except Exception as exc:

        log.exception(
            "[FASTVIP2 NEWMESSAGE HANDLER ERROR] "
            f"chat={getattr(event, 'chat_id', None)} "
            f"{type(exc).__name__}: {exc}"
        )


@client.on(events.Album(chats=FASTVIP2_SOURCE_CHATS))
async def fastvip2_album_handler(event):
    try:
        unit = list(event.messages or [])
        if not unit:
            return
        unit.sort(key=lambda message: int(getattr(message, 'id', 0) or 0))
        first = unit[0]
        matches = fastvip2_matching_groups(event.chat_id, first)
        for source_key, routes in matches:
            await fastvip2_enqueue(source_key, routes, unit, 'telegram_album')
    except Exception as exc:
        log.exception(f"[FASTVIP2 ALBUM HANDLER ERROR] chat={getattr(event, 'chat_id', None)} {type(exc).__name__}: {exc}")


async def fast_vip_event_router_v2():

    groups = FASTVIP2_GROUPS


    if not groups:

        log.error(
            "[FASTVIP2 FATAL CONFIG] "
            "No VIP source groups"
        )

        return


    # Safety assertion:
    # The legacy V1 router must NOT be started at runtime.
    log.warning(
        "[FASTVIP2 EVENT ROUTER READY] "
        f"vip={FAST_VIP_CHAT} "
        f"source_groups={len(groups)} "
        f"source_chats={FASTVIP2_SOURCE_CHATS} "
        "PRIMARY=TELEGRAM_PUSH "
        f"WATCHDOG={FASTVIP2_WATCHDOG_SECONDS}s "
        "WATCHDOG_ISOLATED=True "
        "NO_HISTORY=True "
        "SINGLE_WRITER=True"
    )


    tasks = []


    for source_key, routes in groups.items():

        tasks.append(
            asyncio.create_task(
                fastvip2_source_worker(
                    source_key
                )
            )
        )

        tasks.append(
            asyncio.create_task(
                fastvip2_watchdog(
                    source_key,
                    routes,
                )
            )
        )


    await asyncio.gather(
        *tasks
    )


# END FAST_VIP_EVENT_ROUTER_V2


async def handle_edited_message(event):
    """A source post was edited: update its mapped copies in place.

    New posts are delivered by FASTVIP2 and the owner routes through the
    delivery engine; an edit never creates a new destination message.
    """
    message = event.message
    chat_id = event.chat_id
    if has_username_mention(message):
        log_username_filter_main([message], "edited_event", source_chat=chat_id, source_topic=None)
        return
    topic_id = topic_of(message, chat_id)
    if should_hard_block_sender(message, "edit_handler"):
        return
    if is_promo_text(text_of(message), topic_id):
        log.info(f"[promo blocked incoming] msg={getattr(message, 'id', None)} topic={topic_id}")
        return
    for route in routes_for(chat_id, topic_id, message):
        try:
            if not existing_destination_ids(message, route):
                log.info(f"[unmapped edit skipped] route={route['name']} source={chat_id}_{topic_id} msg={message.id}")
                continue
            if not await edit_existing_destination_in_place(message, route):
                log.warning(f"[mapped edit not applied] route={route['name']} source={chat_id}_{topic_id} msg={message.id}")
        except FloodWaitError as exc:
            log.warning(f"FloodWait {exc.seconds}s")
            await asyncio.sleep(min(exc.seconds + 1, 60))
        except Exception as exc:
            log.exception(f"[edit failed] {route['name']} source={chat_id}_{topic_id} msg={getattr(message, 'id', None)}: {exc}")




@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def on_message_edited(event):
    if not FORWARD_EDITED_MESSAGES:
        return
    try:
        await handle_edited_message(event)
    except Exception as exc:
        log.exception(f"[handler crash:on_message_edited] chat={getattr(event, 'chat_id', None)}: {exc}")
        alert_crash("imperium-telegram-worker:on_message_edited", exc)


@client.on(events.MessageDeleted(chats=SOURCE_CHATS))
async def on_message_deleted(event):
    if not MIRROR_DELETED_MESSAGES:
        return

    try:
        chat_id = getattr(event, "chat_id", None)
        deleted_ids = list(getattr(event, "deleted_ids", []) or [])

        if not deleted_ids:
            one_id = getattr(event, "deleted_id", None)
            if one_id:
                deleted_ids = [one_id]

        if chat_id is None:
            log.warning(f"[delete mirror skipped] missing chat_id deleted_ids={deleted_ids}")
            return

        for msg_id in deleted_ids:
            try:
                await delete_mapped_destination_for_source(chat_id, msg_id)
            except Exception as exc:
                log.warning(f"[delete mirror item failed] chat={chat_id} msg={msg_id}: {exc}")

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
        req_cls = channel_function('GetForumTopicsByIDRequest')
        if req_cls:
            res = await client(req_cls(channel=int(chat_id), topics=[int(topic_id)]))
            topics = getattr(res, 'topics', None) or []
            if topics:
                title = getattr(topics[0], 'title', None)
                if title:
                    return str(title).strip()
    except Exception as exc:
        log.info(f'[topic title api fallback] chat={chat_id} topic={topic_id}: {type(exc).__name__}: {exc}')
    try:
        msg = await client.get_messages(int(chat_id), ids=int(topic_id))
        if msg:
            action = getattr(msg, 'action', None)
            for attr in ('title', 'new_title'):
                value = getattr(action, attr, None)
                if value:
                    return str(value).strip()
            raw = text_of(msg)
            if raw:
                return raw.splitlines()[0].strip()
    except Exception as exc:
        log.warning(f'[topic title fetch failed] chat={chat_id} topic={topic_id}: {exc}')
    return ''


# BEGIN VIP7_REBUILD_FROM_RELAY508_V1

VIP7_REBUILD_FROM_RELAY508_ONCE = (
    os.environ.get(
        "VIP7_REBUILD_FROM_RELAY508_ONCE",
        "0",
    ).strip()
    == "1"
)

VIP7_REBUILD_SOURCE_CHAT = -1004367822325
VIP7_REBUILD_SOURCE_TOPIC = 508
VIP7_REBUILD_DEST_CHAT = -1003726286301
VIP7_REBUILD_DEST_TOPIC = 7

VIP7_REBUILD_STATE_FILE = (
    DATA_DIR / "vip7_rebuild_from_relay508_v1.state.json"
)
VIP7_REBUILD_DONE_FILE = (
    DATA_DIR / "vip7_rebuild_from_relay508_v1.done.json"
)

def vip7_rebuild_load_state():
    if not VIP7_REBUILD_STATE_FILE.exists():
        return {}

    try:
        value = json.loads(
            VIP7_REBUILD_STATE_FILE.read_text(
                encoding="utf-8"
            )
        )
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


VIP7_REBUILD_SUPPRESS_IDS = set(
    int(value)
    for value in vip7_rebuild_load_state().get(
        "suppress_dest_ids",
        [],
    )
    if str(value).strip()
)


def vip7_rebuild_in_progress():
    return (
        VIP7_REBUILD_FROM_RELAY508_ONCE
        and not VIP7_REBUILD_DONE_FILE.exists()
    )

def vip7_rebuild_should_hold_or_suppress(
    source_key,
    unit,
):
    ids = [
        int(getattr(message, "id", 0) or 0)
        for message in (unit or [])
    ]

    if not ids:
        return None

    source_chat = int(source_key[0])
    source_topic = (
        int(source_key[1])
        if source_key[1] is not None
        else None
    )

    # Historical refill messages created in VIP7 must never fan
    # out through existing VIP7 outbound routes.
    if (
        source_chat == VIP7_REBUILD_DEST_CHAT
        and source_topic == VIP7_REBUILD_DEST_TOPIC
    ):
        flags = [
            message_id in VIP7_REBUILD_SUPPRESS_IDS
            for message_id in ids
        ]

        if flags and all(flags):
            log.warning(
                "[VIP7 REBUILD OUTBOUND HISTORY SUPPRESSED] "
                f"ids={ids}"
            )
            return True

        if vip7_rebuild_in_progress():
            log.warning(
                "[VIP7 REBUILD OUTBOUND HELD] "
                f"ids={ids} "
                "WATCHDOG_WILL_RECHECK=True"
            )
            return False

    # Hold new Relay508 live input until purge+refill finishes.
    if (
        source_chat == VIP7_REBUILD_SOURCE_CHAT
        and source_topic == VIP7_REBUILD_SOURCE_TOPIC
        and vip7_rebuild_in_progress()
    ):
        log.warning(
            "[VIP7 REBUILD RELAY508 LIVE HELD] "
            f"ids={ids} "
            "WATCHDOG_WILL_RECHECK=True"
        )
        return False

    return None


# END VIP7_REBUILD_FROM_RELAY508_V1


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
        log.warning('[ENTITY CACHE WARMUP] disabled')
        return
    wanted = set()
    for route in ROUTES:
        for key in ('source_chat', 'dest_chat'):
            try:
                wanted.add(int(route[key]))
            except Exception:
                continue
    dialogs = 0
    try:
        async for _dialog in client.iter_dialogs():
            dialogs += 1
    except FloodWaitError as exc:
        log.warning(f'[ENTITY CACHE WARMUP FLOODWAIT] wait={exc.seconds}s; continuing without a full warmup')
        return
    except Exception as exc:
        log.warning(f'[ENTITY CACHE WARMUP FAILED SAFE] {type(exc).__name__}: {exc}')
        return
    resolved = 0
    unresolved = []
    for chat_id in sorted(wanted):
        try:
            await client.get_input_entity(chat_id)
            resolved += 1
        except Exception as exc:
            unresolved.append((chat_id, type(exc).__name__))
    log.warning(f'[ENTITY CACHE WARMUP DONE] dialogs={dialogs} route_peers={len(wanted)} resolved={resolved} unresolved={len(unresolved)}')
    for chat_id, error_name in unresolved:
        log.warning(f'[ENTITY STILL UNRESOLVED] chat={chat_id} {error_name}')


# OFF BY DEFAULT — DO NOT ENABLE ON THIS TELETHON BUILD.
#
# The reliable check is GetForumTopicsByIDRequest, which this Telethon runtime
# does not expose. The service-message fallback below is NOT a valid substitute:
# a live topic whose creation service message is missing or unretrievable reads
# back as deleted, and on 2026-09-14 that wrongly dropped most routes in
# production (Triad FX, NS Trades, all Route568/Route569, and more).
#
# Only turn this on after upgrading Telethon to a version that has
# GetForumTopicsByIDRequest, and verify the guard reports missing=0 on a healthy
# start before trusting it.


async def main():
    await start_runtime_guard("imperium-telegram-worker", log)
    liveness.LoopWatchdog("ExposedFX MAIN", DATA_DIR, log).start()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    await warm_entity_cache_once()

    owner_module = __import__("sys").modules[__name__]
    from telegram_worker.engine.runtime import install_engine
    install_engine(owner_module)
    from telegram_worker.owner_requested_routes_20261102 import run_owner_requested_routes
    owner_module.OWNER_REQUESTED_ROUTES_TASK = asyncio.create_task(run_owner_requested_routes(owner_module))
    from telegram_worker.owner_formatted_route_20261103 import run_owner_formatted_route
    owner_module.OWNER_FORMATTED_ROUTE_TASK = asyncio.create_task(run_owner_formatted_route(owner_module))
    from telegram_worker.health_monitor import install_health_monitor
    owner_module.HEALTH_MONITOR = install_health_monitor("ExposedFX MAIN", client, DATA_DIR)
    from telegram_worker.destination_directory import log_destination_directory
    owner_module.DESTINATION_DIRECTORY_TASK = asyncio.create_task(log_destination_directory(owner_module))
    from telegram_worker.engine.audit import run_replay_audit
    owner_module.ENGINE_AUDIT_TASK = asyncio.create_task(run_replay_audit(owner_module, DELIVERY_ENGINE.planner))
    log.warning(
        "[MAIN READY] "
        f"routes={len(ROUTES)} source_chats={SOURCE_CHATS} "
        f"message_map_entries={len(message_map)} dry_run={DRY_RUN} "
        f"edits={FORWARD_EDITED_MESSAGES} deletes={MIRROR_DELETED_MESSAGES} "
        f"copy_retry_attempts={COPY_RETRY_ATTEMPTS} blocked_senders={sorted(BLOCKED_SENDER_IDS)} "
        f"telethon={telethon_version} python={sys.version.split()[0]}"
    )
    asyncio.create_task(relay508_mapped_edit_reconcile_loop())
    asyncio.create_task(fast_vip_event_router_v2())
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



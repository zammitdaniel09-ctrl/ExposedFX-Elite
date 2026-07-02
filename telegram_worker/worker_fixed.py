import os
import json
import base64
import asyncio
import logging
import time
import re
import hashlib
from pathlib import Path

import requests
import telethon
from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaWebPage

from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
from telegram_worker.provider_profiles import is_promo_text
from telegram_worker.routes import ROUTES
from telegram_worker.parser import parse_signal
from telegram_worker.stats_reporter import WeeklyStats

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("imperium-worker")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AUTO_TOKEN = os.environ.get("AUTO_TOKEN", "change-this-token")
DRY_RUN = os.environ.get("DRY_RUN", "0").strip() == "1"
FORWARD_EDITED_MESSAGES = os.environ.get("FORWARD_EDITED_MESSAGES", "1").strip() == "1"
PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER = os.environ.get("PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER", "1").strip() == "1"
ENABLE_ALBUM_HANDLER = os.environ.get("ENABLE_ALBUM_HANDLER", "0").strip() == "1"
ENABLE_ROUTE_FALLBACK_ALL_MESSAGES = os.environ.get("ENABLE_ROUTE_FALLBACK_ALL_MESSAGES", "1").strip() == "1"
ENABLE_ALBUM_REUPLOAD_FALLBACK = os.environ.get("ENABLE_ALBUM_REUPLOAD_FALLBACK", "1").strip() == "1"
NO_ROUTE_TEXT_LIMIT = int(os.environ.get("NO_ROUTE_TEXT_LIMIT", "180"))
COPY_RETRY_ATTEMPTS = int(os.environ.get("COPY_RETRY_ATTEMPTS", "2"))
COPY_RETRY_SLEEP_CAP_SECONDS = int(os.environ.get("COPY_RETRY_SLEEP_CAP_SECONDS", "120"))
ROUTE_TITLE_CHECK_INTERVAL_SECONDS = int(os.environ.get("ROUTE_TITLE_CHECK_INTERVAL_SECONDS", "3600"))
NEW_MIRROR_DEBUG_CHATS_RAW = os.environ.get(
    "NEW_MIRROR_DEBUG_CHATS",
    "-1003812195730,-1003371106919,-1003651353503,-1003087047858,-1002817163788",
).strip()

NEW_MIRROR_DEBUG_CHATS = {
    int(x)
    for x in re.split(r"[,\s]+", NEW_MIRROR_DEBUG_CHATS_RAW)
    if x.strip()
}

NEW_MIRROR_STARTUP_PROBE = os.environ.get("NEW_MIRROR_STARTUP_PROBE", "1").strip() == "1"
NEW_MIRROR_POLLING_ENABLED = os.environ.get("NEW_MIRROR_POLLING_ENABLED", "1").strip() == "1"
NEW_MIRROR_POLL_SECONDS = int(os.environ.get("NEW_MIRROR_POLL_SECONDS", "8"))
NEW_MIRROR_POLL_LIMIT = int(os.environ.get("NEW_MIRROR_POLL_LIMIT", "8"))
NEW_MIRROR_BACKFILL_ON_START = os.environ.get("NEW_MIRROR_BACKFILL_ON_START", "1").strip() == "1"
NEW_MIRROR_BACKFILL_LIMIT = int(os.environ.get("NEW_MIRROR_BACKFILL_LIMIT", "3"))

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
new_mirror_poll_last_ids = {}


MIRROR_STRUCTURE_REPAIR = os.environ.get("MIRROR_STRUCTURE_REPAIR", "1").strip() == "1"
STRICT_ROUTE_TITLE_CHECK = os.environ.get("STRICT_ROUTE_TITLE_CHECK", "0").strip() == "1"
AUTO_SYNC_TOPIC_TITLES = os.environ.get("AUTO_SYNC_TOPIC_TITLES", "1").strip() == "1"
MIRROR_DELETED_MESSAGES = os.environ.get("MIRROR_DELETED_MESSAGES", "1").strip() == "1"
VERIFY_ROUTE_TITLES = os.environ.get("VERIFY_ROUTE_TITLES", "1").strip() == "1"

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
SESSION_BASE = DATA_DIR / "session"
SESSION_FILE = DATA_DIR / "session.session"
MESSAGE_MAP_FILE = DATA_DIR / "message_map.json"
DEDUP_FILE = DATA_DIR / "dedupe_map.json"
DEDUP_WINDOW_SECONDS = int(os.environ.get("DEDUP_WINDOW_SECONDS", "900"))

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


BLOCKED_SENDER_IDS_RAW = os.environ.get("BLOCKED_SENDER_IDS", "7556281143").strip()
BLOCKED_SENDER_IDS = {
    int(x)
    for x in re.split(r"[,\s]+", BLOCKED_SENDER_IDS_RAW)
    if x.strip()
}

BLOCKED_SENDER_CLEANUP_ENABLED = os.environ.get("BLOCKED_SENDER_CLEANUP_ENABLED", "1").strip() == "1"
BLOCKED_SENDER_CLEANUP_LIMIT = int(os.environ.get("BLOCKED_SENDER_CLEANUP_LIMIT", "300"))
BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW = os.environ.get("BLOCKED_SENDER_CLEANUP_DEST_TOPICS", "28840").strip()
BLOCKED_SENDER_CLEANUP_DEST_TOPICS = {
    int(x)
    for x in re.split(r"[,\\s]+", BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW)
    if x.strip()
} if BLOCKED_SENDER_CLEANUP_DEST_TOPICS_RAW else set()

SOURCE_CHATS = sorted(set(r["source_chat"] for r in ROUTES))
POSTED_SIGNAL_KEYS = set()
stats = WeeklyStats(DATA_DIR)
route_title_status = {}


def _clean_b64(value: str) -> str:
    return "".join((value or "").split()).strip()


def combined_login_blob():
    direct = _clean_b64(os.environ.get("SESSION_B64", ""))
    if direct:
        return direct, "SESSION_B64"

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
    except Exception:
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


def save_dedupe():
    try:
        cutoff = time.time() - DEDUP_WINDOW_SECONDS
        old = list(content_dedupe_map.keys())
        for k in old:
            if float(content_dedupe_map.get(k, 0)) < cutoff:
                content_dedupe_map.pop(k, None)

        tmp = DEDUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(content_dedupe_map), encoding="utf-8")
        tmp.replace(DEDUP_FILE)
    except Exception as exc:
        log.warning(f"[dedupe save failed] {exc}")


def normalise_for_dedupe(text):
    text = (text or "").lower()
    text = text.replace("\\ufe0f", "").replace("\\u200d", "").replace("\\u200b", "")
    text = re.sub(r"\\s+", "", text)
    return text.strip()


def media_tag(message):
    if not getattr(message, "media", None):
        return "no-media"

    # Keep albums/screenshots safer: exact duplicate media/text gets blocked,
    # different images with same caption can still pass.
    media_id = getattr(getattr(message, "photo", None), "id", None)
    if media_id:
        return f"photo:{media_id}"

    document = getattr(message, "document", None)
    if document:
        return f"doc:{getattr(document, 'id', '')}:{getattr(document, 'size', '')}"

    return "media"


def dedupe_key(route, message, text):
    """
    Normal dedupe keeps sources separate.
    For selected destination topics, dedupe across different source groups too,
    because providers often forward each other's exact messages.
    """
    cross_source = int(route["dest_topic"]) in CROSS_SOURCE_DEDUP_DEST_TOPICS

    source_chat_key = "ANY_SOURCE" if cross_source else str(route["source_chat"])
    source_topic_key = "ANY_TOPIC" if cross_source else str(route.get("source_topic"))

    # For text/caption messages in cross-source dedupe topics, prioritise text.
    # This blocks duplicate forwarded posts even if Telegram gives different media ids.
    media_key = "TEXT_OR_CAPTION" if cross_source and normalise_for_dedupe(text) else media_tag(message)

    base = "|".join([
        source_chat_key,
        source_topic_key,
        str(route["dest_chat"]),
        str(route["dest_topic"]),
        normalise_for_dedupe(text),
        media_key,
    ])
    return hashlib.sha256(base.encode("utf-8", errors="ignore")).hexdigest()


def is_recent_duplicate(route, message, text):
    key = dedupe_key(route, message, text)
    last = float(content_dedupe_map.get(key, 0) or 0)
    return (time.time() - last) <= DEDUP_WINDOW_SECONDS


def remember_dedupe(route, message, text):
    key = dedupe_key(route, message, text)
    content_dedupe_map[key] = time.time()
    save_dedupe()


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


def existing_destination_id(message, route):
    ids = existing_destination_ids(message, route)
    return ids[0] if ids else None


def existing_destination_ids(message, route):
    key = map_key(route["source_chat"], message.id, route["dest_chat"], route["dest_topic"])
    return mapped_ids_from_value(message_map.get(key))


def all_existing_destination_ids_for_source(message, route):
    """
    Strong edit cleanup:
    - exact key match
    - scan old map keys for same source chat + source msg id + same destination
    This protects against topic/fallback route changes between original and edit.
    """
    wanted_source = str(route["source_chat"])
    wanted_msg = str(message.id)
    wanted_dest_chat = str(route["dest_chat"])
    wanted_dest_topic = str(route["dest_topic"])

    ids = []
    keys_to_remove = set()

    exact_key = map_key(route["source_chat"], message.id, route["dest_chat"], route["dest_topic"])

    for key, value in list(message_map.items()):
        try:
            parts = str(key).split(":")
            if len(parts) != 4:
                continue

            source_chat, source_msg_id, dest_chat, dest_topic = parts

            same_source_msg = source_chat == wanted_source and source_msg_id == wanted_msg
            same_dest = dest_chat == wanted_dest_chat and dest_topic == wanted_dest_topic

            if key == exact_key or (same_source_msg and same_dest):
                ids.extend(mapped_ids_from_value(value))
                keys_to_remove.add(key)

        except Exception:
            continue

    clean_ids = []
    for mid in ids:
        if mid and mid not in clean_ids:
            clean_ids.append(mid)

    return clean_ids, keys_to_remove


async def delete_existing_destination(message, route):
    ids, keys_to_remove = all_existing_destination_ids_for_source(message, route)

    if not ids:
        log.info(f"[edited cleanup] no previous mapped copy source={getattr(message, 'id', None)} route={route['name']}")
        return False

    try:
        await client.delete_messages(route["dest_chat"], ids)

        for key in keys_to_remove:
            message_map.pop(key, None)

        save_map()

        log.info(
            f"[edited cleanup] deleted old forwarded msgs {ids} "
            f"for source={message.id} route={route['name']}"
        )
        return True

    except Exception as exc:
        log.warning(
            f"[edited cleanup failed] source={message.id} route={route['name']} "
            f"dest_msgs={ids}: {exc}"
        )
        return False



write_login_file()
message_map = load_map()
content_dedupe_map = load_dedupe()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)


def text_of(message):
    return message.message or message.raw_text or message.text or ""


def entities_of(message):
    return getattr(message, "entities", None) or []




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


def is_blocked_sender(message):
    ids = sender_ids_for_message(message)
    return bool(ids & BLOCKED_SENDER_IDS)


def should_hard_block_sender(message, context="", route=None):
    ids = sender_ids_for_message(message)
    matched = sorted(ids & BLOCKED_SENDER_IDS)

    if not matched:
        return False

    route_name = route.get("name") if isinstance(route, dict) else None
    route_dest = (
        f"{route.get('dest_chat')}_{route.get('dest_topic')}"
        if isinstance(route, dict)
        else None
    )

    log.warning(
        f"[blocked sender hard stop] blocked_ids={matched} "
        f"all_sender_ids={sorted(ids)} msg={getattr(message, 'id', None)} "
        f"context={context} route={route_name} dest={route_dest}"
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



def is_blocked_destination(route):
    try:
        return (
            int(route.get("dest_chat")) == BLOCKED_DEST_CHAT
            and int(route.get("dest_topic")) in BLOCKED_DEST_TOPICS
        )
    except Exception:
        return False


def routes_for(chat_id, topic_id, message=None):
    found = []
    for route in ROUTES:
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


def mapped_reply_id(message, route):
    for source_msg_id in reply_source_ids(message):
        if route["source_topic"] is not None and source_msg_id == route["source_topic"]:
            continue

        key = map_key(route["source_chat"], source_msg_id, route["dest_chat"], route["dest_topic"])
        mapped = message_map.get(key)

        if mapped:
            try:
                return int(mapped)
            except Exception:
                return None

    return None


def reply_target(message, route):
    reply = getattr(message, "reply_to", None)

    if not reply:
        return route["dest_topic"]

    mapped = mapped_reply_id(message, route)

    if mapped:
        return mapped

    log.info(
        f"[reply fallback] {route['name']} could not find mapped source reply "
        f"ids={reply_source_ids(message)} -> using destination topic {route['dest_topic']}"
    )

    return route["dest_topic"]


def remember_message(src_msg, dst_msg, route):
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
            save_map()
        return

    mid = getattr(dst_msg, "id", None)
    if mid:
        message_map[key] = int(mid)
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
            parts = str(key).split(":")
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
            log.warning(f"[delete mirror scan failed] key={key}: {exc}")

    if not targets:
        log.info(f"[delete mirror] no mapped destination for source={source_chat}:{source_msg_id}")
        return False

    deleted_any = False

    for dest_chat, ids in targets.items():
        try:
            await client.delete_messages(dest_chat, ids)
            log.info(f"[delete mirror] deleted dest={dest_chat} msgs={ids} for source={source_chat}:{source_msg_id}")
            deleted_any = True
        except Exception as exc:
            log.warning(f"[delete mirror failed] dest={dest_chat} ids={ids} source={source_chat}:{source_msg_id}: {exc}")

    if deleted_any:
        for key in keys_to_remove:
            message_map.pop(key, None)
        save_map()

    return deleted_any


def clean_title_exact(value):
    return (value or "").strip()


def clean_title_soft(value):
    value = (value or "").casefold().strip()
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def title_from_entity(entity):
    for attr in ("title", "first_name", "username"):
        value = getattr(entity, attr, None)
        if value:
            return str(value).strip()
    return ""


def title_from_topic_message(msg):
    if not msg:
        return ""

    action = getattr(msg, "action", None)

    for attr in ("title", "new_title"):
        value = getattr(action, attr, None)
        if value:
            return str(value).strip()

    raw = text_of(msg)
    if raw:
        return raw.splitlines()[0].strip()

    return ""


async def verify_route_title(route):
    """
    Safety checker for mirror routes:
    compares source group title with destination topic title.
    It logs mismatch but does NOT block forwarding.
    """
    try:
        source_entity = await client.get_entity(route["source_chat"])
        source_title = title_from_entity(source_entity)

        topic_msg = await client.get_messages(route["dest_chat"], ids=int(route["dest_topic"]))
        dest_title = title_from_topic_message(topic_msg)

        source_exact = clean_title_exact(source_title)
        dest_exact = clean_title_exact(dest_title)

        exact_ok = source_exact == dest_exact
        soft_ok = clean_title_soft(source_exact) == clean_title_soft(dest_exact)

        if exact_ok:
            log.info(
                f"[route title check ok] route={route['name']} "
                f"source_title={source_exact!r} dest_topic_title={dest_exact!r}"
            )
        elif soft_ok:
            log.warning(
                f"[route title check soft-ok] route={route['name']} "
                f"source_title={source_exact!r} dest_topic_title={dest_exact!r}"
            )
        else:
            log.warning(
                f"[route title check mismatch] route={route['name']} "
                f"source_title={source_exact!r} dest_topic_title={dest_exact!r} "
                f"source={route['source_chat']} dest={route['dest_chat']}_{route['dest_topic']}"
            )

        return exact_ok or soft_ok

    except Exception as exc:
        log.warning(
            f"[route title check failed] route={route.get('name')} "
            f"source={route.get('source_chat')} dest={route.get('dest_chat')}_{route.get('dest_topic')}: {exc}"
        )
        return False


async def verify_route_titles_once():
    if not VERIFY_ROUTE_TITLES:
        return

    checked = 0

    for route in ROUTES:
        if not route.get("verify_title"):
            continue

        checked += 1
        await verify_route_title(route)

    log.info(f"[route title checker done] checked={checked}")



def log_stats(route, message, text):
    result = stats.log_message(route, message, text)
    if result:
        log.info(f"[stats logged] {route['name']} {result['status']} {result['pips']} pips")


def maybe_post_signal(route, message, text):
    parsed = parse_signal(text)
    if not parsed:
        return

    sig_key = f"{route['source_chat']}:{message.id}"
    if sig_key in POSTED_SIGNAL_KEYS:
        return
    POSTED_SIGNAL_KEYS.add(sig_key)

    payload = {
        "source": route["name"],
        "source_chat_id": route["source_chat"],
        "source_message_id": message.id,
        "raw_text": text,
        **parsed,
    }

    if DRY_RUN:
        log.info(f"[DRY_RUN signal] {route['name']} {parsed['direction']} {parsed['symbol']}")
        return

    try:
        res = requests.post(
            f"{SERVER_URL}/api/v1/signals",
            json=payload,
            headers={"X-AUTO-TOKEN": AUTO_TOKEN},
            timeout=12,
        )
        if res.status_code >= 400:
            log.warning(f"[signal rejected] {route['name']} {res.status_code}: {res.text}")
        else:
            log.info(f"[signal posted] {route['name']} {parsed['direction']} {parsed['symbol']} -> {res.text}")
    except Exception as exc:
        log.error(f"[signal post failed] {route['name']}: {exc}")


def safe_maybe_post_signal(route, message, text, context=""):
    """
    Forwarding must never fail because signal parsing/API side logic failed.
    """
    try:
        maybe_post_signal(route, message, text)
    except Exception as exc:
        log.warning(
            f"[signal sidecar skipped] route={route.get('name')} "
            f"msg={getattr(message, 'id', None)} context={context}: {type(exc).__name__}: {exc}"
        )


def safe_log_stats(route, message, text, context=""):
    """
    Forwarding must never fail because stats parsing failed.
    """
    try:
        log_stats(route, message, text)
    except Exception as exc:
        log.warning(
            f"[stats sidecar skipped] route={route.get('name')} "
            f"msg={getattr(message, 'id', None)} context={context}: {type(exc).__name__}: {exc}"
        )



async def send_media_exact(message, route, target_reply, text, entities):
    """
    Robust media copier:
    1. Try direct Telethon media resend.
    2. If that fails, download and re-upload.
    This preserves photos/videos/documents + captions + caption entities.
    """
    try:
        return await client.send_file(
            route["dest_chat"],
            message.media,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
        )
    except Exception as exc:
        log.warning(f"[media direct copy failed] msg={message.id} route={route['name']}: {exc}")

    cache_dir = DATA_DIR / "media_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    downloaded = None
    try:
        downloaded = await message.download_media(file=str(cache_dir / f"{route['dest_topic']}_{message.id}"))
        if not downloaded:
            raise RuntimeError("download_media returned no file path")

        sent = await client.send_file(
            route["dest_chat"],
            downloaded,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
        )

        return sent

    finally:
        if downloaded:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except Exception:
                pass


async def ensure_replied_message_copied(message, route, depth=0):
    """
    If source message replies to another source message and that parent was not copied yet,
    copy the parent first, then this message can reply to the correct destination message.
    This prevents detached replies in ExposedFX topics.
    """
    if depth > 2:
        return False

    for source_msg_id in reply_source_ids(message):
        try:
            source_msg_id = int(source_msg_id)
        except Exception:
            continue

        if route.get("source_topic") is not None and source_msg_id == int(route["source_topic"]):
            continue

        key = map_key(route["source_chat"], source_msg_id, route["dest_chat"], route["dest_topic"])
        if message_map.get(key):
            return True

        try:
            parent = await client.get_messages(route["source_chat"], ids=source_msg_id)
            if not parent:
                continue

            if should_hard_block_sender(parent, "reply_parent", route):
                continue

            await copy_one(parent, route, edited=False, ensure_reply=False)
            log.info(
                f"[reply parent copied] route={route['name']} "
                f"parent_source={source_msg_id} -> dest={route['dest_chat']}_{route['dest_topic']}"
            )
            return True

        except Exception as exc:
            log.warning(
                f"[reply parent copy failed] route={route['name']} "
                f"parent_source={source_msg_id}: {exc}"
            )

    return False


def sent_as_list(sent):
    if sent is None:
        return []
    if isinstance(sent, list):
        return [x for x in sent if x]
    return [sent]


def sent_ids(sent):
    ids = []
    for item in sent_as_list(sent):
        mid = getattr(item, "id", None)
        if mid:
            ids.append(int(mid))
    return ids


def normalise_structure_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def mirror_structure_status(source_message, sent, expected_text):
    items = sent_as_list(sent)

    if not items:
        return False, "no_sent_message"

    source_has_media = is_real_media(source_message)
    dest_has_media = any(is_real_media(item) for item in items)

    if source_has_media and not dest_has_media:
        return False, "missing_media"

    if not source_has_media and dest_has_media:
        return False, "unexpected_media"

    expected = normalise_structure_text(expected_text)
    received = normalise_structure_text("\n".join(text_of(item) for item in items if text_of(item)))

    if expected and expected != received:
        return False, "text_or_caption_mismatch"

    return True, "ok"


async def delete_sent_copy(route, sent, reason):
    ids = sent_ids(sent)

    if not ids:
        return

    try:
        await client.delete_messages(route["dest_chat"], ids)
        log.warning(f"[mirror structure repair] deleted bad copy ids={ids} route={route['name']} reason={reason}")
    except Exception as exc:
        log.warning(f"[mirror structure repair delete failed] ids={ids} route={route['name']} reason={reason}: {exc}")


async def repair_bad_mirror_structure(message, route, target_reply, text, entities, sent):
    if not MIRROR_STRUCTURE_REPAIR:
        return sent

    ok, reason = mirror_structure_status(message, sent, text)

    if ok:
        return sent

    await delete_sent_copy(route, sent, reason)

    try:
        if is_real_media(message):
            repaired = await send_media_exact(message, route, target_reply, text, entities)
        else:
            repaired = await client.send_message(
                route["dest_chat"],
                text,
                formatting_entities=entities if text else None,
                parse_mode=None,
                reply_to=target_reply,
                link_preview=True,
            )

        ok2, reason2 = mirror_structure_status(message, repaired, text)

        if ok2:
            log.info(f"[mirror structure repair ok] route={route['name']} source_msg={message.id}")
        else:
            log.warning(f"[mirror structure repair still bad] route={route['name']} source_msg={message.id} reason={reason2}")

        return repaired

    except Exception as exc:
        log.warning(f"[mirror structure repair resend failed] route={route['name']} source_msg={message.id}: {exc}")
        return sent



async def copy_one(message, route, edited=False, ensure_reply=True):
    if should_hard_block_sender(message, "copy_one", route):
        return None

    if edited:
        await delete_existing_destination(message, route)

    text = text_of(message)
    entities = entities_of(message)

    if ensure_reply:
        await ensure_replied_message_copied(message, route)

    target_reply = reply_target(message, route)

    if DRY_RUN:
        log.info(f"[DRY_RUN copy] {route['name']}")
        return None

    if is_real_media(message):
        log.info(
            f"[media copy] msg={message.id} route={route['name']} "
            f"has_caption={bool(text)} reply_to={target_reply}"
        )
        sent = await send_media_exact(message, route, target_reply, text, entities)

    else:
        if not text:
            log.info(f"[skip empty unsupported] route={route['name']} msg={message.id}")
            return None

        sent = await client.send_message(
            route["dest_chat"],
            text,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=target_reply,
            link_preview=True,
        )

    sent = await repair_bad_mirror_structure(message, route, target_reply, text, entities, sent)
    remember_message(message, sent, route)
    return sent


async def copy_album(messages, route):
    for item in messages:
        if should_hard_block_sender(item, "album", route):
            return None

    first = messages[0]
    await ensure_replied_message_copied(first, route)
    target_reply = reply_target(first, route)

    files = []
    caption = None
    caption_entities = None

    for msg in messages:
        if is_real_media(msg):
            files.append(msg.media)
        if caption is None:
            txt = text_of(msg)
            if txt:
                caption = txt
                caption_entities = entities_of(msg)

    if DRY_RUN:
        log.info(f"[DRY_RUN album] {route['name']} items={len(files)}")
        return None

    if not files:
        log.info(f"[album skip empty] {route['name']} first_msg={getattr(first, 'id', None)}")
        return None

    sent = None
    downloaded_files = []

    try:
        sent = await client.send_file(
            route["dest_chat"],
            files,
            caption=caption,
            formatting_entities=caption_entities,
            parse_mode=None,
            reply_to=target_reply,
        )
    except Exception as exc:
        log.warning(f"[album direct copy failed] {route['name']} first_msg={getattr(first, 'id', None)}: {exc}")

        if not ENABLE_ALBUM_REUPLOAD_FALLBACK:
            raise

        cache_dir = DATA_DIR / "media_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        try:
            for msg in messages:
                if not is_real_media(msg):
                    continue

                downloaded = await msg.download_media(
                    file=str(cache_dir / f"album_{route['dest_topic']}_{msg.id}")
                )

                if downloaded:
                    downloaded_files.append(downloaded)

            if not downloaded_files:
                raise RuntimeError("album fallback downloaded no files")

            sent = await client.send_file(
                route["dest_chat"],
                downloaded_files,
                caption=caption,
                formatting_entities=caption_entities,
                parse_mode=None,
                reply_to=target_reply,
            )

            log.info(f"[album fallback reupload ok] {route['name']} items={len(downloaded_files)}")

        finally:
            for downloaded in downloaded_files:
                try:
                    Path(downloaded).unlink(missing_ok=True)
                except Exception:
                    pass

    if isinstance(sent, list):
        for src, dst in zip(messages, sent):
            remember_message(src, dst, route)
    else:
        remember_message(first, sent, route)

    return sent


def is_transient_copy_error(exc):
    text = str(exc).lower()
    patterns = (
        "timeout",
        "timed out",
        "connection",
        "server disconnected",
        "temporarily",
        "transport",
        "network",
        "request failed",
    )
    return any(p in text for p in patterns)


async def copy_one_with_retry(message, route, edited=False):
    attempts = max(1, COPY_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await copy_one(message, route, edited=edited)
        except FloodWaitError as exc:
            last_exc = exc
            wait = min(int(exc.seconds) + 1, COPY_RETRY_SLEEP_CAP_SECONDS)
            log.warning(
                f"[copy retry floodwait] attempt={attempt}/{attempts} wait={wait}s "
                f"route={route['name']} msg={getattr(message, 'id', None)}"
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and is_transient_copy_error(exc):
                wait = min(2 * attempt, COPY_RETRY_SLEEP_CAP_SECONDS)
                log.warning(
                    f"[copy retry transient] attempt={attempt}/{attempts} wait={wait}s "
                    f"route={route['name']} msg={getattr(message, 'id', None)} error={exc}"
                )
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc

    return None


async def copy_album_with_retry(messages, route):
    attempts = max(1, COPY_RETRY_ATTEMPTS)
    first = messages[0] if messages else None
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await copy_album(messages, route)
        except FloodWaitError as exc:
            last_exc = exc
            wait = min(int(exc.seconds) + 1, COPY_RETRY_SLEEP_CAP_SECONDS)
            log.warning(
                f"[album retry floodwait] attempt={attempt}/{attempts} wait={wait}s "
                f"route={route['name']} first_msg={getattr(first, 'id', None)}"
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and is_transient_copy_error(exc):
                wait = min(2 * attempt, COPY_RETRY_SLEEP_CAP_SECONDS)
                log.warning(
                    f"[album retry transient] attempt={attempt}/{attempts} wait={wait}s "
                    f"route={route['name']} first_msg={getattr(first, 'id', None)} error={exc}"
                )
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc

    return None


def route_debug_names(routes):
    try:
        return [r.get("name") for r in routes]
    except Exception:
        return []


async def probe_new_mirror_routes_once():
    """
    Startup probe for the 3 new mirror sources.
    This does not forward old messages.
    It only proves whether the Railway Telegram account can see each source and what the latest messages are.
    """
    if not NEW_MIRROR_STARTUP_PROBE:
        return

    checked = 0

    for route in ROUTES:
        if int(route.get("source_chat")) not in NEW_MIRROR_DEBUG_CHATS:
            continue

        checked += 1

        try:
            entity = await client.get_entity(route["source_chat"])
            title = title_from_entity(entity) if "title_from_entity" in globals() else getattr(entity, "title", "") or getattr(entity, "username", "") or ""
            log.info(
                f"[new mirror probe access ok] route={route['name']} "
                f"source={route['source_chat']} title={title!r} "
                f"dest={route['dest_chat']}_{route['dest_topic']}"
            )

            latest = await client.get_messages(route["source_chat"], limit=3)

            if not latest:
                log.warning(f"[new mirror probe empty] route={route['name']} source={route['source_chat']} no latest messages")
                continue

            for msg in reversed(latest):
                try:
                    topic = topic_of(msg, route["source_chat"])
                    txt = text_of(msg)
                    matched = routes_for(route["source_chat"], topic, msg)

                    log.info(
                        f"[new mirror probe latest] route={route['name']} "
                        f"source={route['source_chat']} msg={getattr(msg, 'id', None)} "
                        f"topic={topic} media={is_real_media(msg)} "
                        f"text={txt[:120]!r} matched_routes={route_debug_names(matched)}"
                    )

                except Exception as exc:
                    log.warning(f"[new mirror probe latest failed] route={route['name']} msg={getattr(msg, 'id', None)}: {exc}")

        except Exception as exc:
            log.warning(
                f"[new mirror probe access failed] route={route.get('name')} "
                f"source={route.get('source_chat')} dest={route.get('dest_chat')}_{route.get('dest_topic')}: "
                f"{type(exc).__name__}: {exc}"
            )

    log.info(f"[new mirror probe done] checked={checked}")



def new_mirror_routes():
    out = []

    for route in ROUTES:
        try:
            source_chat = int(route.get("source_chat"))
            dest_topic = int(route.get("dest_topic"))
        except Exception:
            continue

        if source_chat not in NEW_MIRROR_DEBUG_CHATS:
            continue

        if NEW_MIRROR_DEBUG_DEST_TOPICS and dest_topic not in NEW_MIRROR_DEBUG_DEST_TOPICS:
            continue

        out.append(route)

    return out


def route_poll_key(route):
    return f"{int(route.get('source_chat'))}:{route.get('source_topic')}:{int(route.get('dest_topic'))}"


async def get_route_poll_messages(route, limit):
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
                log.info(
                    f"[new mirror topic poll fetch] route={route['name']} "
                    f"source={chat_id}_{source_topic} limit={limit} count={count}"
                )
                return msgs
        except Exception as exc:
            log.warning(
                f"[new mirror topic poll fetch failed] route={route.get('name')} "
                f"source={chat_id}_{source_topic}: {type(exc).__name__}: {exc}. Falling back to chat latest."
            )

    return await client.get_messages(chat_id, limit=limit)




async def cleanup_existing_blocked_sender_copies_once():
    """
    Deletes already-mapped destination copies for blocked senders.
    This cleans old copies made before the hard block existed.
    """
    if not BLOCKED_SENDER_CLEANUP_ENABLED:
        log.info("[blocked sender cleanup] disabled")
        return

    checked = 0
    blocked_seen = 0
    deleted_total = 0

    for route in ROUTES:
        try:
            dest_topic = int(route.get("dest_topic"))
        except Exception:
            continue

        if BLOCKED_SENDER_CLEANUP_DEST_TOPICS and dest_topic not in BLOCKED_SENDER_CLEANUP_DEST_TOPICS:
            continue

        try:
            msgs = await get_route_poll_messages(route, BLOCKED_SENDER_CLEANUP_LIMIT)
        except Exception as exc:
            log.warning(
                f"[blocked sender cleanup fetch failed] route={route.get('name')} "
                f"dest={route.get('dest_chat')}_{route.get('dest_topic')}: {type(exc).__name__}: {exc}"
            )
            continue

        for msg in msgs or []:
            checked += 1

            if not should_hard_block_sender(msg, "startup_cleanup_scan", route):
                continue

            blocked_seen += 1

            ids, keys_to_remove = all_existing_destination_ids_for_source(msg, route)

            if not ids:
                log.info(
                    f"[blocked sender cleanup no mapped copy] route={route.get('name')} "
                    f"source_msg={getattr(msg, 'id', None)}"
                )
                continue

            try:
                await client.delete_messages(route["dest_chat"], ids)

                for key in keys_to_remove:
                    message_map.pop(key, None)

                save_map()
                deleted_total += len(ids)

                log.warning(
                    f"[blocked sender cleanup deleted] route={route.get('name')} "
                    f"source_msg={getattr(msg, 'id', None)} dest_ids={ids}"
                )

            except Exception as exc:
                log.warning(
                    f"[blocked sender cleanup delete failed] route={route.get('name')} "
                    f"source_msg={getattr(msg, 'id', None)} dest_ids={ids}: {type(exc).__name__}: {exc}"
                )

    log.info(
        f"[blocked sender cleanup done] checked={checked} "
        f"blocked_seen={blocked_seen} deleted={deleted_total}"
    )


async def forward_polled_new_mirror_message(route, msg, reason):
    chat_id = int(route["source_chat"])
    topic_id = topic_of(msg, chat_id)
    text = text_of(msg)

    if should_hard_block_sender(msg, f"poll_{reason}", route):
        return False

    routes = routes_for(chat_id, topic_id, msg)

    if route not in routes:
        # Keep it safe but still explain what matched.
        log.info(
            f"[new mirror poll route check] expected={route['name']} "
            f"msg={getattr(msg, 'id', None)} topic={topic_id} "
            f"matched_routes={route_debug_names(routes)}"
        )

    forwarded_any = False

    for matched_route in routes:
        if int(matched_route.get("source_chat")) not in NEW_MIRROR_DEBUG_CHATS:
            continue

        try:
            if text and is_recent_duplicate(matched_route, msg, text):
                log.info(
                    f"[new mirror poll duplicate skipped] route={matched_route['name']} "
                    f"msg={getattr(msg, 'id', None)} reason={reason}"
                )
                continue

            if existing_destination_ids(msg, matched_route):
                log.info(
                    f"[new mirror poll already mapped skipped] route={matched_route['name']} "
                    f"msg={getattr(msg, 'id', None)} reason={reason}"
                )
                continue

            sent = await copy_one_with_retry(msg, matched_route, edited=False)

            if not sent:
                log.warning(
                    f"[new mirror poll copy returned none] route={matched_route['name']} "
                    f"msg={getattr(msg, 'id', None)} reason={reason}"
                )
                continue

            remember_dedupe(matched_route, msg, text)

            if text:
                safe_maybe_post_signal(matched_route, msg, text, reason)
                safe_log_stats(matched_route, msg, text, reason)

            log.info(
                f"[new mirror poll copied] route={matched_route['name']} "
                f"source={chat_id}_{topic_id} msg={getattr(msg, 'id', None)} "
                f"dest={matched_route['dest_chat']}_{matched_route['dest_topic']} reason={reason}"
            )

            forwarded_any = True

        except Exception as exc:
            log.exception(
                f"[new mirror poll copy failed] route={matched_route['name']} "
                f"source={chat_id}_{topic_id} msg={getattr(msg, 'id', None)} reason={reason}: {exc}"
            )

    return forwarded_any


async def initialise_new_mirror_poll_state():
    """
    Sets latest seen ids and optionally backfills last few messages
    so the user can immediately see the new mirrors working.
    """
    if not NEW_MIRROR_POLLING_ENABLED:
        return

    for route in new_mirror_routes():
        chat_id = int(route["source_chat"])

        try:
            latest = await get_route_poll_messages(route, NEW_MIRROR_BACKFILL_LIMIT)

            if not latest:
                log.warning(f"[new mirror poll init empty] route={route['name']} source={chat_id}")
                continue

            latest_sorted = sorted(latest, key=lambda m: int(getattr(m, "id", 0) or 0))

            do_backfill = (
                NEW_MIRROR_BACKFILL_ON_START
                and (
                    not NEW_MIRROR_BACKFILL_ONLY_CHATS
                    or chat_id in NEW_MIRROR_BACKFILL_ONLY_CHATS
                )
                and (
                    not NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS
                    or int(route.get("dest_topic")) in NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS
                )
            )

            if do_backfill:
                for msg in latest_sorted:
                    await forward_polled_new_mirror_message(route, msg, "startup_backfill")

            max_id = max(int(getattr(m, "id", 0) or 0) for m in latest_sorted)
            new_mirror_poll_last_ids[route_poll_key(route)] = max_id

            log.info(
                f"[new mirror poll init] route={route['name']} "
                f"source={chat_id} last_id={max_id} backfill={do_backfill}"
            )

        except Exception as exc:
            log.warning(
                f"[new mirror poll init failed] route={route.get('name')} source={chat_id}: "
                f"{type(exc).__name__}: {exc}"
            )


async def new_mirror_poll_loop():
    if not NEW_MIRROR_POLLING_ENABLED:
        return

    await initialise_new_mirror_poll_state()

    if NEW_MIRROR_POLL_SECONDS <= 0:
        return

    while True:
        await asyncio.sleep(NEW_MIRROR_POLL_SECONDS)

        for route in new_mirror_routes():
            chat_id = int(route["source_chat"])
            last_id = int(new_mirror_poll_last_ids.get(route_poll_key(route), 0) or 0)

            try:
                msgs = await get_route_poll_messages(route, NEW_MIRROR_POLL_LIMIT)

                if not msgs:
                    continue

                fresh = [
                    m for m in msgs
                    if int(getattr(m, "id", 0) or 0) > last_id
                ]

                fresh.sort(key=lambda m: int(getattr(m, "id", 0) or 0))

                for msg in fresh:
                    await forward_polled_new_mirror_message(route, msg, "poll_new")

                max_seen = max(int(getattr(m, "id", 0) or 0) for m in msgs)

                if max_seen > last_id:
                    new_mirror_poll_last_ids[route_poll_key(route)] = max_seen
                    log.info(
                        f"[new mirror poll heartbeat] route={route['name']} "
                        f"source={chat_id} last_id={max_seen} fresh={len(fresh)}"
                    )

            except Exception as exc:
                log.warning(
                    f"[new mirror poll loop failed] route={route.get('name')} source={chat_id}: "
                    f"{type(exc).__name__}: {exc}"
                )



async def handle_single_message(event, edited=False):
    message = event.message
    chat_id = event.chat_id
    topic_id = topic_of(message, chat_id)
    text = text_of(message)

    if should_hard_block_sender(message, "live_handler"):
        return

    if is_promo_text(text, topic_id):
        log.info(f"[promo blocked incoming] msg={getattr(message, 'id', None)} topic={topic_id}")
        return

    if getattr(message, "grouped_id", None) and not PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER and not edited:
        return

    routes = routes_for(chat_id, topic_id, message)

    if int(chat_id) in NEW_MIRROR_DEBUG_CHATS:
        log.info(
            f"[new mirror incoming] chat={chat_id} topic={topic_id} "
            f"msg={getattr(message, 'id', None)} edited={edited} "
            f"media={is_real_media(message)} text={text[:160]!r} "
            f"matched_routes={route_debug_names(routes)}"
        )

    if not routes:
        known = sorted(known_source_topics_for_chat(chat_id))
        log.warning(
            f"[no route] source={chat_id}_{topic_id} msg={getattr(message, 'id', None)} "
            f"grouped={getattr(message, 'grouped_id', None)} media={is_real_media(message)} "
            f"reply_ids={reply_source_ids(message)} known_topics={known[:40]} "
            f"text={text[:NO_ROUTE_TEXT_LIMIT]!r}"
        )
        return

    for route in routes:
        try:
            if not edited and is_recent_duplicate(route, message, text):
                log.info(f"[duplicate skipped] {route['name']} source={chat_id}_{topic_id} msg={message.id}")
                continue

            sent = await copy_one_with_retry(message, route, edited=edited)

            if not sent:
                log.warning(f"[copy returned none] {route['name']} source={chat_id}_{topic_id} msg={getattr(message, 'id', None)}")
                continue

            remember_dedupe(route, message, text)

            if text:
                safe_maybe_post_signal(route, message, text, 'live')
                safe_log_stats(route, message, text, 'live')

            direction = "outgoing" if getattr(message, "out", False) else "incoming"
            edit_tag = ":edited" if edited else ""
            log.info(f"[copied{edit_tag}:{direction}] {route['name']} source={chat_id}_{topic_id} -> dest={route['dest_chat']}_{route['dest_topic']}")

        except FloodWaitError as exc:
            log.warning(f"FloodWait {exc.seconds}s")
            await asyncio.sleep(min(exc.seconds + 1, 60))

        except Exception as exc:
            log.exception(f"[copy failed] {route['name']} source={chat_id}_{topic_id} msg={getattr(message, 'id', None)}: {exc}")


@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def on_message(event):
    try:
        await handle_single_message(event, edited=False)
    except Exception as exc:
        log.exception(f"[handler crash:on_message] chat={getattr(event, 'chat_id', None)}: {exc}")
        alert_crash("imperium-telegram-worker:on_message", exc)


@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def on_message_edited(event):
    if not FORWARD_EDITED_MESSAGES:
        return
    try:
        await handle_single_message(event, edited=True)
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


@client.on(events.Album(chats=SOURCE_CHATS))
async def on_album(event):
    try:
        if not ENABLE_ALBUM_HANDLER:
            return
        if not event.messages:
            return

        first = event.messages[0]

        if any(is_blocked_sender(m) for m in event.messages):
            ids = sorted(set().union(*(sender_ids_for_message(m) for m in event.messages)))
            log.warning(f"[blocked sender album] ids={ids} first_msg={getattr(first, 'id', None)}")
            return

        chat_id = event.chat_id
        topic_id = topic_of(first, chat_id)
        routes = routes_for(chat_id, topic_id, first)

        if not routes:
            known = sorted(known_source_topics_for_chat(chat_id))
            log.warning(
                f"[album no route] source={chat_id}_{topic_id} first_msg={getattr(first, 'id', None)} "
                f"items={len(event.messages)} known_topics={known[:40]}"
            )
            return

        text = ""
        for msg in event.messages:
            text = text_of(msg)
            if text:
                break

        for route in routes:
            try:
                if text and is_recent_duplicate(route, first, text):
                    log.info(f"[album duplicate skipped] {route['name']} source={chat_id}_{topic_id} items={len(event.messages)}")
                    continue

                sent = await copy_album_with_retry(event.messages, route)

                if not sent:
                    log.warning(f"[album copy returned none] {route['name']} source={chat_id}_{topic_id} items={len(event.messages)}")
                    continue

                if text:
                    remember_dedupe(route, first, text)
                    safe_maybe_post_signal(route, first, text, 'album')
                    safe_log_stats(route, first, text, 'album')

                direction = "outgoing" if getattr(first, "out", False) else "incoming"
                log.info(f"[album copied:{direction}] {route['name']} items={len(event.messages)}")

            except FloodWaitError as exc:
                log.warning(f"FloodWait album {exc.seconds}s")
                await asyncio.sleep(min(exc.seconds + 1, 60))

            except Exception as exc:
                log.exception(f"[album failed] {route['name']} source={chat_id}_{topic_id} first_msg={getattr(first, 'id', None)}: {exc}")

    except Exception as exc:
        log.exception(f"[handler crash:on_album] chat={getattr(event, 'chat_id', None)}: {exc}")
        alert_crash("imperium-telegram-worker:on_album", exc)



def available_topic_functions():
    try:
        names = dir(functions.channels)
        return sorted(x for x in names if "Forum" in x or "Topic" in x)
    except Exception:
        return []


def channel_function(name):
    return getattr(functions.channels, name, None)


def log_topic_function_support():
    available = available_topic_functions()
    log.info(f"TELETHON_VERSION={getattr(telethon, '__version__', 'unknown')}")
    log.info(f"Topic function support: {available}")

    if not channel_function("EditForumTopicRequest"):
        log.warning(
            "[topic title auto-sync unsupported] Telethon runtime has no EditForumTopicRequest. "
            "Checker will still detect mismatches and print exact rename instructions."
        )


# STRICT EXACT MIRROR TITLE CHECKER V2

def route_identity(route):
    return f"{route.get('source_chat')}:{route.get('source_topic')}:{route.get('dest_chat')}:{route.get('dest_topic')}"


def route_destination_identity(route):
    return f"{route.get('dest_chat')}:{route.get('dest_topic')}"


def dest_route_count(route):
    dest = route_destination_identity(route)
    return sum(1 for r in ROUTES if route_destination_identity(r) == dest)


def title_clean_exact(value):
    return (value or "").strip()


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
            res = await client(
                req_cls(
                    channel=int(chat_id),
                    topics=[int(topic_id)],
                )
            )

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


async def source_title_for_route(route):
    if route.get("source_topic") is not None:
        topic_title = await get_forum_topic_title(route["source_chat"], route["source_topic"])
        if topic_title:
            return topic_title

    entity = await client.get_entity(route["source_chat"])
    return title_from_entity(entity)


async def dest_title_for_route(route):
    return await get_forum_topic_title(route["dest_chat"], route["dest_topic"])


async def sync_destination_topic_title(route, wanted_title):
    try:
        req_cls = channel_function("EditForumTopicRequest")

        if not req_cls:
            log.warning(
                f"[route title manual rename needed] route={route['name']} "
                f"dest={route['dest_chat']}_{route['dest_topic']} exact_title={wanted_title!r}"
            )
            return False

        await client(
            req_cls(
                channel=int(route["dest_chat"]),
                topic_id=int(route["dest_topic"]),
                title=str(wanted_title),
            )
        )
        log.info(
            f"[route title auto-sync requested] route={route['name']} "
            f"dest={route['dest_chat']}_{route['dest_topic']} title={wanted_title!r}"
        )
        await asyncio.sleep(1.2)
        return True

    except Exception as exc:
        log.warning(
            f"[route title auto-sync failed] route={route['name']} "
            f"dest={route['dest_chat']}_{route['dest_topic']} title={wanted_title!r}: {type(exc).__name__}: {exc}"
        )
        return False


async def verify_route_title(route):
    """
    Exact checker:
    - source group/topic title must equal destination topic title exactly
    - if destination topic is dedicated to one route, auto-renames it
    - if destination topic is shared by multiple routes, logs warning and does NOT auto-rename
    """
    rid = route_identity(route)

    try:
        source_title = title_clean_exact(await source_title_for_route(route))
        dest_title = title_clean_exact(await dest_title_for_route(route))

        if not source_title:
            route_title_status[rid] = False
            log.warning(f"[route title exact failed] route={route['name']} missing source title")
            return False

        if not dest_title:
            route_title_status[rid] = False
            log.warning(f"[route title exact failed] route={route['name']} missing destination topic title")
            return False

        if source_title == dest_title:
            route_title_status[rid] = True
            log.info(
                f"[route title exact ok] route={route['name']} "
                f"title={source_title!r} dest={route['dest_chat']}_{route['dest_topic']}"
            )
            return True

        shared_count = dest_route_count(route)

        if shared_count > 1:
            route_title_status[rid] = False
            log.warning(
                f"[route title shared-dest mismatch] route={route['name']} "
                f"source_title={source_title!r} dest_topic_title={dest_title!r} "
                f"shared_routes={shared_count}. Auto-sync skipped to avoid renaming a shared topic. "
                f"Manual decision needed for dest={route['dest_chat']}_{route['dest_topic']}."
            )
            return False

        log.warning(
            f"[route title exact mismatch] route={route['name']} "
            f"source_title={source_title!r} dest_topic_title={dest_title!r}"
        )

        if AUTO_SYNC_TOPIC_TITLES:
            synced = await sync_destination_topic_title(route, source_title)

            if synced:
                refreshed = title_clean_exact(await dest_title_for_route(route))

                if refreshed == source_title:
                    route_title_status[rid] = True
                    log.info(
                        f"[route title auto-sync ok] route={route['name']} "
                        f"title={source_title!r}"
                    )
                    return True

                route_title_status[rid] = False
                log.warning(
                    f"[route title auto-sync still mismatch] route={route['name']} "
                    f"wanted={source_title!r} got={refreshed!r}"
                )
                return False

        route_title_status[rid] = False
        return False

    except Exception as exc:
        route_title_status[rid] = False
        log.warning(f"[route title exact check failed] route={route.get('name')}: {type(exc).__name__}: {exc}")
        return False


async def verify_route_titles_once():
    if not VERIFY_ROUTE_TITLES:
        return

    checked = 0
    ok = 0
    failed = 0

    for route in ROUTES:
        if not route.get("verify_title"):
            continue

        checked += 1

        if await verify_route_title(route):
            ok += 1
        else:
            failed += 1

    log.info(f"[route title checker done] checked={checked} exact_ok={ok} failed={failed}")


async def route_title_checker_loop():
    if not VERIFY_ROUTE_TITLES:
        return

    if ROUTE_TITLE_CHECK_INTERVAL_SECONDS <= 0:
        return

    while True:
        await asyncio.sleep(ROUTE_TITLE_CHECK_INTERVAL_SECONDS)
        try:
            await verify_route_titles_once()
        except Exception as exc:
            log.warning(f"[route title checker loop failed] {type(exc).__name__}: {exc}")



async def main():
    await start_runtime_guard("imperium-telegram-worker", log)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"SERVER_URL={SERVER_URL}")
    log.info(f"DATA_DIR={DATA_DIR}")
    log.info(f"DRY_RUN={DRY_RUN}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats: {SOURCE_CHATS}")
    log.info(f"Loaded {len(ROUTES)} routes")
    log.info(f"FORWARD_EDITED_MESSAGES={FORWARD_EDITED_MESSAGES}")
    log.info(f"DEDUP_WINDOW_SECONDS={DEDUP_WINDOW_SECONDS}")
    log.info(f"CROSS_SOURCE_DEDUP_DEST_TOPICS={sorted(CROSS_SOURCE_DEDUP_DEST_TOPICS)}")
    log.info(f"BLOCKED_DEST_CHAT={BLOCKED_DEST_CHAT} BLOCKED_DEST_TOPICS={sorted(BLOCKED_DEST_TOPICS)}")
    log.info(f"BLOCKED_SENDER_IDS={sorted(BLOCKED_SENDER_IDS)}")
    log.info(f"PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER={PROCESS_GROUPED_MESSAGES_IN_NEW_HANDLER}")
    log.info(f"ENABLE_ALBUM_HANDLER={ENABLE_ALBUM_HANDLER}")
    log.info(f"ENABLE_ROUTE_FALLBACK_ALL_MESSAGES={ENABLE_ROUTE_FALLBACK_ALL_MESSAGES}")
    log.info(f"ENABLE_ALBUM_REUPLOAD_FALLBACK={ENABLE_ALBUM_REUPLOAD_FALLBACK}")
    log.info(f"COPY_RETRY_ATTEMPTS={COPY_RETRY_ATTEMPTS}")
    log.info(f"COPY_RETRY_SLEEP_CAP_SECONDS={COPY_RETRY_SLEEP_CAP_SECONDS}")
    log.info("Exact media/reply copy hardening active: True")
    log.info("Strong edited-message cleanup active: True")
    log.info(f"MIRROR_DELETED_MESSAGES={MIRROR_DELETED_MESSAGES}")
    log.info(f"VERIFY_ROUTE_TITLES={VERIFY_ROUTE_TITLES}")
    log.info(f"AUTO_SYNC_TOPIC_TITLES={AUTO_SYNC_TOPIC_TITLES}")
    log.info(f"STRICT_ROUTE_TITLE_CHECK={STRICT_ROUTE_TITLE_CHECK}")
    log.info(f"MIRROR_STRUCTURE_REPAIR={MIRROR_STRUCTURE_REPAIR}")
    log.info("Deleted-message mirror active: True")
    log.info("Strict exact route title checker active: True")
    log.info("Mirror structure repair active: True")
    log.info(f"NEW_MIRROR_DEBUG_CHATS={sorted(NEW_MIRROR_DEBUG_CHATS)}")
    log.info(f"Hard blocked sender IDs active: {sorted(BLOCKED_SENDER_IDS)}")
    log.info(f"BLOCKED_SENDER_CLEANUP_ENABLED={BLOCKED_SENDER_CLEANUP_ENABLED}")
    log.info(f"BLOCKED_SENDER_CLEANUP_DEST_TOPICS={sorted(BLOCKED_SENDER_CLEANUP_DEST_TOPICS)}")
    log.info(f"NEW_MIRROR_DEBUG_DEST_TOPICS={sorted(NEW_MIRROR_DEBUG_DEST_TOPICS)}")
    log.info(f"NEW_MIRROR_STARTUP_PROBE={NEW_MIRROR_STARTUP_PROBE}")
    log.info("New mirror forwarding debug active: True")
    log.info("Topic 28464 mirror active: True")
    log.info("Topic 28840 mirror active: True")
    log.info(f"NEW_MIRROR_POLLING_ENABLED={NEW_MIRROR_POLLING_ENABLED}")
    log.info(f"NEW_MIRROR_POLL_SECONDS={NEW_MIRROR_POLL_SECONDS}")
    log.info(f"NEW_MIRROR_BACKFILL_ON_START={NEW_MIRROR_BACKFILL_ON_START}")
    log.info(f"NEW_MIRROR_BACKFILL_ONLY_CHATS={sorted(NEW_MIRROR_BACKFILL_ONLY_CHATS)}")
    log.info(f"NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS={sorted(NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS)}")
    log.info("New mirror polling backup active: True")
    log.info("Forward sidecar isolation active: True")
    await verify_route_titles_once()
    await probe_new_mirror_routes_once()
    asyncio.create_task(route_title_checker_loop())
    await cleanup_existing_blocked_sender_copies_once()
    asyncio.create_task(new_mirror_poll_loop())
    log.info("Imperium fixed Telegram worker running...")
    asyncio.create_task(stats.loop(client))
    log.info("Weekly stats reporter running for Sunday 00:00 Europe/Malta")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log.exception(f"[worker fatal crash] {type(exc).__name__}: {exc}")
        alert_crash("imperium-telegram-worker:fatal", exc)
        raise




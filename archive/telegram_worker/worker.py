import os
import json
import base64
import asyncio
import logging
from pathlib import Path

import requests
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage
from telethon.errors import FloodWaitError

from telegram_worker.routes import ROUTES
from telegram_worker.parser import parse_signal


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

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_BASE = DATA_DIR / "session"
SESSION_FILE = DATA_DIR / "session.session"
MESSAGE_MAP_FILE = DATA_DIR / "message_map.json"

SOURCE_CHATS = sorted(set(route["source_chat"] for route in ROUTES))
POSTED_SIGNAL_KEYS = set()


def restore_session_from_chunks():
    if SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 0:
        log.info(f"Telegram session already exists: {SESSION_FILE}")
        return

    b64 = os.environ.get("SESSION_B64", "").strip()

    if not b64:
        chunks = []
        i = 1
        while True:
            part = os.environ.get(f"SESSION_B64_{i}", "").strip()
            if not part:
                break
            chunks.append(part)
            i += 1

        if chunks:
            b64 = "".join(chunks)
            log.info(f"Loaded Telegram session from {len(chunks)} SESSION_B64 chunks")

    if not b64:
        raise RuntimeError("No Telegram session found. Set SESSION_B64_1, SESSION_B64_2, etc. in Railway variables.")

    try:
        raw = base64.b64decode(b64)
        SESSION_FILE.write_bytes(raw)
        log.info(f"Session restored to {SESSION_FILE} | bytes={len(raw)}")
    except Exception as e:
        raise RuntimeError(f"Failed to decode SESSION_B64 chunks: {e}")


def load_message_map():
    if MESSAGE_MAP_FILE.exists():
        try:
            return json.loads(MESSAGE_MAP_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_message_map():
    tmp = MESSAGE_MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(message_map), encoding="utf-8")
    tmp.replace(MESSAGE_MAP_FILE)


message_map = load_message_map()


client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)


def get_text(message):
    # message.message keeps the exact Telegram text/caption body used by entity offsets.
    return message.message or message.raw_text or message.text or ""


def get_entities(message):
    # Mandatory for keeping Telegram premium/custom animated emoji, spoilers, bold, italic, links, etc.
    # Custom emoji can only remain animated if Telegram provides the custom-emoji entity and the sending account can use it.
    return getattr(message, "entities", None) or []


def get_topic_id(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def is_exact_self_route(route, topic_id):
    return (
        route["source_chat"] == route["dest_chat"]
        and route.get("source_topic") == route.get("dest_topic")
        and topic_id == route.get("dest_topic")
    )


def matching_routes(chat_id, topic_id):
    out = []
    for route in ROUTES:
        if route["source_chat"] != chat_id:
            continue
        if route["source_topic"] is not None and route["source_topic"] != topic_id:
            continue
        if is_exact_self_route(route, topic_id):
            log.warning(
                f"[self-route skipped] {route['name']} source and destination are the same: "
                f"{route['source_chat']}_{topic_id}"
            )
            continue
        out.append(route)
    return out


def has_real_media(message):
    if not getattr(message, "media", None):
        return False
    if isinstance(message.media, MessageMediaWebPage):
        return False
    return True


def map_key(source_chat, source_msg_id, dest_chat, dest_topic):
    return f"{source_chat}:{source_msg_id}:{dest_chat}:{dest_topic}"


def get_reply_to(message, route):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return route["dest_topic"]

    reply_msg_id = getattr(reply, "reply_to_msg_id", None)
    if not reply_msg_id:
        return route["dest_topic"]

    if route["source_topic"] is not None and reply_msg_id == route["source_topic"]:
        return route["dest_topic"]

    key = map_key(route["source_chat"], reply_msg_id, route["dest_chat"], route["dest_topic"])
    return int(message_map.get(key, route["dest_topic"]))


def store_mapping(src_msg, dst_msg, route):
    if not src_msg or not dst_msg:
        return

    key = map_key(route["source_chat"], src_msg.id, route["dest_chat"], route["dest_topic"])
    message_map[key] = dst_msg.id
    save_message_map()


def post_signal(route, message, text):
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
        r = requests.post(
            f"{SERVER_URL}/api/v1/signals",
            json=payload,
            headers={"X-AUTO-TOKEN": AUTO_TOKEN},
            timeout=12,
        )

        if r.status_code >= 400:
            log.warning(f"[signal rejected] {route['name']} {r.status_code}: {r.text}")
        else:
            log.info(f"[signal posted] {route['name']} {parsed['direction']} {parsed['symbol']} -> {r.text}")

    except Exception as e:
        log.error(f"[signal post failed] {route['name']}: {e}")


async def send_single(message, route):
    reply_to = get_reply_to(message, route)
    text = get_text(message)
    entities = get_entities(message)

    if DRY_RUN:
        log.info(f"[DRY_RUN copy] {route['name']}")
        return None

    if has_real_media(message):
        sent = await client.send_file(
            route["dest_chat"],
            message.media,
            caption=text if text else None,
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=reply_to,
        )
    else:
        sent = await client.send_message(
            route["dest_chat"],
            text if text else "Unsupported message type.",
            formatting_entities=entities if text else None,
            parse_mode=None,
            reply_to=reply_to,
            link_preview=True,
        )

    store_mapping(message, sent, route)
    return sent


async def send_album(messages, route):
    first = messages[0]
    reply_to = get_reply_to(first, route)

    files = []
    caption = None
    caption_entities = None

    for msg in messages:
        if has_real_media(msg):
            files.append(msg.media)
        if caption is None:
            text = get_text(msg)
            if text:
                caption = text
                caption_entities = get_entities(msg)

    if DRY_RUN:
        log.info(f"[DRY_RUN album] {route['name']} items={len(files)}")
        return

    if files:
        sent = await client.send_file(
            route["dest_chat"],
            files,
            caption=caption,
            formatting_entities=caption_entities,
            parse_mode=None,
            reply_to=reply_to,
        )

        if isinstance(sent, list):
            for src, dst in zip(messages, sent):
                store_mapping(src, dst, route)
        else:
            store_mapping(first, sent, route)


@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def on_message(event):
    # No incoming=True filter: we must also catch messages sent by this same account in owned/VIP source groups.
    message = event.message

    if getattr(message, "grouped_id", None):
        return

    chat_id = event.chat_id
    topic_id = get_topic_id(message)
    routes = matching_routes(chat_id, topic_id)

    if not routes:
        return

    text = get_text(message)

    for route in routes:
        try:
            await send_single(message, route)
            if text:
                post_signal(route, message, text)

            direction = "outgoing" if getattr(message, "out", False) else "incoming"
            log.info(
                f"[copied:{direction}] {route['name']} "
                f"source={chat_id}_{topic_id} -> dest={route['dest_chat']}_{route['dest_topic']}"
            )

        except FloodWaitError as e:
            log.warning(f"FloodWait {e.seconds}s")
            await asyncio.sleep(min(e.seconds + 1, 60))

        except Exception as e:
            log.error(f"[copy failed] {route['name']}: {e}")


@client.on(events.Album(chats=SOURCE_CHATS))
async def on_album(event):
    if not event.messages:
        return

    first = event.messages[0]
    chat_id = event.chat_id
    topic_id = get_topic_id(first)
    routes = matching_routes(chat_id, topic_id)

    if not routes:
        return

    text = ""
    for msg in event.messages:
        text = get_text(msg)
        if text:
            break

    for route in routes:
        try:
            await send_album(event.messages, route)
            if text:
                post_signal(route, first, text)

            direction = "outgoing" if getattr(first, "out", False) else "incoming"
            log.info(f"[album copied:{direction}] {route['name']} items={len(event.messages)}")

        except Exception as e:
            log.error(f"[album failed] {route['name']}: {e}")


async def main():
    restore_session_from_chunks()

    await client.connect()

    authorised = await client.is_user_authorized()
    if not authorised:
        raise RuntimeError("Telegram session restored but is NOT authorised. Recreate session.session locally and regenerate SESSION_B64 chunks.")

    me = await client.get_me()

    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"SERVER_URL={SERVER_URL}")
    log.info(f"DATA_DIR={DATA_DIR}")
    log.info(f"DRY_RUN={DRY_RUN}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats")
    log.info("Imperium final Telegram worker running...")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

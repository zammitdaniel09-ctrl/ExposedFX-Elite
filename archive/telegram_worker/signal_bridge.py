# telegram_bridge/signal_bridge.py
# Reads Telegram source messages, parses actionable signals, sends valid signals to server.

import os
import asyncio
import logging
from pathlib import Path

import requests
from telethon import TelegramClient, events

from sources import SOURCES
from parser import parse_signal


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("signal-bridge")


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SERVER_URL = os.environ.get("SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
AUTO_TOKEN = os.environ.get("AUTO_TOKEN", "change-this-token")

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_PATH = str(DATA_DIR / "session")

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

SOURCE_CHATS = sorted(set(s["source_chat"] for s in SOURCES))


def get_message_text(message) -> str:
    if not message:
        return ""
    return message.raw_text or message.text or message.message or ""


def get_message_topic_id(message):
    reply = getattr(message, "reply_to", None)
    if not reply:
        return None

    return (
        getattr(reply, "reply_to_top_id", None)
        or getattr(reply, "reply_to_msg_id", None)
    )


def matching_sources(chat_id: int, topic_id: int):
    matches = []
    for src in SOURCES:
        if src["source_chat"] != chat_id:
            continue
        if src["source_topic"] is None:
            matches.append(src)
            continue
        if src["source_topic"] == topic_id:
            matches.append(src)
    return matches


def post_signal(payload):
    url = f"{SERVER_URL}/api/v1/signals"
    r = requests.post(
        url,
        json=payload,
        headers={"X-AUTO-TOKEN": AUTO_TOKEN},
        timeout=10,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()


@client.on(events.NewMessage(chats=SOURCE_CHATS, incoming=True))
async def on_message(event):
    chat_id = event.chat_id
    topic_id = get_message_topic_id(event.message)
    sources = matching_sources(chat_id, topic_id)

    if not sources:
        return

    text = get_message_text(event.message)
    parsed = parse_signal(text)

    if not parsed:
        log.info(f"[ignored-not-actionable] source={chat_id}_{topic_id} msg={event.message.id}")
        return

    for src in sources:
        payload = {
            "source": src["name"],
            "source_chat_id": chat_id,
            "source_message_id": event.message.id,
            "raw_text": text,
            **parsed,
        }

        try:
            res = post_signal(payload)
            log.info(f"[signal-posted] {src['name']} id={res.get('id')} {parsed['direction']} {parsed['symbol']}")
        except Exception as e:
            log.error(f"[post-failed] {src['name']} msg={event.message.id}: {e}")


async def main():
    await client.start()
    me = await client.get_me()

    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"SERVER_URL={SERVER_URL}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats")
    log.info("Signal bridge running...")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

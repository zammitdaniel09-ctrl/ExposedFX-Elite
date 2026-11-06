"""Read-only startup log of every forwarding destination by its exact Telegram name.

Owner request 3 Nov 2026: list the groups/channels and their topics so routes
can be kept or stopped by name. Reads titles only; never sends or edits.
"""

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("destination-directory")

# Destinations written outside routes.py.
EXTRA_ACTIVE = ((-1003743381585, None, "SamGtrades whole-chat relay"),)
# Relay-group topics the ALT account writes into; MAIN reads them as sources.
ALT_RELAY_CHAT = -1004367822325
ALT_RELAY_TOPICS = (106, 2, 508, 115, 2583, 2585, 7248, 7250)
PAUSED_ARCHIVE = Path(__file__).resolve().parents[1] / "docs" / "paused_routes_20261103.md"
STATE_FILE = "destination_directory_v1.json"


def _paused_routes():
    try:
        text = PAUSED_ARCHIVE.read_text(encoding="utf-8")
        block = re.search(r"```json\n(.*?)\n```", text, re.S)
        return json.loads(block.group(1)) if block else []
    except Exception:
        return []


def _directory(worker):
    entries = {}

    def add(status, chat, topic, label):
        key = (status, int(chat), int(topic) if topic else None)
        entries.setdefault(key, []).append(label)

    for route in worker.ROUTES:
        add("active", route["dest_chat"], route.get("dest_topic"), route.get("name"))
    for chat, topic, label in EXTRA_ACTIVE:
        add("active", chat, topic, label)
    for topic in ALT_RELAY_TOPICS:
        add("alt-relay", ALT_RELAY_CHAT, topic, "ALT account writes here")
    for route in _paused_routes():
        add("paused", route["dest_chat"], route.get("dest_topic"), route.get("name"))
    return entries


async def _topic_titles(worker, chat, topics):
    titles = {}
    req_cls = worker.channel_function("GetForumTopicsByIDRequest")
    if req_cls and topics:
        try:
            result = await worker.client(req_cls(channel=int(chat), topics=sorted(topics)))
            for topic in getattr(result, "topics", None) or []:
                if getattr(topic, "title", None):
                    titles[int(topic.id)] = str(topic.title).strip()
        except Exception as exc:
            log.info("[DEST DIRECTORY TOPICS FALLBACK] chat=%s %s", chat, type(exc).__name__)
    for topic in topics:
        if topic not in titles:
            titles[topic] = await worker.get_forum_topic_title(chat, topic)
            await asyncio.sleep(0.2)
    return titles


def _signature(entries):
    return hashlib.sha256(
        json.dumps(sorted([list(k), sorted(map(str, v))] for k, v in entries.items()), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


async def log_destination_directory(worker):
    try:
        entries = _directory(worker)
        # Titles are only re-read when the route set changes (~50 API calls).
        state_path = Path(worker.DATA_DIR) / STATE_FILE
        signature = _signature(entries)
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8")).get("signature")
        except Exception:
            previous = None
        if previous == signature:
            log.warning("[DEST DIRECTORY UNCHANGED] entries=%s API_CALLS=0", len(entries))
            return
        chats = sorted({chat for _, chat, _ in entries})
        for chat in chats:
            try:
                chat_title = worker.title_from_entity(await worker.client.get_entity(chat))
            except Exception as exc:
                chat_title = f"<unavailable: {type(exc).__name__}>"
            topics = {topic for _, c, topic in entries if c == chat and topic}
            titles = await _topic_titles(worker, chat, topics)
            for status, c, topic in sorted(k for k in entries if k[1] == chat):
                log.warning(
                    "[DEST DIRECTORY] status=%s chat=%s chat_title=%r topic=%s topic_title=%r routes=%s",
                    status,
                    chat,
                    chat_title,
                    topic,
                    titles.get(topic, "") if topic else "",
                    " | ".join(sorted(set(str(x) for x in entries[(status, c, topic)]))),
                )
            await asyncio.sleep(0.3)
        log.warning("[DEST DIRECTORY DONE] chats=%s entries=%s", len(chats), len(entries))
        state_path.write_text(json.dumps({"signature": signature}), encoding="utf-8")
    except Exception as exc:
        log.warning("[DEST DIRECTORY FAILED SAFE] %s: %s", type(exc).__name__, exc)

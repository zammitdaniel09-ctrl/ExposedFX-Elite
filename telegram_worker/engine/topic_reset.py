"""One-time reset of a forum topic: delete every post in it and keep the topic.

Owner request 6 Nov 2026: relay topic 8323 and VIP topic 12289 start on a fresh
slate. Each topic is reset once: the result is saved on the volume, so a restart
never deletes again. Only posts whose topic is exactly `topic` are deleted, and
never the topic's own first message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from telethon.errors import FloodWaitError

log = logging.getLogger("engine-topic-reset")
BATCH = 100
FLOOD_ATTEMPTS = 5
Sleep = Callable[[float], Awaitable[Any]]


def topic_of(message: Any) -> Optional[int]:
    reply = getattr(message, "reply_to", None)
    top = getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)
    return int(top) if top else None


def state_path(data_dir: Any, chat: int, topic: int) -> Path:
    return Path(data_dir) / f"topic_reset_{abs(int(chat))}_{int(topic)}_v1.json"


async def _call(call: Callable[[], Awaitable[Any]], sleep: Sleep) -> Any:
    for attempt in range(1, FLOOD_ATTEMPTS + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            if attempt == FLOOD_ATTEMPTS:
                raise
            await sleep(int(exc.seconds) + 1)


async def topic_posts(client: Any, chat: int, topic: int) -> List[int]:
    """Ids of every post in the topic except its first message; a post from elsewhere is never listed."""
    posts = [m async for m in client.iter_messages(chat, reply_to=topic)]
    return sorted(int(m.id) for m in posts if m is not None and int(m.id) != topic and topic_of(m) == topic)


async def _delete(client: Any, chat: int, ids: List[int], sleep: Sleep) -> None:
    await _call(lambda: client.delete_messages(chat, ids), sleep)


async def delete_posts(client: Any, chat: int, ids: List[int], sleep: Sleep) -> List[int]:
    """Deletes the posts; returns the ids Telegram refused to delete."""
    refused: List[int] = []
    for start in range(0, len(ids), BATCH):
        batch = ids[start : start + BATCH]
        try:
            await _delete(client, chat, batch, sleep)
            continue
        except FloodWaitError:
            raise
        except Exception:
            pass  # one post we may not delete fails the batch: retry them one by one
        for one in batch:
            try:
                await _delete(client, chat, [one], sleep)
            except FloodWaitError:
                raise
            except Exception as exc:
                refused.append(one)
                log.warning("[TOPIC RESET REFUSED] chat=%s id=%s %s", chat, one, type(exc).__name__)
    return refused


async def reset_topic_once(
    client: Any, chat: int, topic: int, data_dir: Any, sleep: Sleep = asyncio.sleep
) -> Optional[Dict[str, Any]]:
    path = state_path(data_dir, chat, topic)
    if path.exists():
        log.warning("[TOPIC RESET SKIPPED] chat=%s topic=%s already_done=True", chat, topic)
        return None
    found = await topic_posts(client, chat, topic)
    refused = await delete_posts(client, chat, found, sleep)
    remaining = await topic_posts(client, chat, topic)
    result = {
        "chat": chat,
        "topic": topic,
        "found": len(found),
        "refused": refused,
        "remaining": remaining,
        "at": time.time(),
    }
    path.write_text(json.dumps(result), encoding="utf-8")
    log.warning(
        "[TOPIC RESET DONE] chat=%s topic=%s found=%s deleted=%s remaining=%s",
        chat,
        topic,
        len(found),
        len(found) - len(remaining),
        remaining,
    )
    return result


async def reset_when_ready(
    client: Any,
    chat: int,
    topic: int,
    data_dir: Any,
    attempts: int = 6,
    retry_seconds: float = 10.0,
    sleep: Sleep = asyncio.sleep,
) -> Optional[Dict[str, Any]]:
    """Waits for the connection, then resets the topic; a failure is retried, then reported."""
    while not client.is_connected():
        await sleep(0.25)
    for attempt in range(1, attempts + 1):
        try:
            return await reset_topic_once(client, chat, topic, data_dir, sleep)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning(
                "[TOPIC RESET RETRY] chat=%s topic=%s attempt=%s/%s %s: %s",
                chat,
                topic,
                attempt,
                attempts,
                type(exc).__name__,
                exc,
            )
            await sleep(retry_seconds)
    log.error("[TOPIC RESET FAILED] chat=%s topic=%s attempts=%s", chat, topic, attempts)
    return None

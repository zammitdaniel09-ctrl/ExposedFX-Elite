"""One-time, read-only export of a provider channel for an owner analysis (6 Nov 2026).

Reads the channel's whole history oldest first at a gentle pace (never sends,
edits or deletes). The posts are saved on the volume and printed to the log as
numbered gzip+base64 chunks, so `railway logs` can rebuild the file; the sha256
in "[CHANNEL EXPORT START]" checks it. A state file stops a second run.
"""

import asyncio
import base64
import gzip
import hashlib
import json
import time
from pathlib import Path

from telethon.errors import FloodWaitError

CHANNEL = -1002211486103  # T4T AE Scalping VIP (route "ARABIC VIP EN -> RELAY115")
STATE_FILE = "channel_export_2211486103_v1.json"
EXPORT_FILE = "channel_export_2211486103_v1.json.gz"
SETTLE_SECONDS = 60
PAGE_PAUSE_SECONDS = 1.0
CHUNK = 3000
CHUNK_PAUSE_SECONDS = 0.05


def _ts(value):
    return int(value.timestamp()) if value else None


def row(message):
    reply = getattr(message, "reply_to", None)
    media = getattr(message, "media", None)
    action = getattr(message, "action", None)
    return {
        "id": int(message.id),
        "date": _ts(getattr(message, "date", None)),
        "edit": _ts(getattr(message, "edit_date", None)),
        "text": getattr(message, "message", None) or "",
        "reply": getattr(reply, "reply_to_msg_id", None),
        "group": getattr(message, "grouped_id", None),
        "media": type(media).__name__ if media else None,
        "action": type(action).__name__ if action else None,
        "views": getattr(message, "views", None),
    }


async def read_all(client, log, sleep=asyncio.sleep):
    rows, last_id = [], 0
    while True:
        try:
            async for message in client.iter_messages(
                CHANNEL, reverse=True, min_id=last_id, wait_time=PAGE_PAUSE_SECONDS
            ):
                rows.append(row(message))
                last_id = int(message.id)
            return rows
        except FloodWaitError as exc:
            log.warning("[CHANNEL EXPORT FLOODWAIT] read=%s wait=%ss", len(rows), exc.seconds)
            await sleep(int(exc.seconds) + 1)


async def export_once(client, data_dir, log, sleep=asyncio.sleep):
    state = Path(data_dir) / STATE_FILE
    if state.exists():
        log.warning("[CHANNEL EXPORT SKIPPED] channel=%s already_done=True", CHANNEL)
        return None
    while not client.is_connected():
        await sleep(1)
    await sleep(SETTLE_SECONDS)  # let start-up and live delivery settle first
    started = time.time()
    rows = await read_all(client, log, sleep)
    packed = gzip.compress(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    (Path(data_dir) / EXPORT_FILE).write_bytes(packed)
    blob = base64.b64encode(packed).decode("ascii")
    chunks = [blob[i : i + CHUNK] for i in range(0, len(blob), CHUNK)]
    digest = hashlib.sha256(blob.encode("ascii")).hexdigest()
    log.warning(
        "[CHANNEL EXPORT START] channel=%s posts=%s chunks=%s sha256=%s read_seconds=%s",
        CHANNEL,
        len(rows),
        len(chunks),
        digest,
        int(time.time() - started),
    )
    for number, chunk in enumerate(chunks, start=1):
        log.warning("[CHANNEL EXPORT CHUNK] %s/%s %s", number, len(chunks), chunk)
        await sleep(CHUNK_PAUSE_SECONDS)
    state.write_text(json.dumps({"posts": len(rows), "sha256": digest, "at": time.time()}), encoding="utf-8")
    log.warning("[CHANNEL EXPORT DONE] channel=%s posts=%s chunks=%s", CHANNEL, len(rows), len(chunks))
    return rows


async def export_guarded(client, data_dir, log):
    try:
        await export_once(client, data_dir, log)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[CHANNEL EXPORT FAILED] %s: %s LIVE_FORWARDING_CONTINUES=True", type(exc).__name__, exc)

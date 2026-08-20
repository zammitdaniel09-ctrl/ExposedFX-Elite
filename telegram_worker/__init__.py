"""Small raw-worker-only startup hooks.

This package initializer is intentionally inert for the web, AI formatter,
clean forwarder, scripts, and every process except the raw Telegram worker.
"""

from __future__ import annotations

import asyncio
import io
import json
import mimetypes
import os
import sys
from pathlib import Path


_SOURCE_CHAT = -1002521699926
_DEST_CHAT = -1003852763875
_DEST_TOPIC = 5762
_BACKFILL_COUNT = 20
_BACKFILL_KEY = "2521699926_to_5762_force_v3"


def _is_raw_worker_process() -> bool:
    script = (sys.argv[0] or "").replace("\\", "/").lower()
    return script.endswith("telegram_worker/worker_fixed.py") or script.endswith("telegram_worker/worker.py") or script.endswith("worker_fixed.py") or script.endswith("worker.py")


def _marker_paths():
    data_dir = Path(os.environ.get("DATA_DIR") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    done = data_dir / f"forced_backfill_{_BACKFILL_KEY}.done"
    progress = data_dir / f"forced_backfill_{_BACKFILL_KEY}.json"
    return done, progress


def _load_progress(path: Path) -> set[int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {int(x) for x in raw if int(x) > 0}
    except Exception:
        return set()


def _save_progress(path: Path, ids: set[int]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    tmp.replace(path)


async def _send_copy(client, msg):
    """Copy one Telegram message as the logged-in account, never as a forward."""
    text = getattr(msg, "message", None) or ""
    entities = getattr(msg, "entities", None)
    media = getattr(msg, "media", None)

    if media is not None:
        kwargs = {
            "caption": text or None,
            "reply_to": _DEST_TOPIC,
        }
        if entities:
            kwargs["formatting_entities"] = entities

        try:
            return await client.send_file(_DEST_CHAT, media, **kwargs)
        except Exception:
            # Protected/restricted media may reject direct server-side reuse.
            # Download then re-upload as a fresh KratosFX message.
            raw = await client.download_media(msg, file=bytes)
            if raw is None:
                raise

            stream = io.BytesIO(raw)
            filename = f"telegram_{getattr(msg, 'id', 'media')}"
            document = getattr(msg, "document", None)
            mime = getattr(document, "mime_type", None) if document else None
            suffix = mimetypes.guess_extension(mime or "") or ""
            stream.name = filename + suffix
            stream.seek(0)
            return await client.send_file(_DEST_CHAT, stream, **kwargs)

    if text:
        kwargs = {"reply_to": _DEST_TOPIC}
        if entities:
            kwargs["formatting_entities"] = entities
        return await client.send_message(_DEST_CHAT, text, **kwargs)

    return None


async def _forced_last20_backfill(client) -> None:
    done_path, progress_path = _marker_paths()
    if done_path.exists():
        return

    log = None
    try:
        import logging
        log = logging.getLogger("imperium-worker")
        log.warning(
            "[FORCED LAST20 BEGIN] source=%s dest=%s_%s count=%s sender=KratosFX",
            _SOURCE_CHAT,
            _DEST_CHAT,
            _DEST_TOPIC,
            _BACKFILL_COUNT,
        )

        msgs = await client.get_messages(_SOURCE_CHAT, limit=_BACKFILL_COUNT)
        ordered = sorted(
            list(msgs or []),
            key=lambda m: int(getattr(m, "id", 0) or 0),
        )

        completed = _load_progress(progress_path)
        copied = 0
        skipped = 0

        for msg in ordered:
            mid = int(getattr(msg, "id", 0) or 0)
            if not mid:
                continue
            if mid in completed:
                skipped += 1
                continue

            sent = await _send_copy(client, msg)
            if sent is None:
                log.warning("[FORCED LAST20 EMPTY SKIP] msg=%s", mid)
            else:
                copied += 1
                log.warning(
                    "[FORCED LAST20 COPIED] source_msg=%s dest=%s_%s",
                    mid,
                    _DEST_CHAT,
                    _DEST_TOPIC,
                )

            completed.add(mid)
            _save_progress(progress_path, completed)
            await asyncio.sleep(0.15)

        done_path.write_text(
            f"done fetched={len(ordered)} copied={copied} skipped={skipped}\n",
            encoding="utf-8",
        )
        log.warning(
            "[FORCED LAST20 DONE] fetched=%s copied=%s skipped=%s dest=%s_%s",
            len(ordered),
            copied,
            skipped,
            _DEST_CHAT,
            _DEST_TOPIC,
        )
    except Exception as exc:
        if log is not None:
            log.exception("[FORCED LAST20 FAILED] %s: %s", type(exc).__name__, exc)
        # Deliberately leave the done marker absent so a later restart retries
        # only IDs not already recorded in the progress file.


def _install_raw_worker_hook() -> None:
    if not _is_raw_worker_process():
        return

    try:
        from telethon import TelegramClient
    except Exception:
        return

    original = TelegramClient.run_until_disconnected
    if getattr(original, "_exposedfx_last20_wrapped", False):
        return

    async def wrapped(self, *args, **kwargs):
        # main() reaches run_until_disconnected only after connect/auth. Run the
        # one-time backfill here, then hand control back to Telethon normally.
        await _forced_last20_backfill(self)
        return await original(self, *args, **kwargs)

    wrapped._exposedfx_last20_wrapped = True
    TelegramClient.run_until_disconnected = wrapped


_install_raw_worker_hook()

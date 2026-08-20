"""Raw-worker-only one-time startup repair hooks.

This file is inert for every process except the raw Telegram worker.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_SOURCE_CHAT = -1002521699926
_DEST_CHAT = -1003852763875
_DEST_TOPIC = 5762
_BACKFILL_COUNT = 20
_BACKFILL_KEY = "2521699926_to_5762_force_v4"


def _is_raw_worker_process() -> bool:
    script = (sys.argv[0] or "").replace("\\", "/").lower()
    return script.endswith("worker_fixed.py") or script.endswith("worker.py")


def _paths():
    data_dir = Path(os.environ.get("DATA_DIR") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    return (
        data_dir / f"forced_backfill_{_BACKFILL_KEY}.done",
        data_dir / f"forced_backfill_{_BACKFILL_KEY}.json",
    )


def _load_ids(path: Path) -> set[int]:
    try:
        return {int(x) for x in json.loads(path.read_text(encoding="utf-8"))}
    except Exception:
        return set()


def _save_ids(path: Path, ids: set[int]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(sorted(ids)), encoding="utf-8")
    tmp.replace(path)


async def _forced_last20_backfill(client) -> None:
    done_path, progress_path = _paths()
    if done_path.exists():
        return

    import logging
    log = logging.getLogger("imperium-worker")

    # The normal generic startup backfill was not firing reliably for this
    # route. Disable it for this process so this exact one-time job is the only
    # historical copier and cannot race/duplicate it.
    mainmod = sys.modules.get("__main__")
    if mainmod is not None:
        if hasattr(mainmod, "NEW_MIRROR_BACKFILL_ON_START"):
            mainmod.NEW_MIRROR_BACKFILL_ON_START = False
        if hasattr(mainmod, "NEW_MIRROR_BACKFILL_ALL_ON_START"):
            mainmod.NEW_MIRROR_BACKFILL_ALL_ON_START = False

    try:
        routes = []
        try:
            from telegram_worker.routes import ROUTES
            routes = [
                r for r in ROUTES
                if int(r.get("source_chat", 0)) == _SOURCE_CHAT
                and int(r.get("dest_chat", 0)) == _DEST_CHAT
                and int(r.get("dest_topic", 0)) == _DEST_TOPIC
            ]
        except Exception:
            routes = []

        route = routes[-1] if routes else {
            "name": "Private Live 2521699926 To 5762",
            "source_chat": _SOURCE_CHAT,
            "source_topic": None,
            "dest_chat": _DEST_CHAT,
            "dest_topic": _DEST_TOPIC,
            "verify_title": False,
            "anonymous_send_as": False,
            "live_only": True,
            "copy_reply_parent": False,
        }

        log.warning(
            "[FORCED LAST20 BEGIN] source=%s dest=%s_%s count=%s sender=KratosFX",
            _SOURCE_CHAT, _DEST_CHAT, _DEST_TOPIC, _BACKFILL_COUNT,
        )

        msgs = await client.get_messages(_SOURCE_CHAT, limit=_BACKFILL_COUNT)
        ordered = sorted(list(msgs or []), key=lambda m: int(getattr(m, "id", 0) or 0))
        completed = _load_ids(progress_path)
        copied = 0
        skipped = 0

        worker_copy = getattr(mainmod, "copy_one_with_retry", None) if mainmod else None
        existing_ids = getattr(mainmod, "existing_destination_ids", None) if mainmod else None

        if worker_copy is None:
            raise RuntimeError("raw worker copy_one_with_retry is unavailable")

        for msg in ordered:
            mid = int(getattr(msg, "id", 0) or 0)
            if not mid:
                continue

            if mid in completed:
                skipped += 1
                continue

            if existing_ids is not None and existing_ids(msg, route):
                completed.add(mid)
                _save_ids(progress_path, completed)
                skipped += 1
                log.warning("[FORCED LAST20 ALREADY MAPPED] source_msg=%s", mid)
                continue

            sent = await worker_copy(msg, route, edited=False)
            if not sent:
                raise RuntimeError(f"worker copy returned no destination message for source msg {mid}")

            completed.add(mid)
            _save_ids(progress_path, completed)
            copied += 1
            log.warning(
                "[FORCED LAST20 COPIED] source_msg=%s dest=%s_%s",
                mid, _DEST_CHAT, _DEST_TOPIC,
            )
            await asyncio.sleep(0.15)

        # Mark done only after the whole fetched batch has been handled.
        done_path.write_text(
            f"done fetched={len(ordered)} copied={copied} skipped={skipped}\n",
            encoding="utf-8",
        )
        log.warning(
            "[FORCED LAST20 DONE] fetched=%s copied=%s skipped=%s dest=%s_%s",
            len(ordered), copied, skipped, _DEST_CHAT, _DEST_TOPIC,
        )

    except Exception as exc:
        log.exception("[FORCED LAST20 FAILED] %s: %s", type(exc).__name__, exc)
        # No done marker: the next raw-worker restart retries only IDs not saved
        # in the progress file.


def _install() -> None:
    if not _is_raw_worker_process():
        return

    try:
        from telethon import TelegramClient
    except Exception:
        return

    original = TelegramClient.run_until_disconnected
    if getattr(original, "_exposedfx_forced_last20_v4", False):
        return

    async def wrapped(self, *args, **kwargs):
        await _forced_last20_backfill(self)
        return await original(self, *args, **kwargs)

    wrapped._exposedfx_forced_last20_v4 = True
    TelegramClient.run_until_disconnected = wrapped


_install()

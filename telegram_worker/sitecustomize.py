"""Runtime-only route injection for the raw Telegram worker.

This module is imported automatically by Python's site startup when the raw
worker is executed from telegram_worker/. It is deliberately inert for every
other service/script.
"""

import os
import sys
from pathlib import Path


def _append_csv_int_env(name: str, value: int) -> None:
    raw = (os.environ.get(name) or "").strip()
    parts = [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]
    sval = str(int(value))
    if sval not in parts:
        parts.append(sval)
    os.environ[name] = ",".join(parts)


def _activate_raw_worker_patch() -> None:
    # Ensure the repository root is importable even when Python was launched as
    # `python telegram_worker/worker_fixed.py`.
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from telegram_worker import routes as routes_module

    source_chat = -1002521699926
    dest_chat = -1003852763875
    dest_topic = 5762

    # Remove an accidental duplicate of this exact route, then append one clean
    # definition. anonymous_send_as=False means Telegram sends as KratosFX.
    routes_module.ROUTES[:] = [
        r
        for r in routes_module.ROUTES
        if not (
            int(r.get("source_chat", 0)) == source_chat
            and int(r.get("dest_chat", 0)) == dest_chat
            and int(r.get("dest_topic", 0)) == dest_topic
        )
    ]

    routes_module.ROUTES.append(
        {
            "name": "Private Live 2521699926 To 5762",
            "source_chat": source_chat,
            "source_topic": None,
            "dest_chat": dest_chat,
            "dest_topic": dest_topic,
            "verify_title": False,
            "anonymous_send_as": False,
            "live_only": True,
            "copy_reply_parent": False,
        }
    )

    # Use the worker's existing, proven startup-backfill path for exactly this
    # route. It will fetch the most recent 20 messages, copy them oldest->newest,
    # then continue live polling. Existing message mappings prevent duplicates
    # on a normal restart when DATA_DIR is persistent.
    os.environ["NEW_MIRROR_BACKFILL_ON_START"] = "1"
    os.environ["NEW_MIRROR_BACKFILL_ALL_ON_START"] = "0"
    os.environ["NEW_MIRROR_BACKFILL_LIMIT"] = "20"
    os.environ["NEW_MIRROR_BACKFILL_ONLY_CHATS"] = str(source_chat)
    os.environ["NEW_MIRROR_BACKFILL_ONLY_DEST_TOPICS"] = str(dest_topic)

    # Preserve every currently configured polling route while adding this one.
    _append_csv_int_env("NEW_MIRROR_DEBUG_CHATS", source_chat)
    _append_csv_int_env("NEW_MIRROR_DEBUG_DEST_TOPICS", dest_topic)


_script = (sys.argv[0] or "").replace("\\", "/").lower()
if _script.endswith("telegram_worker/worker_fixed.py") or _script.endswith("worker_fixed.py"):
    _activate_raw_worker_patch()

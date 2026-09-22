"""Stable entrypoint for the ALT worker with reply preservation installed.

The historical ALT worker is kept byte-for-byte as alt_worker_legacy.py. This
wrapper lets Railway keep its existing `python -m telegram_worker.alt_worker`
start command while installing reply hardening before legacy main() begins.

It also applies small runtime configuration migrations before importing the
legacy worker, so Railway's existing ALT_ROUTES_JSON can stay backwards
compatible while a provider source group is swapped without changing any of
the forwarding, dedupe, translation, watchdog, edit, reply or destination
logic.
"""

import asyncio
import json
import os


OLD_PAIR106_SOURCE_CHAT = -1002438454194
NEW_PAIR106_SOURCE_CHAT = -1004347858259

CONJOINED2530_SOURCE_CHAT = -1004349952583
CONJOINED2530_SOURCE_TOPICS = (185, 9)
CONJOINED2530_DEST_CHAT = -1004367822325
CONJOINED2530_DEST_TOPIC = 2530

SPLIT_PAIR_SOURCE_A = -1004347858259
SPLIT_PAIR_SOURCE_B = -1003252087470
SPLIT_PAIR_DEST_CHAT = -1004367822325
SPLIT_PAIR_DEST_A = 2583
SPLIT_PAIR_DEST_B = 2585


def _swap_replaced_source_in_env() -> int:
    """Replace only the old source_chat in ALT_ROUTES_JSON before legacy import."""
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        return 0

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    changed = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            source_chat = int(item.get("source_chat"))
        except Exception:
            continue

        if source_chat == OLD_PAIR106_SOURCE_CHAT:
            item["source_chat"] = NEW_PAIR106_SOURCE_CHAT
            changed += 1

    # Preserve whether the Railway variable was configured as one route object
    # or a route list. The legacy loader accepts either shape.
    migrated = items if isinstance(parsed, list) else items[0]
    os.environ["ALT_ROUTES_JSON"] = json.dumps(
        migrated,
        separators=(",", ":"),
    )
    return changed


_SOURCE_ROUTE_SWAPS = _swap_replaced_source_in_env()


def _replace_pair106_with_split_routes_in_env():
    """Retire the old conjoined pair106 paths and make both sources exclusive.

    The owner requested:
      source A -> relay topic 2583 only
      source B -> relay topic 2585 only
    Therefore every previous ALT route from either source is removed before the
    two exact replacements are added. Unrelated ALT routes remain untouched.
    """
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(items, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a route list/object")

    sources = {SPLIT_PAIR_SOURCE_A, SPLIT_PAIR_SOURCE_B}
    kept = []
    removed = []
    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue
        try:
            source_chat = int(item.get("source_chat", 0))
        except Exception:
            source_chat = 0
        if source_chat in sources:
            removed.append(item)
        else:
            kept.append(item)

    wanted = [
        {
            "name": "Split Source4347858259 To Relay2583",
            "source_chat": SPLIT_PAIR_SOURCE_A,
            "source_topic": None,
            "dest_chat": SPLIT_PAIR_DEST_CHAT,
            "dest_topic": SPLIT_PAIR_DEST_A,
        },
        {
            "name": "Split Source3252087470 To Relay2585",
            "source_chat": SPLIT_PAIR_SOURCE_B,
            "source_topic": None,
            "dest_chat": SPLIT_PAIR_DEST_CHAT,
            "dest_topic": SPLIT_PAIR_DEST_B,
        },
    ]
    kept.extend(wanted)

    os.environ["ALT_ROUTES_JSON"] = json.dumps(kept, separators=(",", ":"))
    return len(removed), [str(item.get("name") or "") for item in removed]


_SPLIT_PAIR_REMOVED, _SPLIT_PAIR_REMOVED_NAMES = _replace_pair106_with_split_routes_in_env()


def _ensure_conjoined2530_routes_in_env() -> int:
    """Add the two owner-requested source topics before legacy import.

    They intentionally share one destination topic so Telegram posts from both
    sources form one combined live stream. Existing unrelated routes are left
    untouched.
    """
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(items, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a route list/object")

    added = 0
    wanted = [
        {
            "name": "Conjoined Topic185 To Relay2530",
            "source_chat": CONJOINED2530_SOURCE_CHAT,
            "source_topic": 185,
            "dest_chat": CONJOINED2530_DEST_CHAT,
            "dest_topic": CONJOINED2530_DEST_TOPIC,
        },
        {
            "name": "Conjoined Topic9 To Relay2530",
            "source_chat": CONJOINED2530_SOURCE_CHAT,
            "source_topic": 9,
            "dest_chat": CONJOINED2530_DEST_CHAT,
            "dest_topic": CONJOINED2530_DEST_TOPIC,
        },
    ]

    for route in wanted:
        exists = any(
            isinstance(item, dict)
            and int(item.get("source_chat", 0)) == int(route["source_chat"])
            and int(item.get("source_topic", 0) or 0) == int(route["source_topic"])
            and int(item.get("dest_chat", 0)) == int(route["dest_chat"])
            and int(item.get("dest_topic", 0)) == int(route["dest_topic"])
            for item in items
        )
        if not exists:
            items.append(route)
            added += 1

    os.environ["ALT_ROUTES_JSON"] = json.dumps(items, separators=(",", ":"))
    return added


_CONJOINED2530_ROUTES_ADDED = _ensure_conjoined2530_routes_in_env()

# Import only AFTER ALT_ROUTES_JSON has been migrated. This is important because
# the legacy module registers Telethon NewMessage handlers at import time using
# SOURCE_CHATS; changing the route after import would leave the event filter on
# the old Telegram group.
from telegram_worker import alt_worker_legacy as _legacy
from telegram_worker.alt_reply_hardening import install_alt_reply_hardening
from telegram_worker.alt_conjoined2530 import run_alt_conjoined2530_last50_once
from telegram_worker.alt_split_pair_last100 import run_alt_split_pair_last100_once


# The old pair106 provider merge has now been retired completely. These two
# sources are independent routes, so pair106 dedupe/baseline logic must not
# capture them.
if hasattr(_legacy, "ALT_PAIR106_SOURCE_CHATS"):
    _legacy.ALT_PAIR106_SOURCE_CHATS = set()

async def _pair106_retired_noop():
    _legacy.log.warning(
        "[ALT PAIR106 RETIRED] old_dest=-1004367822325_106 "
        "replacement_a=-1004367822325_2583 "
        "replacement_b=-1004367822325_2585"
    )
    return True

_legacy.initialise_alt_pair106_no_history = _pair106_retired_noop

# Fail closed if a configured route somehow still points at the retired source.
_retired_routes = [
    route.get("name")
    for route in getattr(_legacy, "ROUTES", [])
    if int(route.get("source_chat", 0)) == OLD_PAIR106_SOURCE_CHAT
]
if _retired_routes:
    raise RuntimeError(
        "Retired Telegram source still present after migration: "
        f"{_retired_routes}"
    )

_legacy.log.warning(
    "[ALT SPLIT PAIR ROUTES READY] "
    f"source_a={SPLIT_PAIR_SOURCE_A} dest_a={SPLIT_PAIR_DEST_CHAT}_{SPLIT_PAIR_DEST_A} "
    f"source_b={SPLIT_PAIR_SOURCE_B} dest_b={SPLIT_PAIR_DEST_CHAT}_{SPLIT_PAIR_DEST_B} "
    f"old_pair_routes_removed={_SPLIT_PAIR_REMOVED} "
    f"removed_names={_SPLIT_PAIR_REMOVED_NAMES} "
    "PAIR106_RETIRED=True EXCLUSIVE_SOURCES=True"
)


install_alt_reply_hardening(_legacy, logger=_legacy.log)

_legacy.log.warning(
    "[ALT CONJOINED2530 ROUTES READY] "
    f"source={CONJOINED2530_SOURCE_CHAT} "
    f"topics={list(CONJOINED2530_SOURCE_TOPICS)} "
    f"dest={CONJOINED2530_DEST_CHAT}_{CONJOINED2530_DEST_TOPIC} "
    f"routes_added={_CONJOINED2530_ROUTES_ADDED} "
    "LIVE_FORWARDING=True"
)

# Keep useful module attributes available for diagnostics/importers.
client = _legacy.client
ROUTES = _legacy.ROUTES
SOURCE_CHATS = _legacy.SOURCE_CHATS


async def _run_split_pair_last100_guarded():
    try:
        await run_alt_split_pair_last100_once(_legacy, logger=_legacy.log)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _legacy.log.exception(
            "[ALT SPLIT LAST100 FAILED SAFE] "
            f"{type(exc).__name__}: {exc} LIVE_FORWARDING_CONTINUES=True"
        )


async def _run_conjoined2530_history_guarded():
    try:
        await run_alt_conjoined2530_last50_once(_legacy, logger=_legacy.log)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _legacy.log.exception(
            "[ALT CONJOINED2530 LAST50 FAILED SAFE] "
            f"{type(exc).__name__}: {exc} LIVE_FORWARDING_CONTINUES=True"
        )


async def main():
    split_history_task = asyncio.create_task(_run_split_pair_last100_guarded())
    conjoined_history_task = asyncio.create_task(_run_conjoined2530_history_guarded())
    try:
        return await _legacy.main()
    finally:
        for task in (split_history_task, conjoined_history_task):
            if not task.done():
                task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

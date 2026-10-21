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

RETIRED2530_CHAT = -1004367822325
RETIRED2530_TOPIC = 2530

NEW_TOPIC_SOURCE_CHAT = -1004349952583
NEW_TOPIC_DEST_CHAT = -1004367822325
NEW_TOPIC_ROUTES = (
    (5246, 7248),
    (5247, 7250),
)

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


def _ensure_split_routes_alongside_pair106_in_env():
    """Keep the existing paired topic106 forwarding AND add split routes.

    Required simultaneous behavior:
      -1004347858259 -> relay106 (paired/deduped) AND relay2583
      -1003252087470 -> relay106 (paired/deduped) AND relay2585

    No existing pair106 route is removed. Unrelated ALT routes are untouched.
    """
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(items, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a route list/object")

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

    added = 0
    for route in wanted:
        exists = any(
            isinstance(item, dict)
            and int(item.get("source_chat", 0)) == int(route["source_chat"])
            and (
                int(item["source_topic"])
                if item.get("source_topic") not in (None, "")
                else None
            ) == route["source_topic"]
            and int(item.get("dest_chat", 0)) == int(route["dest_chat"])
            and int(item.get("dest_topic", 0)) == int(route["dest_topic"])
            for item in items
        )
        if not exists:
            items.append(route)
            added += 1

    os.environ["ALT_ROUTES_JSON"] = json.dumps(items, separators=(",", ":"))
    return added


_SPLIT_PAIR_ROUTES_ADDED = _ensure_split_routes_alongside_pair106_in_env()

def _hard_stop_2530_in_env() -> int:
    """Force-retire relay topic 2530 in both directions before legacy import.

    Any route writing INTO -1004367822325/2530 or reading FROM that exact topic
    is stripped, including stale routes still present in Railway ALT_ROUTES_JSON.
    """
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(items, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a route list/object")

    kept = []
    removed = 0

    for item in items:
        if not isinstance(item, dict):
            kept.append(item)
            continue

        try:
            source_chat = int(item.get("source_chat", 0))
            source_topic = (
                int(item["source_topic"])
                if item.get("source_topic") not in (None, "")
                else None
            )
            dest_chat = int(item.get("dest_chat", 0))
            dest_topic = int(item.get("dest_topic", 0))
        except Exception:
            kept.append(item)
            continue

        retired_source = (
            source_chat == RETIRED2530_CHAT
            and source_topic == RETIRED2530_TOPIC
        )
        retired_dest = (
            dest_chat == RETIRED2530_CHAT
            and dest_topic == RETIRED2530_TOPIC
        )

        if retired_source or retired_dest:
            removed += 1
            continue

        kept.append(item)

    os.environ["ALT_ROUTES_JSON"] = json.dumps(
        kept,
        separators=(",", ":"),
    )
    return removed


_RETIRED2530_ROUTES_REMOVED = _hard_stop_2530_in_env()


def _ensure_new_topic_routes_in_env() -> int:
    """Add the two owner-requested replacement routes on the ALT account."""
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")

    parsed = json.loads(raw)
    items = parsed if isinstance(parsed, list) else [parsed]
    if not isinstance(items, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a route list/object")

    wanted = [
        {
            "name": "NewAccount Topic5246 To Relay7248",
            "source_chat": NEW_TOPIC_SOURCE_CHAT,
            "source_topic": 5246,
            "dest_chat": NEW_TOPIC_DEST_CHAT,
            "dest_topic": 7248,
        },
        {
            "name": "NewAccount Topic5247 To Relay7250",
            "source_chat": NEW_TOPIC_SOURCE_CHAT,
            "source_topic": 5247,
            "dest_chat": NEW_TOPIC_DEST_CHAT,
            "dest_topic": 7250,
        },
    ]

    added = 0
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

    os.environ["ALT_ROUTES_JSON"] = json.dumps(
        items,
        separators=(",", ":"),
    )
    return added


_NEW_TOPIC_ROUTES_ADDED = _ensure_new_topic_routes_in_env()

# Import only AFTER ALT_ROUTES_JSON has been migrated. This is important because
# the legacy module registers Telethon NewMessage handlers at import time using
# SOURCE_CHATS; changing the route after import would leave the event filter on
# the old Telegram group.
from telegram_worker import alt_worker_legacy as _legacy
from telegram_worker.alt_reply_hardening import install_alt_reply_hardening
from telegram_worker.alt_new_topics7248_7250_last20 import (
    run_alt_new_topics_last20_once,
)
from telegram_worker.alt_split_pair_last100 import run_alt_split_pair_last100_once


# Preserve the original paired-provider dedupe/baseline engine while replacing
# the retired old source id with the current source id. The same two providers
# therefore continue feeding relay topic106, first-copy-wins, while ALSO
# feeding their new independent relay2583/relay2585 routes.
if hasattr(_legacy, "ALT_PAIR106_SOURCE_CHATS"):
    _legacy.ALT_PAIR106_SOURCE_CHATS = {
        NEW_PAIR106_SOURCE_CHAT
        if int(chat_id) == OLD_PAIR106_SOURCE_CHAT
        else int(chat_id)
        for chat_id in _legacy.ALT_PAIR106_SOURCE_CHATS
    }

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
    "[ALT PAIR106 + SPLIT ROUTES READY] "
    f"pair106_sources={sorted(getattr(_legacy, 'ALT_PAIR106_SOURCE_CHATS', []))} "
    f"pair106_dest={SPLIT_PAIR_DEST_CHAT}_106 "
    f"source_a={SPLIT_PAIR_SOURCE_A} split_dest_a={SPLIT_PAIR_DEST_CHAT}_{SPLIT_PAIR_DEST_A} "
    f"source_b={SPLIT_PAIR_SOURCE_B} split_dest_b={SPLIT_PAIR_DEST_CHAT}_{SPLIT_PAIR_DEST_B} "
    f"split_routes_added={_SPLIT_PAIR_ROUTES_ADDED} "
    "PAIR106_KEPT=True FIRST_COPY_WINS=True SPLIT_FORWARDING=True"
)


install_alt_reply_hardening(_legacy, logger=_legacy.log)

_retired2530_runtime = [
    route.get("name")
    for route in getattr(_legacy, "ROUTES", [])
    if (
        int(route.get("dest_chat", 0)) == RETIRED2530_CHAT
        and int(route.get("dest_topic", 0)) == RETIRED2530_TOPIC
    )
    or (
        int(route.get("source_chat", 0)) == RETIRED2530_CHAT
        and int(route.get("source_topic", 0) or 0) == RETIRED2530_TOPIC
    )
]
if _retired2530_runtime:
    raise RuntimeError(
        "FORCE STOP 2530 FAILED; routes remain: "
        f"{_retired2530_runtime}"
    )

_legacy.log.error(
    "[ALT TOPIC2530 FORCE STOP ACTIVE] "
    f"chat={RETIRED2530_CHAT} topic={RETIRED2530_TOPIC} "
    f"routes_removed={_RETIRED2530_ROUTES_REMOVED} "
    "INBOUND=False OUTBOUND=False HISTORY_TASK=False"
)

_legacy.log.warning(
    "[ALT NEW ACCOUNT TOPIC ROUTES READY] "
    f"source={NEW_TOPIC_SOURCE_CHAT} "
    "source_topics=[5246,5247] "
    f"dest_chat={NEW_TOPIC_DEST_CHAT} "
    "dest_topics=[7248,7250] "
    f"routes_added={_NEW_TOPIC_ROUTES_ADDED} "
    "LIVE_FORWARDING=True LAST20=True"
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


async def _run_new_topics_last20_guarded():
    try:
        await run_alt_new_topics_last20_once(
            _legacy,
            logger=_legacy.log,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _legacy.log.exception(
            "[ALT NEWTOPICS LAST20 FAILED SAFE] "
            f"{type(exc).__name__}: {exc} "
            "LIVE_FORWARDING_CONTINUES=True"
        )


async def main():
    split_history_task = asyncio.create_task(
        _run_split_pair_last100_guarded()
    )
    new_topics_last20_task = asyncio.create_task(
        _run_new_topics_last20_guarded()
    )
    try:
        return await _legacy.main()
    finally:
        for task in (
            split_history_task,
            new_topics_last20_task,
        ):
            if not task.done():
                task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

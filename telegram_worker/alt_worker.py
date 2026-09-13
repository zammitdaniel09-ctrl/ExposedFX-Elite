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

# Import only AFTER ALT_ROUTES_JSON has been migrated. This is important because
# the legacy module registers Telethon NewMessage handlers at import time using
# SOURCE_CHATS; changing the route after import would leave the event filter on
# the old Telegram group.
from telegram_worker import alt_worker_legacy as _legacy
from telegram_worker.alt_reply_hardening import install_alt_reply_hardening


# The paired-provider dedupe set is a separate hard-coded runtime guard in the
# legacy worker. Swap the same source there while leaving the other provider,
# destination topic and first-copy-wins logic exactly unchanged.
if hasattr(_legacy, "ALT_PAIR106_SOURCE_CHATS"):
    _legacy.ALT_PAIR106_SOURCE_CHATS = {
        NEW_PAIR106_SOURCE_CHAT if int(chat_id) == OLD_PAIR106_SOURCE_CHAT else int(chat_id)
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
    "[ALT SOURCE SWAP ACTIVE] "
    f"old={OLD_PAIR106_SOURCE_CHAT} "
    f"new={NEW_PAIR106_SOURCE_CHAT} "
    f"routes_changed={_SOURCE_ROUTE_SWAPS} "
    f"pair106_sources={sorted(getattr(_legacy, 'ALT_PAIR106_SOURCE_CHATS', []))} "
    "LOGIC_UNCHANGED=True DESTINATION_UNCHANGED=True"
)


install_alt_reply_hardening(_legacy, logger=_legacy.log)

# Keep useful module attributes available for diagnostics/importers.
client = _legacy.client
ROUTES = _legacy.ROUTES
SOURCE_CHATS = _legacy.SOURCE_CHATS


async def main():
    return await _legacy.main()


if __name__ == "__main__":
    asyncio.run(main())

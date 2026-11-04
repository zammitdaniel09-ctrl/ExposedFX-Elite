"""MAIN's live routes (intake and owner paths), from route_table.py.

The worker reads ROUTES from here; route_table.py is the single place where
routes are defined. Two reversible Railway switches still apply:
DISABLE_PROVIDER_ROUTES=1 turns every route off, and PAUSED_DEST_CHATS /
PAUSED_SOURCE_CHATS (comma or space separated peer ids) pause routes by chat.
"""

import os

from telegram_worker.owner_forwarding_policy import known_stopped
from telegram_worker.route_table import routes


def _parse_pause_ids(raw):
    values = set()
    for token in raw.replace(",", " ").split():
        try:
            values.add(int(token))
        except ValueError:
            pass
    return values


PAUSED_DEST_CHATS = _parse_pause_ids(os.environ.get("PAUSED_DEST_CHATS", ""))
PAUSED_SOURCE_CHATS = _parse_pause_ids(os.environ.get("PAUSED_SOURCE_CHATS", ""))

ROUTES = (
    [] if os.environ.get("DISABLE_PROVIDER_ROUTES", "0").strip() == "1" else routes("MAIN", "intake", "owner")
)
ROUTES[:] = [
    route
    for route in ROUTES
    if route["dest_chat"] not in PAUSED_DEST_CHATS
    and route["source_chat"] not in PAUSED_SOURCE_CHATS
    and not known_stopped(route)
]

"""Emergency hard stop for ALL forwarding into private topic 5632.

This runs before worker_fixed.py builds its source-chat list and pollers.
It removes every real route targeting -1003852763875 topic 5632.

The old Hub568 one-message startup helper still exists in worker_fixed.py and
expects its route to exist. To keep the worker from crashing, one inert marker
route is retained only for that helper. The hub source is ignored globally and
the marker route is not live_only, so it cannot forward messages. A persistent
done marker also makes the historical helper return immediately.
"""

import os
from pathlib import Path

_STOP_DEST_CHAT = -1003852763875
_STOP_DEST_TOPIC = 5632
_HUB_SOURCE_CHAT = -1003918958200
_HUB_SOURCE_TOPIC = 568


def _apply_emergency_stop() -> None:
    from telegram_worker import routes as routes_module

    kept = []
    inert_helper_route = None

    for route in routes_module.ROUTES:
        try:
            is_stopped_dest = (
                int(route.get("dest_chat", 0)) == _STOP_DEST_CHAT
                and int(route.get("dest_topic", 0)) == _STOP_DEST_TOPIC
            )
        except Exception:
            is_stopped_dest = False

        if not is_stopped_dest:
            kept.append(route)
            continue

        # Keep only the exact route required by the legacy one-message startup
        # helper so main() does not crash. It is made inert below.
        try:
            is_legacy_helper_route = (
                int(route.get("source_chat", 0)) == _HUB_SOURCE_CHAT
                and int(route.get("source_topic") or 0) == _HUB_SOURCE_TOPIC
            )
        except Exception:
            is_legacy_helper_route = False

        if is_legacy_helper_route and inert_helper_route is None:
            inert_helper_route = dict(route)
            inert_helper_route["live_only"] = False
            inert_helper_route["copy_reply_parent"] = False
            kept.append(inert_helper_route)

    routes_module.ROUTES[:] = kept

    # Ensure the retained helper route can never receive live events or poll.
    ignored_raw = os.environ.get("IGNORED_SOURCE_CHATS", "-1003852763875")
    ignored = {
        item.strip()
        for item in ignored_raw.replace(";", ",").split(",")
        if item.strip()
    }
    ignored.add(str(_HUB_SOURCE_CHAT))
    os.environ["IGNORED_SOURCE_CHATS"] = ",".join(sorted(ignored))

    # Prevent the old one-message historical helper from sending anything.
    data_dir = Path(os.environ.get("DATA_DIR") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / "last1_hub568_to_5632_v1.done"
    marker.write_text("disabled by emergency hard stop for topic 5632\n", encoding="utf-8")


_apply_emergency_stop()

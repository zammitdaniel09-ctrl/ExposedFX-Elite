"""Emergency runtime stop for private destination topic 5632.

This runs when the telegram_worker package is imported by the raw worker.
It removes every route targeting -1003852763875 topic 5632 before
worker_fixed.py imports ROUTES, and marks the old one-message startup backfill
as completed so it cannot send into the stopped topic.
"""

import os
from pathlib import Path

_STOP_DEST_CHAT = -1003852763875
_STOP_DEST_TOPIC = 5632


def _apply_emergency_stop() -> None:
    from telegram_worker import routes as routes_module

    routes_module.ROUTES[:] = [
        route
        for route in routes_module.ROUTES
        if not (
            int(route.get("dest_chat", 0)) == _STOP_DEST_CHAT
            and int(route.get("dest_topic", 0)) == _STOP_DEST_TOPIC
        )
    ]

    # The current worker still calls the old one-message Hub568 startup helper.
    # Creating its done marker makes that helper return immediately, preventing
    # any historical message from being copied into the stopped topic.
    data_dir = Path(os.environ.get("DATA_DIR") or "./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    marker = data_dir / "last1_hub568_to_5632_v1.done"
    marker.write_text("disabled by emergency hard stop\n", encoding="utf-8")


_apply_emergency_stop()

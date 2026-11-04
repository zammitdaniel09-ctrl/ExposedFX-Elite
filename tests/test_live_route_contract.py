"""Live topology safety (4 Nov 2026): every route comes from route_table.py and none
can reach a retired or paused destination. Routes themselves are added or removed by
editing one line in route_table.py; these checks need no update when that happens.
"""

import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_formatted_route as formatted
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import route_table
from telegram_worker import samgtrades_relay as wholechat
from telegram_worker.routes import ROUTES

VIP = -1003726286301

PAUSED_DESTINATIONS = {-1003216105903, -1003918958200, -1004442967052, -1003802436353, -1004365831817}


class LiveRouteContractTests(unittest.TestCase):
    def test_every_main_route_comes_from_the_table(self):
        self.assertEqual(ROUTES, route_table.routes("MAIN", "intake", "owner"))
        self.assertEqual(formatted.ROUTE, route_table.one(formatted.ROUTE["name"]).as_dict())
        sam = route_table.one("SamGtrades To Exposed VIP")
        self.assertEqual((wholechat.SOURCE_CHAT, wholechat.DEST_CHAT), (sam.source_chat, sam.dest_chat))

    def test_retired_destinations_have_no_route(self):
        # Retired 4 Nov 2026 with their one-time guards: partner -1003885074241
        # (topics 4660/4663/4665) and VIP topic 5.
        dests = {(r["dest_chat"], r.get("dest_topic")) for r in ROUTES}
        self.assertFalse({d for d in dests if d[0] == -1003885074241})
        self.assertNotIn((VIP, 5), dests)

    def test_paused_destinations_stay_paused(self):
        self.assertTrue(PAUSED_DESTINATIONS <= set(policy.STOPPED_DEST_CHATS))
        self.assertFalse({r["dest_chat"] for r in ROUTES} & PAUSED_DESTINATIONS)


if __name__ == "__main__":
    unittest.main()

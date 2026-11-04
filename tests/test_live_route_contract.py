"""Live topology contract (4 Nov 2026): any change to what MAIN delivers must be deliberate.

If this fails, a route was added, removed or redirected. Update the contract only
together with the owner's instruction and docs/ARCHITECTURE.md.
"""
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_formatted_route_20261103 as formatted
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import wholechat_relay_4281603170_to_3743381585 as wholechat
from telegram_worker.routes import ROUTES

VIP = -1003726286301
RELAY = -1004367822325
MARKET_MAKERS = -1003786932333

# (source chat, source topic, destination chat, destination topic, owner_managed)
LIVE_ROUTES = {
    (-1003838973021, None, VIP, 9, False),      # Market Slayers VIP -> VIP topic 9
    (RELAY, 106, VIP, 11, False),               # relay AKX -> VIP topic 11
    (RELAY, 2, VIP, 1364, False),               # Private Circle G Trades -> Extra Partner 2
    (RELAY, 508, VIP, 7, False),                # XAU Exclusive Room -> VIP topic 7 (Imperium format)
    (RELAY, 7248, VIP, 10877, False),           # B+SCALPING -> EXTRA PARTNER 1
    (RELAY, 7250, VIP, 12161, True),            # B+XAU -> EXTRA PARTNER [ 4 ]
    (VIP, 1364, MARKET_MAKERS, 28, True),       # -> Sniper Tradez
    (VIP, 10877, MARKET_MAKERS, 8, True),       # -> Exclusive trades
    (VIP, 12161, MARKET_MAKERS, 24, True),      # -> Sniper Trades
}
PAUSED_DESTINATIONS = {-1003216105903, -1003918958200, -1004442967052, -1003802436353, -1004365831817}


class LiveRouteContractTests(unittest.TestCase):
    def test_main_routes_are_exactly_the_live_set(self):
        actual = {(r["source_chat"], r.get("source_topic"), r["dest_chat"], r.get("dest_topic"),
                   bool(r.get("owner_managed"))) for r in ROUTES}
        self.assertEqual(actual, LIVE_ROUTES)

    def test_side_routes_outside_routes_py(self):
        self.assertEqual((formatted.ROUTE["source_chat"], formatted.ROUTE["source_topic"],
                          formatted.ROUTE["dest_chat"], formatted.ROUTE["dest_topic"]),
                         (RELAY, 8323, VIP, 12289))
        self.assertEqual((wholechat.SOURCE_CHAT, wholechat.DEST_CHAT), (-1004281603170, -1003743381585))

    def test_paused_destinations_stay_paused(self):
        self.assertTrue(PAUSED_DESTINATIONS <= set(policy.STOPPED_DEST_CHATS))
        self.assertFalse({r["dest_chat"] for r in ROUTES} & PAUSED_DESTINATIONS)


if __name__ == "__main__":
    unittest.main()

"""One typed route table for both workers (telegram_worker/route_table.py)."""

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import route_table
from telegram_worker.route_table import RELAY, VIP, RouteSpec


class TableTests(unittest.TestCase):
    def test_main_routes_are_the_live_and_history_lines(self):
        from telegram_worker.routes import ROUTES

        self.assertEqual(ROUTES, route_table.routes("MAIN", "intake", "owner"))
        self.assertEqual({r["path"] for r in ROUTES}, {"intake", "owner"})
        live = [r for r in ROUTES if r["path"] == "intake"]
        self.assertTrue(all(r["live_only"] and not r.get("owner_managed") for r in live))

    def test_one_line_helpers_build_the_full_route(self):
        spec = route_table.live("New Route", (RELAY, 9000), (VIP, 9001))
        route = spec.as_dict()
        self.assertEqual(
            (route["path"], route["strict_source_topic"], route["loop_guard_peers"]),
            ("intake", True, [RELAY, VIP]),
        )
        whole = route_table.live("Whole Chat", (-1001, None), (VIP, 9002)).as_dict()
        self.assertNotIn("strict_source_topic", whole)
        hist = route_table.history("Hist", (VIP, 9001), (route_table.MARKET_MAKERS, 99), last=30).as_dict()
        self.assertEqual((hist["path"], hist["owner_managed"], hist["backfill"]), ("owner", True, 30))
        alt = route_table.relay("Feed", (-1002, 7), 9003).as_dict()
        self.assertEqual((alt["dest_chat"], alt["dest_topic"], alt["path"]), (RELAY, 9003, "relay"))

    def test_owner_targets_follow_the_history_lines(self):
        from telegram_worker import owner_routes

        self.assertEqual(
            owner_routes.TARGETS,
            (
                (RELAY, 7250, VIP, 12161, 50),
                (VIP, 1364, route_table.MARKET_MAKERS, 28, 20),
                (VIP, 10877, route_table.MARKET_MAKERS, 8, 20),
                (VIP, 12161, route_table.MARKET_MAKERS, 24, 20),
            ),
        )

    def test_routes_py_applies_the_railway_switches(self):
        import importlib

        from telegram_worker import routes

        try:
            with patch.dict(os.environ, {"PAUSED_DEST_CHATS": f"{VIP}, x", "PAUSED_SOURCE_CHATS": ""}):
                self.assertEqual(
                    [r["name"] for r in importlib.reload(routes).ROUTES],
                    [
                        "Owner VIP1364 To Partner28",
                        "Owner VIP10877 To Partner8",
                        "Owner VIP12161 To Partner24",
                    ],
                )
            with patch.dict(os.environ, {"DISABLE_PROVIDER_ROUTES": "1"}):
                self.assertEqual(importlib.reload(routes).ROUTES, [])
        finally:
            importlib.reload(routes)

    def test_alt_routes_are_the_five_relay_feeds(self):
        alt = route_table.routes("ALT")
        self.assertEqual(len(alt), 5)
        self.assertEqual({r["dest_chat"] for r in alt}, {RELAY})
        self.assertEqual(sorted(r["dest_topic"] for r in alt), [2, 115, 7248, 7250, 8323])
        self.assertEqual(
            set(alt[0]), {"name", "source_chat", "source_topic", "dest_chat", "dest_topic", "path"}
        )

    def test_side_routes_and_lookup(self):
        self.assertEqual(route_table.one("SamGtrades To Exposed VIP").source_chat, -1004281603170)
        self.assertEqual(route_table.routes("MAIN", "formatted")[0]["dest_topic"], 12289)
        self.assertEqual(len(route_table.routes("MAIN")), 11)
        with self.assertRaises(KeyError):
            route_table.one("No Such Route")

    def test_validate_refuses_mistakes(self):
        ok = RouteSpec("ok", "MAIN", "intake", RELAY, 2, VIP, 1364)
        for bad, reason in (
            (RouteSpec("x", "MAIN", "relay", RELAY, 2, VIP, 1), "unknown worker/path"),
            (RouteSpec("ok", "MAIN", "intake", RELAY, 3, VIP, 1), "duplicate name"),
            (RouteSpec("y", "MAIN", "intake", RELAY, 2, VIP, 1364), "duplicate source/destination"),
            (RouteSpec("z", "MAIN", "intake", RELAY, 2, VIP, 5), "retired destination"),
            (RouteSpec("z", "MAIN", "intake", RELAY, 2530, VIP, 9), "retired source"),
            (RouteSpec("z", "MAIN", "intake", RELAY, 7250, VIP, 11), "relay 7250 may only feed VIP12161"),
            (RouteSpec("z", "ALT", "relay", -1001, None, VIP, 9), "ALT writes only into the relay group"),
            (RouteSpec("z", "MAIN", "intake", VIP, 9, VIP, 9), "source and destination are the same"),
            (RouteSpec("z", "MAIN", "intake", RELAY, 9, VIP, None), "destination topic required"),
        ):
            with self.assertRaisesRegex(ValueError, reason):
                route_table.validate((ok, bad))


class AltSelectionTests(unittest.TestCase):
    """ALT takes its routes from the table only; a leftover ALT_ROUTES_JSON is ignored."""

    def select(self, env_value):
        import ast
        import logging

        from telegram_worker import owner_forwarding_policy as policy

        tree = ast.parse((ROOT / "telegram_worker/alt_worker_legacy.py").read_text(encoding="utf-8"))
        wanted = {"live_routes", "select_routes", "alt_vip5_block"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        ns = {
            "os": os,
            "route_table": route_table,
            "owner_policy": policy,
            "log": logging.getLogger("alt-route-test"),
            "ALT_RETIRED_DEST_CHAT": VIP,
            "ALT_RETIRED_DEST_TOPIC": 5,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<alt>", "exec"), ns)
        with patch.dict(os.environ, {"ALT_ROUTES_JSON": env_value}):
            return ns["select_routes"]()

    def test_routes_come_from_the_table(self):
        self.assertEqual(self.select(""), route_table.routes("ALT"))

    def test_a_leftover_railway_variable_is_ignored(self):
        with self.assertLogs("alt-route-test", "WARNING") as logs:
            routes = self.select(
                json.dumps([{"name": "old", "source_chat": 1, "dest_chat": 2, "dest_topic": 3}])
            )
        self.assertEqual(routes, route_table.routes("ALT"))
        self.assertIn("ALT_ROUTES_JSON is ignored", logs.output[0])


if __name__ == "__main__":
    unittest.main()

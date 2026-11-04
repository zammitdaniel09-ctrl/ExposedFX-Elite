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

FIXTURE = ROOT / "tests/fixtures/main_live_routes_20261104.json"


class TableTests(unittest.TestCase):
    def test_main_routes_equal_the_routes_main_ran_with_field_by_field(self):
        """Frozen from the old routes.py (100 definitions, 9 live) before the table replaced it."""
        self.assertEqual(
            route_table.routes("MAIN", "intake", "owner"), json.loads(FIXTURE.read_text("utf-8"))
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
        self.assertEqual(set(alt[0]), {"name", "source_chat", "source_topic", "dest_chat", "dest_topic"})

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
        ):
            with self.assertRaisesRegex(ValueError, reason):
                route_table.validate((ok, bad))


class AltSelectionTests(unittest.TestCase):
    """ALT takes the table, cross-checked against ALT_ROUTES_JSON while Railway still has it."""

    def select(self, env_routes):
        import ast
        import logging

        from telegram_worker import owner_forwarding_policy as policy

        tree = ast.parse((ROOT / "telegram_worker/alt_worker_legacy.py").read_text(encoding="utf-8"))
        wanted = {"load_routes", "live_routes", "select_routes", "alt_vip5_block"}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
        ns = {
            "os": os,
            "json": json,
            "route_table": route_table,
            "owner_policy": policy,
            "log": logging.getLogger("alt-route-test"),
            "ALT_RETIRED_DEST_CHAT": VIP,
            "ALT_RETIRED_DEST_TOPIC": 5,
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<alt>", "exec"), ns)
        env = (
            {"ALT_ROUTES_JSON": json.dumps(env_routes)} if env_routes is not None else {"ALT_ROUTES_JSON": ""}
        )
        with patch.dict(os.environ, env):
            return ns["select_routes"]()

    def test_matching_railway_routes_use_the_table(self):
        with self.assertLogs("alt-route-test", "WARNING") as logs:
            routes = self.select(route_table.routes("ALT"))
        self.assertEqual(routes, route_table.routes("ALT"))
        self.assertIn("matches_ALT_ROUTES_JSON=True", logs.output[0])

    def test_differing_railway_routes_are_kept_and_reported(self):
        configured = route_table.routes("ALT")[:2]
        with self.assertLogs("alt-route-test", "ERROR") as logs:
            routes = self.select(configured)
        self.assertEqual([r["name"] for r in routes], [r["name"] for r in configured])
        self.assertIn("[ROUTE TABLE MISMATCH] worker=ALT", logs.output[0])

    def test_without_the_railway_variable_the_table_is_used(self):
        self.assertEqual(self.select(None), route_table.routes("ALT"))


if __name__ == "__main__":
    unittest.main()

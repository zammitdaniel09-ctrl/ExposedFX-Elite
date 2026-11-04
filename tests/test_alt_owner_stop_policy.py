import ast
import importlib.util
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from telethon.errors import ChannelPrivateError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "owner_policy_test", ROOT / "telegram_worker/owner_forwarding_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
TREE = ast.parse((ROOT / "telegram_worker/alt_worker_legacy.py").read_text(encoding="utf-8"))


def load_functions(*names, **extra):
    ns = {"owner_policy": policy, "os": os, "json": json, "log": logging.getLogger("alt-test"), **extra}
    nodes = [
        n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<alt>", "exec"), ns)
    return ns


class AltStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy.configure(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    async def test_stopped_sources_and_vip5_are_dropped_from_alt_routes(self):
        samples = [
            (1, -1002211486103),
            (2, -1003252087470),
            (3, -1004347858259),
            (4, -1003364661276),
            (5, -1004369388188),
            (6, -1004347858259),
            (7, -1003252087470),
            (8, -1004349952583),
            (9, -1004349952583),
        ]
        data = [
            dict(name=str(i), source_chat=source, source_topic=None, dest_chat=-1004367822325, dest_topic=i)
            for i, source in samples
        ]
        data.append(
            dict(
                name="vip5",
                source_chat=-1004349952583,
                source_topic=1,
                dest_chat=-1003726286301,
                dest_topic=5,
            )
        )
        ns = load_functions(
            "live_routes", "alt_vip5_block", ALT_RETIRED_DEST_CHAT=-1003726286301, ALT_RETIRED_DEST_TOPIC=5
        )
        self.assertEqual([r["name"] for r in ns["live_routes"](data)], ["1", "4", "8", "9"])

    async def test_inaccessible_fetch_is_attempted_once_and_then_stopped(self):
        route = dict(source_chat=-1004349952583, source_topic=5246, dest_chat=-1004367822325, dest_topic=7248)
        calls = []

        async def fail(*args, **kwargs):
            calls.append(1)
            raise ChannelPrivateError(request=None)

        ns = load_functions("fetch_route_messages", _owner_original_fetch_route_messages=fail)
        self.assertEqual(await ns["fetch_route_messages"](route, 50), [])
        self.assertEqual(await ns["fetch_route_messages"](route, 50), [])
        self.assertEqual(calls, [1])
        self.assertTrue(policy.is_stopped(route))

    async def test_stopped_route_is_handled_without_delivering(self):
        route = dict(source_chat=-1003252087470, source_topic=None, dest_chat=-1004367822325, dest_topic=2585)
        policy.stop_route(route, "ChannelPrivateError", logging.getLogger("alt-test"))
        ns = load_functions("intake_deliver")
        # No copy_unit or checkpoint helper is provided: touching them would fail the test.
        self.assertIs(await ns["intake_deliver"](route, [object()], "watchdog"), True)

    async def test_route_stopped_during_delivery_is_not_retried(self):
        route = dict(source_chat=-1004349952583, source_topic=5247, dest_chat=-1004367822325, dest_topic=7250)

        async def copy_unit(route, unit, reason):
            policy.stop_route(route, "ChannelPrivateError", logging.getLogger("alt-test"))
            return False

        ns = load_functions("intake_deliver", copy_unit=copy_unit)
        self.assertIs(await ns["intake_deliver"](route, [object()], "telegram_push"), True)


if __name__ == "__main__":
    unittest.main()

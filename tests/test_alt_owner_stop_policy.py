import ast
import asyncio
import importlib.util
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
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

    async def test_current_nine_routes_keep_four_accessible_routes(self):
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
        ns = load_functions("load_routes")
        with patch.dict(os.environ, {"ALT_ROUTES_JSON": json.dumps(data)}):
            result = [r for r in ns["load_routes"]() if not policy.is_stopped(r)]
        self.assertEqual([r["name"] for r in result], ["1", "4", "8", "9"])

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

    async def test_stopped_watchdog_does_not_read_or_modify_checkpoint(self):
        ns = load_functions("watchdog")
        await ns["watchdog"](dict(source_chat=-1003252087470, dest_chat=-1004367822325, dest_topic=2585))
        # No checkpoint helper is provided: touching it would fail the test.

    async def test_stopped_send_paths_never_contact_telegram(self):
        route = dict(source_chat=-1003252087470, dest_chat=-1004367822325, dest_topic=2585)
        ns = load_functions("send_single", "send_album")
        self.assertIsNone(await ns["send_single"](route, None))
        self.assertIsNone(await ns["send_album"](route, []))


if __name__ == "__main__":
    unittest.main()

"""Backend audit 4 Nov 2026: behaviour-preserving efficiency and safety fixes."""
import ast
import asyncio
import json
import logging
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import destination_directory as dd

try:
    import requests  # noqa: F401  (CI installs only telethon)
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")
from telegram_worker import runtime_guard

WORKER_SOURCE = (ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8")
WORKER_TREE = ast.parse(WORKER_SOURCE)


def worker_functions(*names, **extra):
    ns = {"json": json, "time": time, "log": logging.getLogger("audit-test"), **extra}
    nodes = [n for n in WORKER_TREE.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names), [n.name for n in nodes]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<worker>", "exec"), ns)
    return ns


class MessageMapTests(unittest.TestCase):
    def test_corrupt_map_is_kept_aside_not_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_file = Path(tmp) / "message_map.json"
            map_file.write_text("{not json", encoding="utf-8")
            ns = worker_functions("load_map", MESSAGE_MAP_FILE=map_file)
            self.assertEqual(ns["load_map"](), {})
            kept = list(Path(tmp).glob("message_map.corrupt-*.json"))
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0].read_text(encoding="utf-8"), "{not json")

    def test_album_items_saved_once(self):
        saves = []
        message_map = {}
        ns = worker_functions(
            "remember_message", message_map=message_map, save_map=lambda: saves.append(1),
            map_key=lambda *parts: ":".join(map(str, parts)),
        )
        route = {"source_chat": -1, "dest_chat": -2, "dest_topic": 3}
        ns["remember_message"](SimpleNamespace(id=10), SimpleNamespace(id=20), route, save=False)
        ns["remember_message"](SimpleNamespace(id=11), SimpleNamespace(id=21), route, save=False)
        self.assertEqual(saves, [])
        ns["remember_message"](SimpleNamespace(id=12), SimpleNamespace(id=22), route)
        self.assertEqual((len(saves), message_map["-1:12:-2:3"]), (1, 22))
        self.assertIn("remember_message(src, dst, route, save=False)\n        save_map()", WORKER_SOURCE)


class VipRouterTests(unittest.TestCase):
    def test_backstop_ignores_service_messages(self):
        self.assertIn('> baseline\n                and not getattr(message, "action", None)', WORKER_SOURCE)

    def test_uncopyable_unit_counts_as_handled(self):
        self.assertIn("if not any(text_of(item) or is_real_media(item) for item in unit):", WORKER_SOURCE)
        self.assertIn("[FASTVIP SKIP UNSUPPORTED]", WORKER_SOURCE)

    def test_media_fallbacks_reraise_flood_and_lost_access(self):
        guard = "if isinstance(exc, FloodWaitError) or owner_policy.permanent_access_error(exc):\n            raise"
        self.assertEqual(WORKER_SOURCE.count(guard), 2)


class AlertTests(unittest.TestCase):
    def test_alert_never_blocks_the_event_loop(self):
        posted = []

        def slow_post(text):
            time.sleep(0.5)
            posted.append(text)
            return True

        async def go():
            started = time.monotonic()
            runtime_guard.send_alert("hello")
            return time.monotonic() - started

        with patch.object(runtime_guard, "_alert_enabled", return_value=True), \
                patch.object(runtime_guard, "_post_alert", slow_post):
            elapsed = asyncio.run(go())
            for _ in range(20):
                if posted:
                    break
                time.sleep(0.1)
        self.assertLess(elapsed, 0.2)
        self.assertEqual(posted, ["hello"])


class StartupCostTests(unittest.TestCase):
    def test_unchanged_destination_directory_makes_no_calls(self):
        calls = []

        class Client:
            async def get_entity(self, chat):
                calls.append(chat)
                return SimpleNamespace(title="t")

            async def __call__(self, request):
                calls.append(request)
                return SimpleNamespace(topics=[])

        async def title(chat, topic):
            return "x"

        async def nosleep(_):
            return None

        with tempfile.TemporaryDirectory() as tmp:
            worker = SimpleNamespace(ROUTES=[{"name": "r", "dest_chat": -100, "dest_topic": 7}], client=Client(),
                                     DATA_DIR=tmp, channel_function=lambda name: None,
                                     title_from_entity=lambda e: e.title, get_forum_topic_title=title)
            with patch.object(dd.asyncio, "sleep", nosleep):
                asyncio.run(dd.log_destination_directory(worker))
                first = len(calls)
                asyncio.run(dd.log_destination_directory(worker))
            self.assertGreater(first, 0)
            self.assertEqual(len(calls), first)


class Topic1SweepTests(unittest.TestCase):
    def test_sweep_paused_while_hub_is_a_stopped_destination(self):
        from telegram_worker import owner_forwarding_policy as policy
        from telegram_worker import topic1_kill_switch as kill
        self.assertIn(kill.HUB_CHAT, policy.STOPPED_DEST_CHATS)
        source = (ROOT / "telegram_worker/topic1_kill_switch.py").read_text(encoding="utf-8")
        body = source[source.index("async def sweep_loop():"):]
        self.assertLess(body.index("[TOPIC1 SWEEP PAUSED]"), body.index('purge_and_reverse_audit("startup-full-purge")'))
        self.assertIn("async def dead_topic_intrusion_guard(event):", source)


class RepoHygieneTests(unittest.TestCase):
    def test_no_workflow_rewrites_production_code(self):
        for workflow in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("git push", workflow.read_text(encoding="utf-8"), workflow.name)

    def test_every_package_import_resolves(self):
        package_dir = ROOT / "telegram_worker"
        for path in package_dir.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("telegram_worker."):
                    names = [node.module.split(".", 1)[1]]
                elif isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                    names = [node.module]
                for name in names:
                    target = name.split(".")[0]
                    self.assertTrue((package_dir / f"{target}.py").exists() or (package_dir / target).is_dir(),
                                    f"{path.name} imports {name}")


if __name__ == "__main__":
    unittest.main()

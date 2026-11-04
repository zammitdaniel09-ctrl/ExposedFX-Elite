"""MAIN's DeliveryEngine: start-up from the environment, both entry points, ledger recall and edits."""

import asyncio
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.community_emoji_registry import CommunityEmojiRegistry
from telegram_worker.engine import runtime
from telegram_worker.engine.model import Route
from telegram_worker.permanent_delivery_errors import PermanentFailureLedger

VIP, RELAY = -1003726286301, -1004367822325
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
VIP7 = {
    "name": "Relay508 To VIP7",
    "source_chat": RELAY,
    "source_topic": 508,
    "dest_chat": VIP,
    "dest_topic": 7,
}
LOG = "imperium-worker"
ENV = {"BLOCKED_SENDER_IDS": "111, 222", "FILTER_USERNAME_MENTIONS": "1", "FILTER_LINKS": "0"}


def post(msg_id, text, sender=None):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=[],
        media=None,
        grouped_id=None,
        reply_to=None,
        reply_to_msg_id=None,
        fwd_from=None,
        sender_id=sender,
        from_id=None,
        date=None,
    )


class Client:
    def __init__(self):
        self.calls, self.next_id = [], 9000

    async def send_message(self, chat, text, **kwargs):
        self.calls.append((chat, text, kwargs["reply_to"]))
        self.next_id += 1
        return SimpleNamespace(id=self.next_id, message=text, raw_text=text, text=text, media=None)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)
        self.client = Client()
        self.worker = SimpleNamespace(
            client=self.client,
            message_map={},
            DATA_DIR=self.tmp.name,
            EMOJI_REGISTRY=None,
            ROUTES=[ROUTE, VIP7],
            FASTVIP_PERMANENT_FAILURES=PermanentFailureLedger(Path(self.tmp.name) / "ledger.json"),
            COPY_RETRY_ATTEMPTS=2,
            COPY_RETRY_SLEEP_CAP_SECONDS=30,
            save_map=lambda: None,
        )
        self.worker.remember_message = self.remember
        env = patch.dict(os.environ, ENV)
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        policy._RUNTIME, policy._PATH = None, None
        self.tmp.cleanup()

    def remember(self, src, dst, route, save=True):
        key = f"{route['source_chat']}:{src.id}:{route['dest_chat']}:{route['dest_topic']}"
        self.worker.message_map[key] = dst.id

    def install(self):
        with self.assertLogs(LOG, "WARNING") as logs:
            engine = runtime.install_engine(self.worker)
        return engine, logs.output

    def test_install_reads_retry_settings_and_rules_from_the_worker_and_environment(self):
        engine, output = self.install()
        self.assertIs(self.worker.DELIVERY_ENGINE, engine)
        self.assertIn("[DELIVERY ENGINE READY] routes=2 formatters=['copy', 'imperium_vip']", output[0])
        self.assertEqual((engine.deliverer.attempts, engine.deliverer.sleep_cap), (2, 30))
        rules = engine.planner.rules
        self.assertEqual(
            (rules.blocked_senders, rules.filter_mentions, rules.filter_links), ({111, 222}, True, False)
        )

    def test_bare_worker_gets_the_default_retry_settings(self):
        bare = SimpleNamespace(client=self.client)
        with self.assertLogs(LOG, "WARNING") as logs:
            engine = runtime.install_engine(bare)
        self.assertIn("routes=0 formatters=[]", logs.output[0])
        self.assertEqual((engine.deliverer.attempts, engine.deliverer.sleep_cap), (3, 120))
        self.assertTrue(engine.planner.maps.mapping == {}, "a worker without a map reads as empty")

    def test_live_delivery_follows_the_environment_rules(self):
        engine, _ = self.install()
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(asyncio.run(engine.deliver(ROUTE, [post(1, "chart at example.com")])))
            self.assertTrue(asyncio.run(engine.deliver(ROUTE, [post(2, "TP1 hit", sender=222)])))
        self.assertIn("[FASTVIP COPIED]", logs.output[0])
        self.assertIn("ids=[2] reason=blocked_sender", logs.output[1])
        self.assertEqual(
            self.client.calls, [(VIP, "chart at example.com", 1364)], "blocked sender 222 skipped"
        )
        self.assertEqual(self.worker.message_map, {f"{RELAY}:1:{VIP}:1364": 9001})

    def test_post_refused_for_good_is_recalled_live_but_not_on_replay(self):
        engine, _ = self.install()
        self.worker.FASTVIP_PERMANENT_FAILURES.record(ROUTE, [5], "ChatWriteForbiddenError")
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(asyncio.run(engine.deliver(ROUTE, [post(5, "TP2 hit")])))
        self.assertIn("reason=permanent_failure_recalled", logs.output[0])
        self.assertEqual(self.client.calls, [])
        with self.assertLogs(LOG, "WARNING"):
            self.assertTrue(asyncio.run(engine.deliver(ROUTE, [post(5, "TP2 hit")], live=False)))
        self.assertEqual(self.client.calls, [(VIP, "TP2 hit", 1364)], "replays ignore the ledger")

    def test_owner_copy_reports_sent_then_existing(self):
        engine, _ = self.install()

        def mapped_ids(worker, legacy_route, unit):
            keys = [f"{RELAY}:{m.id}:{VIP}:1364" for m in unit]
            return [worker.message_map[k] for k in keys if k in worker.message_map]

        owner_routes = SimpleNamespace(mapped_ids=mapped_ids)
        self.assertEqual(
            asyncio.run(engine.copy_unit(owner_routes, ROUTE, [post(8, "TP5")])), ("sent", [9001])
        )
        self.assertEqual(
            asyncio.run(engine.copy_unit(owner_routes, ROUTE, [post(8, "TP5")])), ("existing", [9001])
        )
        self.assertEqual(len(self.client.calls), 1)

    def test_only_the_vip7_route_is_restyled(self):
        self.assertEqual(runtime.formatter_for(Route.from_legacy(VIP7)), "imperium_vip")
        self.assertEqual(runtime.engine_route(VIP7).formatter, "imperium_vip")
        self.assertEqual(runtime.engine_route(ROUTE).formatter, "copy")

    def test_edit_that_is_not_a_signal_or_update_is_left_unchanged(self):
        registry = CommunityEmojiRegistry(Path(self.tmp.name))
        registry.entries = {
            alias: {"type": "custom", "document_id": 5000 + i, "fallback": "✅"}
            for i, alias in enumerate(("Boom", "GreenTick", "RedCross", "Warning"))
        }
        edited = post(60, "Good morning traders")
        with self.assertNoLogs(LOG, "WARNING"):
            self.assertIs(runtime.restyle_for_route(edited, VIP7, registry), edited)
            self.assertIs(runtime.restyle_for_route(edited, ROUTE, registry), edited)
        self.assertEqual(edited.message, "Good morning traders")


if __name__ == "__main__":
    unittest.main()

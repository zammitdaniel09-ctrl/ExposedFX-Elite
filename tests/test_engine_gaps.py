"""Deployment-gap recovery: posts that arrived while MAIN was restarting."""

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor
from telegram_worker.engine import gaps
from telegram_worker.engine.model import Route
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules

RELAY, VIP = -1004367822325, -1003726286301
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)


def post(msg_id, text, minutes_ago):
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
        sender_id=None,
        from_id=None,
        action=None,
        date=NOW - timedelta(minutes=minutes_ago),
    )


class Engine:
    def __init__(self, message_map):
        self.planner = Planner(rules=Rules(), maps=MessageMap(message_map))
        self.message_map = message_map
        self.delivered = []

    async def deliver(self, legacy_route, unit, live=True):
        self.delivered.append([m.id for m in unit])
        for m in unit:
            self.message_map[f"{RELAY}:{m.id}:{VIP}:1364"] = 9000 + m.id
        return True


class GapTests(unittest.TestCase):
    def run_gaps(self, message_map, source_posts):
        calls = []

        class Client:
            async def get_messages(self, chat, **kwargs):
                calls.append((chat, kwargs))
                return [p for p in source_posts if p.id > kwargs["min_id"]]

        engine = Engine(message_map)
        worker = SimpleNamespace(client=Client(), message_map=message_map, EMOJI_REGISTRY=None)
        report = asyncio.run(gaps.recover_gaps(worker, engine, [ROUTE], clock=NOW.timestamp))
        return report, engine, calls

    def test_last_delivered_is_the_newest_mapped_source_post_of_that_route(self):
        mapping = {f"{RELAY}:8513:{VIP}:1364": 1, f"{RELAY}:8516:{VIP}:1364": 2, f"{RELAY}:9999:{VIP}:7": 3}
        self.assertEqual(gaps.last_delivered(mapping, Route.from_legacy(ROUTE)), 8516)

    def test_recent_missed_post_is_delivered_and_stale_one_is_reported_not_sent(self):
        mapping = {f"{RELAY}:8516:{VIP}:1364": 1}
        posts = [
            post(8516, "old", 200),
            post(8522, "TP1 hit", 100),
            post(8530, "Buy now", 3),
            post(8531, "join t.me/x", 2),
        ]
        with self.assertLogs("imperium-worker", "WARNING") as logs:
            report, engine, calls = self.run_gaps(mapping, posts)
        self.assertEqual(calls, [(RELAY, {"limit": gaps.FETCH_LIMIT, "min_id": 8516, "reply_to": 2})])
        self.assertEqual(engine.delivered, [[8530]], "only the fresh missed post is sent")
        self.assertEqual(report[ROUTE["name"]], {"recovered": 1, "stale": 1, "skipped": 1})
        stale = [line for line in logs.output if "GAP STALE NOT SENT" in line]
        self.assertEqual(len(stale), 1)
        self.assertIn("ids=[8522]", stale[0])

    def test_route_never_delivered_is_left_alone(self):
        report, engine, calls = self.run_gaps({}, [post(5, "Buy now", 1)])
        self.assertEqual((calls, engine.delivered), ([], []))

    def test_stale_gap_reaches_saved_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = health_monitor.HealthMonitor("ExposedFX MAIN", tmp)
            monitor.observe(
                logging.WARNING,
                "[GAP STALE NOT SENT] route=VIP RELAY Topic2 To VIP1364 ids=[8522] age=6000s max=600s",
            )
            self.assertEqual(len(monitor.outbox), 1)
            self.assertIn("[8522]", monitor.outbox[0])
            self.assertIn("not sent", monitor.outbox[0])


if __name__ == "__main__":
    unittest.main()

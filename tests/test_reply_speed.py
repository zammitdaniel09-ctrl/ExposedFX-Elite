"""Reply-parent resolution stays fast: map hits, in-flight parents, bounded recovery, no double work."""

import asyncio
import sys
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
from telegram_worker import reply_hardening as rh

ROUTE = {"name": "r", "source_chat": -100111, "source_topic": 5, "dest_chat": -100222, "dest_topic": 9}


class FakeClient:
    def __init__(self, delay=0.0):
        self.calls = []
        self.delay = delay

    async def get_messages(self, chat, ids=None, limit=None, reply_to=None):
        self.calls.append((chat, ids, limit))
        if self.delay:
            await asyncio.sleep(self.delay)
        return SimpleNamespace(id=ids) if ids is not None else []


def reply(mid, parent):
    return SimpleNamespace(
        id=mid, reply_to_msg_id=None, reply_to=SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=5)
    )


def key(parent):
    return f"{ROUTE['source_chat']}:{parent}:{ROUTE['dest_chat']}:{ROUTE['dest_topic']}"


class ReplySpeedTests(unittest.TestCase):
    def setUp(self):
        rh._RESOLVED_MEMO.clear()
        self.main = SimpleNamespace(client=FakeClient(), message_map={})

    def resolve(self, message):
        return asyncio.run(rh.ensure_reply_mapping(self.main, message, ROUTE))

    def test_mapped_parent_costs_one_existence_check(self):
        self.main.message_map[key(40)] = 700
        self.assertEqual(self.resolve(reply(41, 40)), 700)
        self.assertEqual(len(self.main.client.calls), 1)

    def test_in_flight_parent_is_awaited_in_memory(self):
        async def go():
            async def land():
                await asyncio.sleep(0.3)
                self.main.message_map[key(50)] = 800

            asyncio.get_running_loop().create_task(land())
            return await rh.ensure_reply_mapping(self.main, reply(51, 50), ROUTE)

        self.assertEqual(asyncio.run(go()), 800)
        self.assertEqual(len(self.main.client.calls), 1)  # only the existence check

    def test_unmapped_parent_is_bounded(self):
        self.main.client = FakeClient(delay=10)
        with patch.object(rh, "PARENT_INFLIGHT_WAIT_SECONDS", 0.2), patch.object(
            rh, "RECOVERY_BUDGET_SECONDS", 0.3
        ):
            started = time.monotonic()
            self.assertIsNone(self.resolve(reply(61, 60)))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_second_copy_layer_reuses_result(self):
        self.main.message_map[key(70)] = 900
        message = reply(71, 70)
        self.assertEqual(self.resolve(message), 900)
        self.assertEqual(self.resolve(message), 900)
        self.assertEqual(len(self.main.client.calls), 1)

    def test_cached_miss_is_rechecked_once_parent_is_imported(self):
        message = reply(81, 80)
        with patch.object(rh, "PARENT_INFLIGHT_WAIT_SECONDS", 0.0):
            self.assertIsNone(self.resolve(message))
            self.main.message_map[key(80)] = 950
            self.assertEqual(self.resolve(message), 950)


if __name__ == "__main__":
    unittest.main()

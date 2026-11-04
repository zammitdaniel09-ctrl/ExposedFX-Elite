"""ALT reply-parent resolution: trusted map, in-flight wait, bounded recovery, deleted-parent fallback."""

import asyncio
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import alt_reply_hardening as arh

ROUTE = {"name": "r", "source_chat": -100111, "source_topic": 5, "dest_chat": -100222, "dest_topic": 9}
NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, delay=0.0, history=None):
        self.calls = []
        self.delay = delay
        self.history = history or []

    async def get_messages(self, chat, ids=None, limit=None, reply_to=None):
        self.calls.append((chat, ids, limit, reply_to))
        if self.delay:
            await asyncio.sleep(self.delay)
        if ids is not None:
            return SimpleNamespace(id=ids, message="Signal text", media=None, date=NOW)
        return self.history


class FakeWorker:
    def __init__(self, client):
        self.client = client
        self.message_map = {}
        self.MAP_FILE = None

    def map_key(self, route, source_id):
        return f"{route['source_chat']}:{source_id}"

    def mapped_ids(self, route, source_id):
        value = self.message_map.get(self.map_key(route, source_id))
        return [value] if value else []

    def save_json(self, path, value):
        pass

    async def translated_payload(self, route, message):
        return message.message, None


def reply(mid, parent):
    return SimpleNamespace(id=mid, reply_to_msg_id=parent, reply_to_top_id=5, top_msg_id=None, reply_to=None)


class AltReplySpeedTests(unittest.TestCase):
    def resolve(self, worker, message):
        return asyncio.run(arh.resolve_reply_target(worker, ROUTE, message))

    def test_mapped_parent_needs_no_api_call(self):
        worker = FakeWorker(FakeClient())
        worker.message_map["-100111:40"] = 700
        self.assertEqual(self.resolve(worker, reply(41, 40)), 700)
        self.assertEqual(worker.client.calls, [])

    def test_in_flight_parent_is_awaited_in_memory(self):
        worker = FakeWorker(FakeClient())

        async def go():
            async def land():
                await asyncio.sleep(0.3)
                worker.message_map["-100111:50"] = 800

            asyncio.get_running_loop().create_task(land())
            return await arh.resolve_reply_target(worker, ROUTE, reply(51, 50))

        self.assertEqual(asyncio.run(go()), 800)
        self.assertEqual(worker.client.calls, [])

    def test_unmapped_parent_falls_back_within_budget(self):
        worker = FakeWorker(FakeClient(delay=10))
        with patch.object(arh, "PARENT_INFLIGHT_WAIT_SECONDS", 0.2), patch.object(
            arh, "RECOVERY_BUDGET_SECONDS", 0.3
        ):
            started = time.monotonic()
            self.assertEqual(self.resolve(worker, reply(61, 60)), 9)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_recovery_is_one_small_history_read(self):
        copy = SimpleNamespace(
            id=555,
            message="Signal text",
            media=None,
            date=NOW,
            reply_to=SimpleNamespace(reply_to_top_id=9, reply_to_msg_id=9, top_msg_id=None),
        )
        worker = FakeWorker(FakeClient(history=[copy]))
        with patch.object(arh, "PARENT_INFLIGHT_WAIT_SECONDS", 0.0):
            self.assertEqual(self.resolve(worker, reply(71, 70)), 555)
        history_reads = [c for c in worker.client.calls if c[1] is None]
        self.assertEqual(len(history_reads), 1)
        self.assertLessEqual(history_reads[0][2], 100)
        self.assertEqual(worker.message_map["-100111:70"], 555)

    def test_deleted_parent_error_is_recognised(self):
        self.assertTrue(arh._reply_target_invalid(Exception("REPLY_MESSAGE_ID_INVALID")))
        self.assertTrue(
            arh._reply_target_invalid(Exception("The message ID used ... (caused by MSG_ID_INVALID)"))
        )
        self.assertFalse(arh._reply_target_invalid(Exception("FLOOD_WAIT_X")))

    def test_telethons_own_error_classes_are_recognised(self):
        # Their text ("The message ID used in the peer was invalid ...") has no RPC code.
        from telethon.errors import MessageIdInvalidError, MsgIdInvalidError

        for error in (MsgIdInvalidError(request=None), MessageIdInvalidError(request=None)):
            self.assertNotIn("MSG_ID_INVALID", str(error).upper())
            self.assertTrue(arh._reply_target_invalid(error), type(error).__name__)


if __name__ == "__main__":
    unittest.main()

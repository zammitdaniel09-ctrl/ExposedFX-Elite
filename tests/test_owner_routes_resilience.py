"""Owner routes (4 Nov 2026): one bad post cannot block a route, stop strikes clear on
success, sends run inside SENDS, the poll is only a 15 s backstop and handlers never stack."""

import asyncio
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import owner_routes as owner
from telegram_worker import route_table
from telegram_worker.state_store import SendGuard
from telethon.errors import ChannelPrivateError, FloodWaitError

KEYS = ("source_chat", "source_topic", "dest_chat", "dest_topic")
ROUTE_7250 = route_table.one("Owner Relay7250 To VIP12161").as_dict()


def post(mid, topic, text="synthetic message", parent=None, minutes=0):
    reply = SimpleNamespace(reply_to_top_id=topic if parent else None, reply_to_msg_id=parent or topic)
    when = datetime.now(timezone.utc) - timedelta(minutes=10) + timedelta(minutes=minutes)
    return SimpleNamespace(id=mid, message=text, media=None, grouped_id=None, date=when, reply_to=reply)


def guard():
    fresh = SendGuard()
    fresh.registered = True  # no shutdown hook for a test guard
    return fresh


def watch_saves(module, sends):
    """Patches module.save_state to record how many sends were in flight at each write."""
    real, seen = module.save_state, []

    def save(path, state):
        seen.append(sends.inflight)
        real(path, state)

    return patch.object(module, "save_state", save), seen


class Client:
    """Offline Telethon stand-in."""

    def __init__(self, rows):
        self.rows = rows
        self.handlers, self.removed = [], []

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return True

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback, event):
        self.removed.append((callback, event))

    async def get_messages(self, chat, ids=None, reply_to=None, limit=None, **kwargs):
        if ids is not None:
            return SimpleNamespace(id=ids, action=type("MessageActionTopicCreate", (), {})())
        return sorted(self.rows, key=lambda m: -m.id)[:limit]

    async def iter_messages(self, chat, reply_to=None, min_id=0, reverse=False, limit=50):
        for row in sorted(self.rows, key=lambda m: m.id):
            if row.id > min_id:
                yield row


class Worker:
    """Stand-in for MAIN with the delivery engine's copy_unit contract, failing on demand."""

    def __init__(self, data_dir, rows=()):
        self.DATA_DIR = data_dir
        self.GLOBAL_REPLY_HARDENING = True
        self.client = Client(list(rows))
        self.DELIVERY_ENGINE = self
        self.sent, self.failing, self.inflight = [], {}, []

    def text_of(self, m):
        return m.message

    def is_real_media(self, m):
        return bool(m.media)

    async def copy_unit(self, owner_module, route, unit):
        self.inflight.append(owner_module.SENDS.inflight)
        if unit[0].id in self.failing:
            raise self.failing[unit[0].id]
        self.sent.append(unit[0].id)
        return "sent", [900000 + unit[0].id]


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.path)
        self.addCleanup(policy._STRIKES.clear)


class OwnerRouteTests(Base):
    def setUp(self):
        super().setUp()
        topic = ROUTE_7250["source_topic"]
        self.worker = Worker(self.path, [post(101, topic), post(102, topic), post(103, topic)])
        self.entry = {"last_source_id": 100}
        self.state = {policy.identity(ROUTE_7250): self.entry}
        self.file = self.path / owner.STATE_FILE

    async def cycle(self):
        await owner.live_cycle(self.worker, ROUTE_7250, self.entry, self.state, self.file)

    async def test_failing_post_is_retried_then_given_up_and_the_route_continues(self):
        self.worker.failing[102] = RuntimeError("Telegram refused the post")
        for attempt in (1, 2):
            with self.assertRaises(RuntimeError):
                await self.cycle()
            self.assertEqual(self.entry["last_source_id"], 101)
            self.assertEqual(self.entry["failures"]["102"]["attempts"], attempt)
        saved = owner.load_state(self.file)[policy.identity(ROUTE_7250)]["failures"]["102"]
        self.assertEqual(saved["attempts"], 2, "the first failure time survives a restart")
        self.assertEqual(self.worker.sent, [101])
        self.entry["failures"]["102"]["first"] -= owner.GIVE_UP_SECONDS  # an hour later
        with self.assertLogs(owner.log, "ERROR") as logs:
            await self.cycle()
        line = logs.records[0].getMessage()
        self.assertEqual(line, "[INTAKE GAVE UP] route=Owner Relay7250 To VIP12161 ids=[102] attempts=3")
        self.assertEqual(health_monitor.GAVE_UP.search(line).group("ids"), "[102]")
        self.assertEqual(self.worker.sent, [101, 103])
        self.assertEqual((self.entry["last_source_id"], self.entry["failures"]), (103, {}))
        await self.cycle()
        self.assertEqual(self.worker.sent, [101, 103])

    async def test_post_that_recovers_clears_its_failure(self):
        self.worker.failing[102] = ConnectionError("network down")
        with self.assertRaises(ConnectionError):
            await self.cycle()
        del self.worker.failing[102]
        await self.cycle()
        self.assertEqual((self.worker.sent, self.entry["failures"]), ([101, 102, 103], {}))

    async def test_flood_waits_and_access_errors_are_not_counted(self):
        self.worker.failing[101] = FloodWaitError(request=None, capture=30)
        with self.assertRaises(FloodWaitError):
            await self.cycle()
        self.assertEqual((self.entry["last_source_id"], self.entry.get("failures", {})), (100, {}))
        holder = {"failures": {"5": {"first": time.time() - 2 * owner.GIVE_UP_SECONDS, "attempts": 9}}}
        for exc in (FloodWaitError(request=None, capture=30), ChannelPrivateError(request=None)):
            self.assertFalse(owner.note_failure(holder, [5], exc, "r", owner.log))
        self.assertEqual(holder["failures"]["5"]["attempts"], 9)

    async def test_send_and_its_checkpoint_write_run_inside_sends(self):
        sends = guard()
        saves, seen = watch_saves(owner, sends)
        with patch.object(owner, "SENDS", sends), saves:
            await self.cycle()
        self.assertEqual((self.worker.inflight, seen), ([1, 1, 1], [1, 1, 1]))
        self.assertEqual(sends.inflight, 0)

    async def test_no_new_send_starts_while_shutting_down(self):
        sends = guard()
        sends.closing = True
        with patch.object(owner, "SENDS", sends):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.cycle(), 0.1)
        self.assertEqual((self.worker.sent, self.entry["last_source_id"]), ([], 100))

    async def test_live_loop_clears_strikes_on_success_and_removes_its_handler(self):
        routes = [dict(zip(KEYS, c[:4]), name=str(i)) for i, c in enumerate(owner.TARGETS)]
        self.worker.ROUTES = routes
        owner.save_state(
            self.file, {policy.identity(r): {"status": "done", "last_source_id": 0} for r in routes}
        )
        for route in routes:
            policy._STRIKES[policy.identity(route)] = [time.time()]
        calls = []

        async def live(worker, route, entry, state, path):
            calls.append(route["name"])
            if len(calls) == 1:  # a new post in the first route's topic wakes the loop at once
                callback, _ = worker.client.handlers[0]
                event = SimpleNamespace(chat_id=route["source_chat"], message=post(7, route["source_topic"]))
                await callback(event)
            if len(calls) == 2:
                raise ConnectionError("network down")
            if len(calls) == 5:
                raise asyncio.CancelledError

        with patch.object(owner, "live_cycle", live):
            with self.assertRaises(asyncio.CancelledError):
                await owner.run_owner_requested_routes(self.worker)
        self.assertEqual(calls, ["0", "1", "2", "3", "0"])
        self.assertEqual([policy.identity(r) in policy._STRIKES for r in routes], [False, True, False, False])
        self.assertEqual(len(self.worker.client.handlers), 1)
        self.assertEqual(self.worker.client.removed, self.worker.client.handlers)

    def test_poll_is_only_a_backstop(self):
        self.assertEqual(owner.LIVE_POLL_SECONDS, 15)
        self.assertEqual(owner.GIVE_UP_SECONDS, 3600)


if __name__ == "__main__":
    unittest.main()

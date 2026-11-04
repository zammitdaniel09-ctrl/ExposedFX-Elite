"""Owner routes and the 8323 formatter (4 Nov 2026): one bad post cannot block a route,
a deleted signal copy falls back to the topic root, stop strikes clear on success,
sends run inside SENDS, the poll is only a 15 s backstop and handlers never stack."""

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
from telegram_worker import owner_formatted_route as formatted
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import owner_routes as owner
from telegram_worker import route_table
from telegram_worker.state_store import SendGuard
from telethon.errors import ChannelPrivateError, ChatWriteForbiddenError, FloodWaitError
from telethon.errors import MessageIdInvalidError, MsgIdInvalidError

KEYS = ("source_chat", "source_topic", "dest_chat", "dest_topic")
ROUTE_7250 = route_table.one("Owner Relay7250 To VIP12161").as_dict()
SIGNAL = "🔥 GOLD TRADE IDEA\n\n🟢 BUY GOLD NOW @ 3895\n\n❌ SL: 3885\n✅ TP1: 3899\n✅ TP2: 3909"
SIGNAL_2 = "💰 Trade idea – GOLD (XAUUSD)\n\n🔴 SELL XAUUSD NOW @ 3928\n\n❌ SL: 3945\n✅ TP1: 3920"
EDITED = "🔥 GOLD TRADE IDEA\n\n🟢 BUY GOLD NOW @ 3896\n\n❌ SL: 3885\n✅ TP1: 3899\n✅ TP2: 3909"


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
    """Offline Telethon stand-in shared by both modules."""

    def __init__(self, rows):
        self.rows = rows
        self.sent, self.edits, self.handlers, self.removed = [], [], [], []
        self.fail_reply_to = {}  # reply_to -> exception raised on every send there
        self.flood_once = False
        self.inflight = []  # formatted.SENDS.inflight seen by each send/edit

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

    async def send_message(self, chat, text, formatting_entities=None, reply_to=None, link_preview=True):
        self.inflight.append(formatted.SENDS.inflight)
        if self.flood_once:
            self.flood_once = False
            raise FloodWaitError(request=None, capture=30)
        if reply_to in self.fail_reply_to:
            raise self.fail_reply_to[reply_to]
        self.sent.append(SimpleNamespace(chat=chat, text=text, reply_to=reply_to, id=50000 + len(self.sent)))
        return self.sent[-1]

    async def edit_message(self, chat, mid, text, formatting_entities=None, link_preview=True):
        self.inflight.append(formatted.SENDS.inflight)
        self.edits.append((mid, text))


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
        self.assertEqual((owner.LIVE_POLL_SECONDS, formatted.POLL_SECONDS), (15, 15))
        self.assertEqual(owner.GIVE_UP_SECONDS, 3600)


class FormattedRouteTests(Base):
    def setUp(self):
        super().setUp()
        topic = formatted.ROUTE["source_topic"]
        self.rows = [post(9000, topic, SIGNAL), post(9001, topic, "✅ TP1 HIT 🔥", parent=9000, minutes=1)]
        self.rows.append(post(9002, topic, SIGNAL_2, minutes=2))
        self.worker = SimpleNamespace(client=Client(self.rows), DATA_DIR=self.tmp.name)
        self.state = {"signals": {}, "thread": {}, "posted": {}, "counts": {}, "checkpoint": 8999}
        self.file = self.path / formatted.STATE_FILE

    async def cycle(self):
        await formatted.cycle(self.worker, self.state, self.file)

    async def test_failing_update_is_retried_then_given_up_and_the_route_continues(self):
        self.worker.client.fail_reply_to[50000] = ConnectionError("network down")
        for attempt in (1, 2):
            with self.assertRaises(ConnectionError):
                await self.cycle()
            self.assertEqual(self.state["checkpoint"], 9000)
            self.assertEqual(self.state["failures"]["9001"]["attempts"], attempt)
        self.assertEqual(formatted.load_state(self.file)["failures"]["9001"]["attempts"], 2)
        self.state["failures"]["9001"]["first"] -= owner.GIVE_UP_SECONDS  # an hour later
        with self.assertLogs(formatted.log, "ERROR") as logs:
            await self.cycle()
        self.assertEqual(
            logs.records[0].getMessage(),
            "[INTAKE GAVE UP] route=Owner Relay8323 To VIP12289 Formatted ids=[9001] attempts=3",
        )
        sent = self.worker.client.sent
        self.assertEqual([s.reply_to for s in sent], [12289, 12289])  # never redirected to the root
        self.assertEqual((self.state["checkpoint"], self.state["failures"]), (9002, {}))
        self.assertEqual(self.state["counts"], {"signal": 2})

    async def test_update_whose_signal_copy_is_gone_posts_to_the_topic_root(self):
        self.worker.client.fail_reply_to[50000] = MsgIdInvalidError(request=None)
        with self.assertLogs(formatted.log, "WARNING") as logs:
            await self.cycle()
        sent = self.worker.client.sent
        self.assertEqual([s.reply_to for s in sent], [12289, 12289, 12289])
        self.assertTrue(sent[1].text.startswith("✅ TP1 HIT"))
        gone = "[OWNER FORMATTED REPLY PARENT GONE] source_id=9001 parent=50000"
        self.assertTrue(any(gone in line for line in logs.output), logs.output)
        self.assertEqual(self.state["counts"], {"signal": 2, "update": 1})
        self.assertEqual((self.state["checkpoint"], self.state.get("failures", {})), (9002, {}))

    def test_reply_parent_gone_matches_class_and_text(self):
        for exc in (
            MsgIdInvalidError(request=None),
            MessageIdInvalidError(request=None),
            Exception("RPCError 400: REPLY_MESSAGE_ID_INVALID (caused by SendMessageRequest)"),
            Exception("RPCError 400: MSG_ID_INVALID"),
        ):
            self.assertTrue(formatted.reply_parent_gone(exc), exc)
        for exc in (ConnectionError("network down"), ChatWriteForbiddenError(request=None)):
            self.assertFalse(formatted.reply_parent_gone(exc), exc)

    async def test_flood_wait_is_slept_through_not_counted(self):
        self.worker.client.flood_once = True
        with patch.object(owner.asyncio, "sleep", return_value=None) as sleep:
            await self.cycle()
        sleep.assert_awaited_once_with(31)
        self.assertEqual((self.state["checkpoint"], self.state.get("failures", {})), (9002, {}))

    async def test_sends_edits_and_their_writes_run_inside_sends(self):
        sends = guard()
        saves, seen = watch_saves(formatted, sends)
        with patch.object(formatted, "SENDS", sends), saves:
            await self.cycle()
            edited = post(9000, formatted.ROUTE["source_topic"], EDITED)
            await formatted.reformat_edited_signal(self.worker, self.state, self.file, edited)
        self.assertEqual(len(self.worker.client.edits), 1)
        self.assertEqual(self.worker.client.inflight, [1, 1, 1, 1])
        self.assertEqual(seen, [1, 1, 1, 1])
        self.assertEqual(sends.inflight, 0)

    async def test_live_loop_clears_strikes_logs_backstop_poll_and_removes_handlers(self):
        policy._STRIKES[policy.identity(formatted.ROUTE)] = [time.time()]
        calls = []

        async def cycle(worker, state, path):
            calls.append(1)
            if len(calls) == 1:  # a new post wakes the reader at once
                callback = worker.client.handlers[0][0]
                await callback(SimpleNamespace(message=post(9100, formatted.ROUTE["source_topic"])))
                return
            raise asyncio.CancelledError

        with patch.object(formatted, "cycle", cycle), self.assertLogs(formatted.log, "WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await formatted.run_owner_formatted_route(self.worker)
        self.assertEqual(len(calls), 2)
        self.assertNotIn(policy.identity(formatted.ROUTE), policy._STRIKES)
        self.assertTrue(any("[OWNER FORMATTED LIVE READY]" in o and "poll=15s" in o for o in logs.output))
        self.assertEqual(len(self.worker.client.handlers), 2)
        self.assertEqual(self.worker.client.removed, self.worker.client.handlers)


if __name__ == "__main__":
    unittest.main()

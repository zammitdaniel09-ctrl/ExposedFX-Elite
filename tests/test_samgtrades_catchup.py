"""SamGtrades relay catch-up (4 Nov 2026): posts missed while the worker was down,
retries, stale and give-up alerts, one set of handlers per retry, Telethon's
album buffer, and sends held open for a graceful shutdown."""

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor
from telegram_worker import samgtrades_relay as relay
from telegram_worker import state_store
from telethon import events

NOW = 1_800_000_000.0
LOGGER = "wholechat-4281603170-to-3743381585"


def post(mid, age=30.0, grouped=None):
    return SimpleNamespace(
        id=mid,
        message=f"Missed post {mid}",
        entities=None,
        reply_to=None,
        media=object() if grouped else None,
        grouped_id=grouped,
        date=datetime.fromtimestamp(NOW - age, timezone.utc),
    )


def isolate_send_guard(test):
    """A fresh SendGuard per test, so no test adds a hook to the real shutdown."""
    guard = state_store.SendGuard()
    guard.registered = True
    patcher = patch.object(relay, "SENDS", guard)
    patcher.start()
    test.addCleanup(patcher.stop)
    return guard


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class FakeRegistry:
    def get(self, alias):
        return {"type": "custom", "document_id": 1}

    def render(self, template):
        return template, []


class FakeClient:
    def __init__(self, posts=()):
        self.source = {p.id: p for p in posts}
        self.sent = []
        self.fail_texts = set()
        self.fail_reads = 0
        self.fail_reload = False
        self.handlers = []
        self.on_send = None
        self.next_id = 9000

    async def get_entity(self, chat):
        return SimpleNamespace(title=str(chat))

    async def get_messages(self, chat, ids=None, limit=None, min_id=0, reverse=False):
        if chat == relay.DEST_CHAT:
            return []
        if self.fail_reads:
            self.fail_reads -= 1
            raise ConnectionError("read failed")
        if ids is not None:
            if self.fail_reload:
                raise RuntimeError("reload failed")
            return [self.source.get(i) for i in ids]
        rows = sorted(self.source.values(), key=lambda m: m.id)
        if reverse:
            return [m for m in rows if m.id > min_id][:limit]
        return list(reversed(rows))[:limit]

    def _accept(self, label):
        if self.on_send:
            self.on_send()
        if label in self.fail_texts:
            raise RuntimeError("SEND_FAILED")
        self.sent.append(label)
        self.next_id += 1
        return SimpleNamespace(id=self.next_id)

    async def send_message(self, chat, text, **kwargs):
        return self._accept(text)

    async def send_file(self, chat, files, caption=None, **kwargs):
        if isinstance(files, list):
            first = self._accept(("album", len(files), caption))
            return [first] + [SimpleNamespace(id=first.id + i) for i in range(1, len(files))]
        return self._accept(caption)

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback, event=None):
        before = len(self.handlers)
        self.handlers = [h for h in self.handlers if h[0] is not callback]
        return before - len(self.handlers)


class CatchUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.map = {}
        self.clock = Clock()
        isolate_send_guard(self)

    def tearDown(self):
        self.tmp.cleanup()

    def catchup(self, client, checkpoint=10):
        async def deliver(unit, reason):
            return await relay._deliver_unit(
                client,
                unit,
                self.map,
                self.data_dir / relay.MAP_FILENAME,
                self.data_dir,
                FakeRegistry(),
                asyncio.Lock(),
                relay.log,
                reason,
            )

        catchup = relay._CatchUp(client, deliver, self.map, self.data_dir, relay.log, clock=self.clock)
        catchup.checkpoint = checkpoint
        return catchup

    def stored(self):
        return relay._load_json(self.data_dir / relay.CATCHUP_FILENAME).get("catchup_last_id")

    def test_sends_post_missed_while_down_and_moves_checkpoint(self):
        self.map["10"] = 500
        client = FakeClient([post(10), post(11), post(12)])
        catchup = self.catchup(client)
        with self.assertLogs(LOGGER, "INFO") as logs:
            asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent, ["Missed post 11", "Missed post 12"])
        self.assertEqual((self.map["11"], self.map["12"]), (9001, 9002))
        self.assertEqual((catchup.checkpoint, self.stored()), (12, 12))
        self.assertTrue(any("[WHOLECHAT CATCHUP SENT] source_ids=[11, 12]" in line for line in logs.output))
        self.assertTrue(any("[WHOLECHAT SENT] reason=catchup" in line for line in logs.output))

    def test_mapped_post_is_not_resent(self):
        self.map.update({"11": 501, "12": [502, 503]})
        client = FakeClient([post(11), post(12)])
        catchup = self.catchup(client)
        asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent, [])
        self.assertEqual(catchup.checkpoint, 12)

    def test_stale_post_is_reported_not_sent(self):
        client = FakeClient([post(11, age=900), post(12)])
        catchup = self.catchup(client)
        with self.assertLogs(LOGGER, "WARNING") as logs:
            asyncio.run(catchup.run_pass())
        line = "WARNING:%s:[GAP STALE NOT SENT] route=SamGtrades To Exposed VIP ids=[11] age=900s max=600s"
        self.assertIn(line % LOGGER, logs.output)
        self.assertTrue(health_monitor.GAP_STALE.search(line))
        self.assertEqual(client.sent, ["Missed post 12"])
        self.assertNotIn("11", self.map)
        self.assertEqual(catchup.checkpoint, 12)

    def test_failing_post_holds_checkpoint_and_is_retried(self):
        client = FakeClient([post(11), post(12)])
        client.fail_texts.add("Missed post 11")
        catchup = self.catchup(client)
        with self.assertLogs(LOGGER, "WARNING") as logs:
            asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent, [])  # 12 waits behind 11: posts stay in order
        self.assertEqual(catchup.checkpoint, 10)
        self.assertIsNone(self.stored())
        self.assertTrue(
            any("[WHOLECHAT CATCHUP RETRY] source_ids=[11] attempts=1" in line for line in logs.output)
        )

        client.fail_texts.clear()
        self.clock.now += 30
        asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent, ["Missed post 11", "Missed post 12"])
        self.assertEqual((catchup.checkpoint, self.stored(), catchup.failures), (12, 12, {}))

    def test_failing_post_is_given_up_after_an_hour(self):
        client = FakeClient([post(11), post(12)])
        client.fail_texts.add("Missed post 11")
        catchup = self.catchup(client)
        # With dated posts the 600 s stale rule fires first; isolate the give-up rule.
        with patch.object(relay, "STALE_SECONDS", 10**9):
            asyncio.run(catchup.run_pass())
            self.clock.now += relay.GIVE_UP_SECONDS - 1
            asyncio.run(catchup.run_pass())
            self.assertEqual((client.sent, catchup.checkpoint), ([], 10))
            self.clock.now += 1
            with self.assertLogs(LOGGER, "ERROR") as logs:
                asyncio.run(catchup.run_pass())
        line = "ERROR:%s:[INTAKE GAVE UP] route=SamGtrades To Exposed VIP ids=[11] attempts=3"
        self.assertEqual(logs.output, [line % LOGGER])
        self.assertTrue(health_monitor.GAVE_UP.search(line))
        self.assertEqual(client.sent, ["Missed post 12"])
        self.assertEqual((catchup.checkpoint, catchup.failures), (12, {}))

    def test_album_younger_than_two_seconds_waits(self):
        client = FakeClient([post(11, age=1, grouped=77), post(12, age=1, grouped=77), post(13, age=0.5)])
        catchup = self.catchup(client)
        asyncio.run(catchup.run_pass())
        self.assertEqual((client.sent, catchup.checkpoint), ([], 10))
        self.clock.now += 5
        asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent, [("album", 2, "Missed post 11"), "Missed post 13"])
        self.assertEqual((self.map["11"], self.map["12"], catchup.checkpoint), (9001, 9002, 13))

    def test_full_page_ending_in_an_album_reads_it_whole_next_pass(self):
        client = FakeClient([post(11), post(12, grouped=5), post(13, grouped=5)])
        catchup = self.catchup(client)
        with patch.object(relay, "CATCHUP_PAGE", 2):
            asyncio.run(catchup.run_pass())
            self.assertEqual((client.sent, catchup.checkpoint), (["Missed post 11"], 11))
            asyncio.run(catchup.run_pass())
        self.assertEqual(client.sent[1], ("album", 2, "Missed post 12"))
        self.assertEqual(catchup.checkpoint, 13)

    def test_checkpoint_starts_at_newest_mapped_post(self):
        self.map.update({"5": 1, "40": 2, "17": [3, 4]})
        client = FakeClient([post(90)])
        catchup = self.catchup(client, checkpoint=None)
        self.assertEqual(asyncio.run(catchup.start()), 40)
        self.assertEqual(self.stored(), 40)
        self.map["41"] = 5
        self.assertEqual(asyncio.run(catchup.start()), 40)  # the stored checkpoint wins

    def test_empty_map_starts_at_newest_post_in_chat(self):
        client = FakeClient([post(88), post(90)])
        catchup = self.catchup(client, checkpoint=None)
        self.assertEqual(asyncio.run(catchup.start()), 90)
        self.assertEqual(self.stored(), 90)

    def test_failing_pass_never_ends_the_loop(self):
        client = FakeClient([post(11)])
        client.fail_reads = 1
        self.map["10"] = 500
        catchup = self.catchup(client, checkpoint=None)
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        with self.assertLogs(LOGGER, "WARNING") as logs:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(catchup.run(sleep=sleep))
        self.assertTrue(any("[WHOLECHAT CATCHUP READY]" in line for line in logs.output))
        self.assertTrue(
            any("[WHOLECHAT CATCHUP PASS FAILED] ConnectionError" in line for line in logs.output)
        )
        self.assertEqual((client.sent, sleeps), (["Missed post 11"], [30, 30]))


class SendGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)
        self.guard = isolate_send_guard(self)

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, client, message_map):
        return relay._deliver_unit(
            client,
            [post(11)],
            message_map,
            self.data_dir / relay.MAP_FILENAME,
            self.data_dir,
            FakeRegistry(),
            asyncio.Lock(),
            relay.log,
            "live",
        )

    def test_send_and_map_write_happen_inside_the_guard(self):
        client, message_map, seen = FakeClient(), {}, []
        client.on_send = lambda: seen.append(self.guard.inflight)
        self.assertEqual(asyncio.run(self.deliver(client, message_map)), "sent")
        self.assertEqual((seen, self.guard.inflight, message_map), ([1], 0, {"11": 9001}))
        self.assertEqual(relay._load_json(self.data_dir / relay.MAP_FILENAME), {"11": 9001})

    def test_no_new_send_starts_while_shutting_down(self):
        client = FakeClient()
        self.guard.closing = True

        async def scenario():
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.deliver(client, {}), 0.05)

        asyncio.run(scenario())
        self.assertEqual(client.sent, [])


class HandlerAndBufferTests(unittest.TestCase):
    def setUp(self):
        isolate_send_guard(self)

    def test_retry_does_not_register_handlers_twice(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        client = FakeClient([post(1)])
        client.fail_reload = True
        main = SimpleNamespace(client=client, EMOJI_REGISTRY=FakeRegistry(), DATA_DIR=Path(tmp.name))
        names = (relay.CATCHUP_TASK_ATTR, relay.ALBUM_SWEEP_TASK_ATTR, "WHOLECHAT_4281603170_RESTYLE_TASK")

        async def scenario():
            # The last-50 import fails after the handlers were added; run_with_retry calls again.
            with self.assertRaises(RuntimeError):
                await relay.run_wholechat_4281603170_to_3743381585(main)
            first_sweeper = getattr(main, relay.ALBUM_SWEEP_TASK_ATTR)
            relay._save_json(Path(tmp.name) / relay.STATE_FILENAME, {"status": "done"})
            relay._save_json(Path(tmp.name) / relay.RESTYLE_FILENAME, {"status": "done"})
            await relay.run_wholechat_4281603170_to_3743381585(main)
            tasks = [first_sweeper] + [getattr(main, name) for name in names]
            try:
                await asyncio.sleep(0)
                self.assertTrue(first_sweeper.cancelled())
                self.assertEqual(
                    [(callback, type(event)) for callback, event in client.handlers],
                    [
                        (getattr(main, relay.NEW_HANDLER_ATTR), events.NewMessage),
                        (getattr(main, relay.ALBUM_HANDLER_ATTR), events.Album),
                    ],
                )
                for callback, event in list(client.handlers):  # what Telethon does with a new post
                    if isinstance(event, events.NewMessage):
                        await asyncio.wait_for(callback(SimpleNamespace(message=post(2))), 1)
                self.assertEqual(client.sent, ["Missed post 2"])
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(scenario())

    def test_album_buffer_sweep_removes_only_expired_entries(self):
        async def scenario():
            now = asyncio.get_running_loop().time()
            albums = {
                1: SimpleNamespace(_due=now - 61),
                2: SimpleNamespace(_due=now - 30),
                3: SimpleNamespace(_due=now + 0.5),
                4: SimpleNamespace(),
            }
            removed = relay._sweep_album_buffer(SimpleNamespace(_albums=albums))
            return removed, sorted(albums), relay._sweep_album_buffer(SimpleNamespace())

        self.assertEqual(asyncio.run(scenario()), (1, [2, 3, 4], 0))


if __name__ == "__main__":
    unittest.main()

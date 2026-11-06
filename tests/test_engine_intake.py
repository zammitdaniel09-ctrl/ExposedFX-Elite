"""Engine intake: pushes, checkpointed watchdog, restart gaps and stale posts (MAIN and ALT)."""

import asyncio
import logging
import sqlite3
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
from telegram_worker.engine import intake

RELAY, VIP = -1004367822325, -1003726286301
SOURCE = (RELAY, 2)
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
OTHER = {**ROUTE, "name": "VIP RELAY Topic2 To MM28", "dest_chat": -1003786932333, "dest_topic": 28}
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)


def post(msg_id, minutes_ago=0.5, grouped_id=None, action=None, seconds_ago=None):
    age = timedelta(seconds=seconds_ago) if seconds_ago is not None else timedelta(minutes=minutes_ago)
    return SimpleNamespace(id=msg_id, grouped_id=grouped_id, action=action, date=NOW - age, reply_to_top_id=2)


class Client:
    """get_messages like Telethon: min_id excluded, reverse = oldest first."""

    def __init__(self, posts):
        self.posts = posts
        self.calls = []
        self.handlers = []

    async def get_messages(self, chat, **kwargs):
        self.calls.append((chat, kwargs))
        newer = sorted((p for p in self.posts if p.id > kwargs.get("min_id", 0)), key=lambda p: p.id)
        if kwargs.get("reverse"):
            return newer[: kwargs["limit"]]
        return list(reversed(newer))[: kwargs["limit"]]

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, type(event).__name__))


class Harness:
    def __init__(
        self,
        posts,
        routes=(ROUTE,),
        results=None,
        stale_after=None,
        pending=None,
        start=0,
        give_up_after=3600,
    ):
        self.client = Client(posts)
        self.now = [NOW.timestamp()]
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.checkpoints = intake.Checkpoints(self.db, clock=lambda: NOW.timestamp())
        self.delivered = []
        self.results = results or {}

        async def deliver(route, unit, reason):
            ids = [m.id for m in unit]
            outcome = self.results.get((route["name"], ids[0]), True)
            if isinstance(outcome, list):
                outcome = outcome.pop(0) if outcome else True
            if isinstance(outcome, Exception):
                raise outcome
            if outcome:
                self.delivered.append((route["name"], ids, reason))
            return outcome

        async def first(source, group):
            return start

        async def no_sleep(seconds):
            await asyncio.sleep(0)

        self.intake = intake.Intake(
            "TEST",
            self.client,
            {SOURCE: list(routes)},
            deliver,
            self.checkpoints,
            lambda message, chat: getattr(message, "reply_to_top_id", None),
            first,
            pending=pending,
            stale_after=stale_after,
            give_up_after=give_up_after,
            album_gather=0.02,
            clock=lambda: self.now[0],
            sleep=no_sleep,
        )

    async def ready(self):
        self.intake.sources[SOURCE] = intake._Source(checkpoint=self.checkpoints.get(SOURCE) or 0)

    async def drain(self):
        state = self.intake.sources[SOURCE]
        worker = asyncio.create_task(self.intake.worker(SOURCE))
        await state.queue.join()
        worker.cancel()

    async def tick(self):
        queued = await self.intake.sweep(SOURCE)
        await self.drain()
        return queued

    def run(self, coro):
        return asyncio.run(coro)


class CheckpointTests(unittest.TestCase):
    def test_checkpoints_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(str(Path(tmp) / "s.sqlite3"), isolation_level=None)
            intake.Checkpoints(db).set(SOURCE, 8522)
            db.close()
            db = sqlite3.connect(str(Path(tmp) / "s.sqlite3"), isolation_level=None)
            self.assertEqual(intake.Checkpoints(db).get(SOURCE), 8522)
            self.assertIsNone(intake.Checkpoints(db).get((RELAY, 106)))
            db.close()

    def test_checkpoint_moves_only_over_handled_posts(self):
        h = Harness([post(10), post(11), post(12)], results={("VIP RELAY Topic2 To VIP1364", 11): [False]})

        async def go():
            await h.ready()
            await h.tick()  # 10 and 12 delivered, 11 failed
            await h.tick()  # checkpoint stops before 11; 11 retried and delivered now
            first = h.checkpoints.get(SOURCE)
            await h.tick()  # the next sweep sees 10-12 handled
            return first, h.checkpoints.get(SOURCE)

        first, after = h.run(go())
        self.assertEqual(first, 10, "a failed post holds the checkpoint, later successes do not skip it")
        self.assertEqual(after, 12)
        self.assertEqual([d[1] for d in h.delivered], [[10], [12], [11]])

    def test_push_success_never_moves_the_checkpoint_past_a_failure(self):
        h = Harness([post(10), post(11)], results={("VIP RELAY Topic2 To VIP1364", 10): [False]})

        async def go():
            await h.ready()
            await h.intake.submit(SOURCE, [post(10)], "telegram_push")
            await h.intake.submit(SOURCE, [post(11)], "telegram_push")
            await h.drain()
            await h.tick()  # watchdog finds 10 unhandled and retries it
            await h.tick()
            return h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), 11)
        self.assertEqual([d[1] for d in h.delivered], [[11], [10]])


class QueueTests(unittest.TestCase):
    def test_push_and_watchdog_never_deliver_the_same_post_twice(self):
        h = Harness([post(10)])

        async def go():
            await h.ready()
            await h.intake.submit(SOURCE, [post(10)], "telegram_push")
            self.assertFalse(await h.intake.submit(SOURCE, [post(10)], "watchdog"), "already queued")
            await h.drain()
            await h.tick()
            self.assertFalse(await h.intake.submit(SOURCE, [post(10)], "telegram_push"), "already handled")

        h.run(go())
        self.assertEqual(h.delivered, [("VIP RELAY Topic2 To VIP1364", [10], "telegram_push")])

    def test_queue_delivers_oldest_first(self):
        h = Harness([])

        async def go():
            await h.ready()
            for msg_id in (14, 12, 13):
                await h.intake.submit(SOURCE, [post(msg_id)], "telegram_push")
            await h.drain()

        h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[12], [13], [14]])

    def test_every_route_of_the_source_must_handle_the_post(self):
        h = Harness([post(10)], routes=(ROUTE, OTHER), results={("VIP RELAY Topic2 To MM28", 10): [False]})

        async def go():
            await h.ready()
            await h.tick()
            await h.tick()
            await h.tick()
            return h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), 10)
        self.assertEqual(
            sorted((d[0], d[2]) for d in h.delivered),
            [
                ("VIP RELAY Topic2 To MM28", "watchdog"),
                ("VIP RELAY Topic2 To VIP1364", "watchdog"),
                ("VIP RELAY Topic2 To VIP1364", "watchdog"),
            ],
            "the route that delivered is asked again; the engine skips it as already copied",
        )

    def test_a_raising_route_is_a_failure_not_a_crash(self):
        h = Harness([post(10)], results={("VIP RELAY Topic2 To VIP1364", 10): [RuntimeError("boom")]})

        async def go():
            await h.ready()
            with self.assertLogs("engine-intake", "ERROR") as logs:
                await h.tick()
            await h.tick()
            await h.tick()
            return logs.output, h.checkpoints.get(SOURCE)

        output, checkpoint = h.run(go())
        self.assertIn("RuntimeError: boom", output[0])
        self.assertEqual(checkpoint, 10)

    def test_post_failing_for_good_is_given_up_and_reported(self):
        h = Harness([post(10), post(11)], results={("VIP RELAY Topic2 To VIP1364", 10): [False] * 5})

        async def go():
            await h.ready()
            with self.assertLogs("engine-intake", "ERROR") as logs:
                for _ in range(4):
                    await h.tick()
                    h.now[0] += 1800  # time-based: after an hour, however often it was retried
            return logs.output, h.checkpoints.get(SOURCE)

        output, checkpoint = h.run(go())
        self.assertTrue(
            any(
                "[INTAKE GAVE UP] route=VIP RELAY Topic2 To VIP1364 ids=[10] attempts=3" in line
                for line in output
            )
        )
        self.assertEqual(checkpoint, 11)

    def test_units_not_started_yet_are_left_to_the_first_sweep(self):
        h = Harness([])
        self.assertFalse(h.run(h.intake.submit(SOURCE, [post(10)], "telegram_push")))


class WatchdogTests(unittest.TestCase):
    def test_reads_only_after_the_checkpoint_oldest_first_and_pages(self):
        posts = [post(i) for i in range(101, 101 + intake.PAGE + 5)]
        h = Harness(posts, start=100)

        async def go():
            h.checkpoints.set(SOURCE, 100)
            await h.ready()
            queued = await h.tick()
            await h.tick()
            return queued

        self.assertEqual(h.run(go()), intake.PAGE + 5)
        self.assertEqual(
            [kwargs["min_id"] for _, kwargs in h.client.calls[:2]],
            [100, 100 + intake.PAGE],
            "two pages, no history",
        )
        self.assertTrue(all(kwargs["reverse"] and kwargs["reply_to"] == 2 for _, kwargs in h.client.calls))
        self.assertEqual(h.checkpoints.get(SOURCE), 100 + intake.PAGE + 5)

    def test_service_messages_and_the_topic_root_are_never_delivered(self):
        h = Harness([post(2), post(10, action=object()), post(11)])

        async def go():
            await h.ready()
            await h.tick()
            await h.tick()
            return h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), 11)
        self.assertEqual([d[1] for d in h.delivered], [[11]])

    def test_album_is_one_unit_and_waits_until_telegram_finished_it(self):
        h = Harness([post(10, grouped_id=7, seconds_ago=1), post(11, grouped_id=7, seconds_ago=1)])

        async def go():
            await h.ready()
            self.assertEqual(await h.tick(), 0, "album still arriving")
            for p in h.client.posts:
                p.date = NOW - timedelta(seconds=5)
            self.assertEqual(await h.tick(), 1)

        h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[10, 11]])

    def test_flood_wait_is_waited_out_and_the_loop_continues(self):
        h = Harness([post(10)])
        waits = []

        class Flood(Exception):
            seconds = 7

        async def flaky(chat, **kwargs):
            if not waits:
                raise Flood("wait")
            return []

        async def sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 3:
                raise asyncio.CancelledError

        h.client.get_messages = flaky
        h.intake.sleep = sleep

        async def go():
            with self.assertLogs("engine-intake", "WARNING"):
                try:
                    await h.intake.run_source(SOURCE)
                except asyncio.CancelledError:
                    pass
            for task in h.intake.tasks:
                task.cancel()

        h.run(go())
        self.assertEqual(waits[:2], [7.0, 30.0])

    def test_a_from_start_route_copies_its_whole_source_oldest_first(self):
        posts = [post(i, minutes_ago=600) for i in (7, 5, 6)]
        h = Harness(posts, routes=({**ROUTE, "from_start": True},), start=7)

        async def stop(seconds):
            await h.intake.sources[SOURCE].queue.join()
            raise asyncio.CancelledError

        h.intake.sleep = stop

        async def go():
            with self.assertLogs("engine-intake", "WARNING") as logs:
                with self.assertRaises(asyncio.CancelledError):
                    await h.intake.run_source(SOURCE)
            for task in h.intake.tasks:
                task.cancel()
            return logs.output

        output = h.run(go())
        self.assertTrue(any("[INTAKE READY]" in line and "checkpoint=0" in line for line in output), output)
        self.assertEqual([d[1] for d in h.delivered], [[5], [6], [7]], "not from the newest post")


class RestartGapTests(unittest.TestCase):
    """Posts that arrived while the worker was down (4 Nov 2026: relay post 8522 was lost)."""

    def test_recent_missed_posts_go_out_in_order_and_stale_ones_are_reported_not_sent(self):
        posts = [post(8522, minutes_ago=100), post(8530, minutes_ago=3), post(8531, minutes_ago=2)]
        h = Harness(posts, stale_after=600, pending=lambda route, unit: True)

        async def go():
            h.checkpoints.set(SOURCE, 8516)
            await h.ready()
            with self.assertLogs("engine-intake", "WARNING") as logs:
                await h.tick()
                await h.tick()
            return logs.output

        output = h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[8530], [8531]])
        stale = [line for line in output if "GAP STALE NOT SENT" in line]
        self.assertEqual(len(stale), 1)
        self.assertIn("route=VIP RELAY Topic2 To VIP1364 ids=[8522] age=6000s max=600s", stale[0])
        self.assertEqual(h.checkpoints.get(SOURCE), 8531)

    def test_old_posts_no_route_would_send_are_passed_silently(self):
        h = Harness([post(8522, minutes_ago=100)], stale_after=600, pending=lambda route, unit: False)

        async def go():
            await h.ready()
            await h.tick()
            await h.tick()
            return h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), 8522)
        self.assertEqual(h.delivered, [])

    def test_alt_has_no_age_limit(self):
        h = Harness([post(8522, minutes_ago=600)])

        async def go():
            await h.ready()
            await h.tick()

        h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[8522]])

    def test_first_start_continues_from_the_old_intake(self):
        client = Client([post(i) for i in range(1, 121)])
        start = intake.first_start_checkpoint
        self.assertEqual(
            asyncio.run(start(client, SOURCE, [0, 0])), 120, "no history: start at the newest post"
        )
        self.assertEqual(asyncio.run(start(client, SOURCE, [118, 115])), 115, "after the oldest handled post")
        self.assertEqual(asyncio.run(start(client, SOURCE, [3])), 70, "never more than one page back")
        self.assertEqual(asyncio.run(start(Client([]), SOURCE, [])), 0)

    def test_last_delivered_is_the_newest_mapped_source_post_of_that_route(self):
        mapping = {
            f"{RELAY}:8513:{VIP}:1364": 1,
            f"{RELAY}:8516:{VIP}:1364": 2,
            f"{RELAY}:9999:{VIP}:7": 3,
            "x:y": 1,
        }
        self.assertEqual(intake.last_delivered(mapping, ROUTE), 8516)

    def test_stale_gap_reaches_saved_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = health_monitor.HealthMonitor("ExposedFX MAIN", tmp)
            monitor.observe(
                logging.WARNING,
                "[GAP STALE NOT SENT] route=VIP RELAY Topic2 To VIP1364 ids=[8522] age=6000s max=600s",
            )
            monitor.observe(logging.ERROR, "[INTAKE GAVE UP] route=ALT RELAY 1 ids=[2290] attempts=120")
            self.assertEqual(len(monitor.outbox), 2)
            self.assertIn("not sent", monitor.outbox[0])
            self.assertIn("[2290]", monitor.outbox[1])


class ReviewFixTests(unittest.TestCase):
    """Fixes from the 4 Nov 2026 independent review."""

    def test_album_item_added_after_delivery_is_sent_on_its_own(self):
        h = Harness([post(11, grouped_id=7, seconds_ago=30), post(12, grouped_id=7, seconds_ago=30)])

        async def go():
            await h.ready()
            await h.intake.submit(SOURCE, [post(11, grouped_id=7), post(12, grouped_id=7)], "telegram_album")
            await h.drain()
            h.client.posts.append(post(13, grouped_id=7, seconds_ago=30))  # Telegram added a third item late
            with self.assertLogs("engine-intake", "WARNING") as logs:
                await h.tick()
            await h.tick()
            return logs.output, h.checkpoints.get(SOURCE)

        output, checkpoint = h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[11, 12], [13]])
        self.assertTrue(any("[INTAKE ALBUM REMAINDER]" in line and "ids=[13]" in line for line in output))
        self.assertEqual(checkpoint, 13)

    def test_post_that_waited_too_long_in_the_queue_is_reported_not_sent(self):
        h = Harness([], stale_after=600, pending=lambda route, unit: True)

        async def go():
            await h.ready()
            await h.intake.submit(
                SOURCE, [post(20, minutes_ago=15)], "telegram_push"
            )  # queued, then a long flood wait
            with self.assertLogs("engine-intake", "WARNING") as logs:
                await h.drain()
            return logs.output

        output = h.run(go())
        self.assertEqual(h.delivered, [])
        self.assertTrue(any("[GAP STALE NOT SENT]" in line and "ids=[20]" in line for line in output))
        self.assertIn(20, h.intake.sources[SOURCE].handled)

    def test_a_failing_stale_check_reports_the_post_instead_of_stalling_the_source(self):
        def broken(route, unit):
            raise ValueError("planner failed")

        h = Harness([post(30, minutes_ago=20), post(31)], stale_after=600, pending=broken)

        async def go():
            await h.ready()
            with self.assertLogs("engine-intake", "WARNING"):
                await h.tick()
            await h.tick()
            return h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), 31)
        self.assertEqual([d[1] for d in h.delivered], [[31]])

    def test_shutdown_drain_starts_nothing_new_and_waits_for_sends_in_flight(self):
        h = Harness([])
        release = asyncio.Event()

        async def slow(route, unit, reason):
            await release.wait()
            h.delivered.append((route["name"], [m.id for m in unit], reason))
            return True

        h.intake.deliver = slow

        async def go():
            await h.ready()
            worker = asyncio.create_task(h.intake.worker(SOURCE))
            await h.intake.submit(SOURCE, [post(40)], "telegram_push")
            await asyncio.sleep(0.01)
            self.assertEqual(h.intake.inflight, 1)
            draining = asyncio.create_task(h.intake.drain(timeout=2.0))
            await asyncio.sleep(0.01)
            self.assertFalse(
                await h.intake.submit(SOURCE, [post(41)], "telegram_push"), "closing: no new work"
            )
            release.set()
            with self.assertLogs("engine-intake", "WARNING"):
                await draining
            worker.cancel()

        h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[40]])
        self.assertEqual(h.intake.inflight, 0)

    def test_queued_work_is_left_for_the_next_start_once_closing(self):
        h = Harness([])

        async def go():
            await h.ready()
            await h.intake.submit(SOURCE, [post(50)], "telegram_push")
            h.intake.closing = True
            await h.drain()

        h.run(go())
        self.assertEqual(h.delivered, [])
        self.assertNotIn(50, h.intake.sources[SOURCE].handled)


class StartTests(unittest.TestCase):
    def test_start_attaches_push_handlers_and_runs_each_source(self):
        h = Harness([post(10)], start=9)

        async def go():
            tasks = h.intake.start()
            for _ in range(20):
                await asyncio.sleep(0)
            for task in list(h.intake.tasks):
                task.cancel()
            return tasks

        h.run(go())
        self.assertEqual(
            [kind for _, kind in h.client.handlers], ["NewMessage"], "albums are gathered from NewMessage"
        )
        self.assertEqual(h.checkpoints.get(SOURCE), 10, "first checkpoint stored, then the gap delivered")
        self.assertEqual(h.delivered, [("VIP RELAY Topic2 To VIP1364", [10], "deployment_gap")])

    def test_push_handlers_route_by_chat_and_topic(self):
        h = Harness([])

        async def go():
            await h.ready()
            await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=post(20)))
            await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=post(21, grouped_id=3)))
            other_topic = post(22)
            other_topic.reply_to_top_id = 106
            await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=other_topic))
            await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=post(24, grouped_id=4)))
            await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=post(23, grouped_id=4)))
            await asyncio.sleep(0.1)
            await h.drain()

        h.run(go())
        self.assertEqual(
            [(d[1], d[2]) for d in h.delivered],
            [([20], "telegram_push"), ([21], "telegram_album"), ([23, 24], "telegram_album")],
        )

    def test_handler_errors_are_logged_not_raised(self):
        h = Harness([])

        async def broken(event):
            raise ValueError("bad update")

        with self.assertLogs("engine-intake", "ERROR"):
            asyncio.run(h.intake._safe(broken)(SimpleNamespace(chat_id=RELAY)))

    def test_routes_group_by_source(self):
        groups = intake.group_by_source([ROUTE, OTHER, {**ROUTE, "source_topic": None, "name": "whole"}])
        self.assertEqual(
            {k: [r["name"] for r in v] for k, v in groups.items()},
            {
                (RELAY, 2): ["VIP RELAY Topic2 To VIP1364", "VIP RELAY Topic2 To MM28"],
                (RELAY, None): ["whole"],
            },
        )


class AlbumGatherTests(unittest.TestCase):
    """Albums are gathered from NewMessage pushes; a newer post never overtakes one."""

    def push(self, h, *messages):
        async def go():
            for message in messages:
                await h.intake.on_message(SimpleNamespace(chat_id=RELAY, message=message))

        return go()

    def test_a_post_right_after_an_album_waits_for_the_album(self):
        h = Harness([])

        async def go():
            await h.ready()
            worker = asyncio.create_task(h.intake.worker(SOURCE))
            await self.push(h, post(10, grouped_id=7), post(11, grouped_id=7))
            await self.push(h, post(13))  # the writer is idle and takes it at once
            await asyncio.sleep(0.005)
            await self.push(h, post(12, grouped_id=7))  # Telegram is still adding album items
            await asyncio.sleep(0.15)
            await h.intake.sources[SOURCE].queue.join()
            worker.cancel()

        h.run(go())
        self.assertEqual(
            [(d[1], d[2]) for d in h.delivered],
            [([10, 11, 12], "telegram_album"), ([13], "telegram_push")],
            "album first, as one post, in id order",
        )
        self.assertEqual(h.intake.sources[SOURCE].albums, {}, "nothing kept once the album is queued")
        self.assertEqual(h.intake.album_tasks, set(), "gather tasks are referenced until done")

    def test_an_older_post_is_not_held_by_a_newer_album(self):
        h = Harness([])

        async def go():
            await h.ready()
            worker = asyncio.create_task(h.intake.worker(SOURCE))
            await self.push(h, post(20, grouped_id=8), post(19))
            await asyncio.sleep(0.005)
            self.assertEqual(h.delivered[0][1], [19])
            await asyncio.sleep(0.1)
            await h.intake.sources[SOURCE].queue.join()
            worker.cancel()

        h.run(go())
        self.assertEqual([d[1] for d in h.delivered], [[19], [20]])

    def test_album_items_are_not_gathered_before_start_or_while_closing(self):
        h = Harness([])

        async def go():
            await self.push(h, post(30, grouped_id=9))  # source not started: the first sweep reads it
            await h.ready()
            h.intake.closing = True
            await self.push(h, post(31, grouped_id=9))
            return dict(h.intake.sources[SOURCE].albums)

        self.assertEqual(h.run(go()), {})

    def test_a_failing_album_submit_is_logged_and_releases_the_writer(self):
        h = Harness([])

        async def broken_submit(source, unit, reason):
            raise RuntimeError("queue gone")

        async def go():
            await h.ready()
            h.intake.submit = broken_submit
            with self.assertLogs("engine-intake", "WARNING") as logs:
                await self.push(h, post(40, grouped_id=10))
                await asyncio.sleep(0.1)
            state = h.intake.sources[SOURCE]
            return logs.output, state.albums, state.album_queued.is_set()

        output, albums, released = h.run(go())
        self.assertIn("[INTAKE ALBUM GATHER FAILED]", output[0])
        self.assertEqual((albums, released), ({}, True))

    def test_waiting_for_an_album_is_bounded(self):
        h = Harness([])
        h.intake.album_gather = 0.0

        async def go():
            await h.ready()
            state = h.intake.sources[SOURCE]
            state.albums[5] = intake._Album(items={1: post(1)}, due=float("inf"))  # never completes
            started = asyncio.get_running_loop().time()
            held = await h.intake._older_album_gathering(state, 2)
            return held, asyncio.get_running_loop().time() - started

        held, waited = h.run(go())
        self.assertTrue(held)
        self.assertLess(waited, 1.5)

    def test_failure_counters_are_dropped_once_the_checkpoint_passes_them(self):
        h = Harness([post(50), post(51)], results={("VIP RELAY Topic2 To VIP1364", 50): [False]})

        async def go():
            await h.ready()
            await h.tick()
            h.client.posts = [post(51), post(52)]  # 50 was deleted before it succeeded
            await h.tick()
            await h.tick()
            state = h.intake.sources[SOURCE]
            return state.attempts, state.first_failure, h.checkpoints.get(SOURCE)

        self.assertEqual(h.run(go()), ({}, {}, 52))


if __name__ == "__main__":
    unittest.main()

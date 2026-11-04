"""Ops modules: watchdog thread, markers, heartbeat pacing, log formats, monitor run loop, store edges."""

import asyncio
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor as hm
from telegram_worker import liveness, log_setup
from telegram_worker.state_store import SQLiteMap


class FakeTime:
    """Stands in for the time module inside liveness: sleeping only advances the clock."""

    def __init__(self, start=1000.0):
        self.now, self.sleeps = start, []

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000.0

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class BrokenFlush(logging.Handler):
    def emit(self, record):
        pass

    def flush(self):
        raise OSError("stdout closed")


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_blocked_event_loop_is_caught_by_the_watchdog_thread(self):
        fake, exits, threads = FakeTime(), [], []

        class Recording(threading.Thread):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                threads.append(self)

        dog = liveness.LoopWatchdog(
            "ExposedFX TEST",
            self.tmp.name,
            logging.getLogger("liveness-cov"),
            stall_seconds=12,
            clock=fake.monotonic,
            exit_process=exits.append,
        )

        async def scenario():
            with patch.object(liveness, "time", fake), patch.object(
                liveness, "threading", types.SimpleNamespace(Thread=Recording)
            ):
                self.assertIs(dog.start(), dog)
                threads[0].join(5)  # the event loop is blocked while we wait: a real stall
            await asyncio.sleep(0)  # the loop turns again and the tick task records it
            dog.task.cancel()

        broken = BrokenFlush()
        logging.getLogger().addHandler(broken)
        try:
            with self.assertLogs("liveness-cov", "CRITICAL") as logs:
                asyncio.run(scenario())
        finally:
            logging.getLogger().removeHandler(broken)
        watcher = threads[0]
        self.assertEqual(
            (watcher.name, watcher.daemon, watcher.is_alive()), ("liveness-watchdog", True, False)
        )
        self.assertEqual(fake.sleeps, [liveness.TICK_SECONDS] * 3, "polls every tick until the stall limit")
        self.assertEqual(exits, [liveness.EXIT_STALLED], "a handler that cannot flush does not stop the exit")
        self.assertIn("[LIVENESS STALL] service=ExposedFX TEST loop_stalled=15s", logs.output[0])
        self.assertEqual(dog.last_tick, fake.now)
        marker = liveness.take_marker(self.tmp.name)
        self.assertEqual(
            marker,
            {"service": "ExposedFX TEST", "reason": "event loop stalled for 15s", "ts": 1_800_000_000.0},
        )

    def test_marker_problems_never_raise(self):
        missing = Path(self.tmp.name) / "gone"
        liveness.write_marker(missing, "svc", "event loop stalled for 130s")
        self.assertIsNone(liveness.take_marker(missing), "an unwritable volume just loses the notice")
        marker_path = Path(self.tmp.name) / liveness.MARKER_FILE
        marker_path.write_text("[1, 2]", encoding="utf-8")
        self.assertIsNone(liveness.take_marker(self.tmp.name), "not a marker object")
        self.assertFalse(marker_path.exists(), "and it is cleared")

    def test_marker_that_cannot_be_cleared_is_kept_for_the_next_start(self):
        liveness.write_marker(self.tmp.name, "svc", "event loop stalled for 130s")
        with patch.object(Path, "unlink", side_effect=PermissionError("file in use")):
            first = liveness.take_marker(self.tmp.name)
        self.assertEqual(first["reason"], "event loop stalled for 130s")
        self.assertEqual(liveness.take_marker(self.tmp.name), first)
        self.assertIsNone(liveness.take_marker(self.tmp.name))

    def test_failed_pings_are_logged_at_1_10_and_every_60th_and_reset_on_success(self):
        state = {"fail": True}

        def fetch(url):
            if state["fail"]:
                raise OSError("unreachable")

        hb = liveness.Heartbeat(
            "https://hc.example/ping",
            healthy=lambda: True,
            logger=logging.getLogger("heartbeat-cov"),
            fetch=fetch,
        )

        async def beats(count):
            return [await hb.beat_once() for _ in range(count)]

        with self.assertLogs("heartbeat-cov", "WARNING") as logs:
            self.assertEqual(set(asyncio.run(beats(60))), {False})
        self.assertEqual([record.args[0] for record in logs.records], [1, 10, 60])
        state["fail"] = False
        self.assertTrue(asyncio.run(hb.beat_once()))
        self.assertEqual(hb.failures, 0)

    def test_default_ping_reads_the_url_and_raises_when_unreachable(self):
        target = Path(self.tmp.name) / "ping"
        target.write_text("OK", encoding="utf-8")
        hb = liveness.Heartbeat(
            target.as_uri(), healthy=lambda: True, logger=logging.getLogger("heartbeat-cov")
        )
        self.assertIs(hb.fetch, liveness._get)
        self.assertTrue(asyncio.run(hb.beat_once()))
        with self.assertRaises(OSError):
            liveness._get((Path(self.tmp.name) / "missing").as_uri())


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        root = logging.getLogger()
        self.saved = (root.handlers[:], root.level)

    def tearDown(self):
        root = logging.getLogger()
        root.handlers[:] = self.saved[0]
        root.setLevel(self.saved[1])

    def emit(self, env):
        out = io.StringIO()
        with patch.dict(os.environ):
            for name in ("RAILWAY_ENVIRONMENT", "LOG_FORMAT", "LOG_LEVEL"):
                os.environ.pop(name, None)
            os.environ.update(env)
            with redirect_stdout(out):
                log_setup.configure_logging("imperium-telegram-worker")
                log_setup.configure_logging("imperium-telegram-worker")  # reconfiguring never duplicates
                logger = logging.getLogger("cov-log-setup")
                logger.debug("debug detail")
                logger.warning("[FASTVIP COPIED] route=%s", "Relay508 To VIP7")
        self.assertEqual(len(logging.getLogger().handlers), 1)
        return out.getvalue().splitlines()

    def test_local_runs_keep_the_plain_text_format_at_info(self):
        lines = self.emit({})
        self.assertEqual(len(lines), 1, "debug is below the default INFO level")
        self.assertRegex(lines[0], r"^\d\d:\d\d:\d\d \[WARNING\] \[FASTVIP COPIED\] route=Relay508 To VIP7$")

    def test_json_format_and_level_come_from_the_environment(self):
        lines = [json.loads(line) for line in self.emit({"LOG_FORMAT": "json", "LOG_LEVEL": "debug"})]
        self.assertEqual(
            lines,
            [
                {
                    "level": "debug",
                    "message": "debug detail",
                    "logger": "cov-log-setup",
                    "service": "imperium-telegram-worker",
                },
                {
                    "level": "warn",
                    "message": "[FASTVIP COPIED] route=Relay508 To VIP7",
                    "logger": "cov-log-setup",
                    "service": "imperium-telegram-worker",
                },
            ],
        )

    def test_railway_always_logs_json(self):
        lines = self.emit({"RAILWAY_ENVIRONMENT": "production"})
        self.assertEqual(json.loads(lines[0])["level"], "warn")


class HealthMonitorEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 5, 9, 0, tzinfo=hm.TZ).timestamp()

    def tearDown(self):
        self.tmp.cleanup()

    def monitor(self, data_dir=None):
        monitor = hm.HealthMonitor("MAIN", data_dir or self.tmp.name, clock=lambda: self.now)
        monitor.state["last_digest_day"] = "2026-11-04"  # today's digest is due
        return monitor

    def run_one_pass(self, monitor, client):
        passes = []

        async def one_pass(seconds):
            passes.append(seconds)
            if len(passes) > 1:
                raise asyncio.CancelledError

        with patch.object(hm.asyncio, "sleep", one_pass), self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor.run(client))

    def saved_state(self):
        return json.loads((Path(self.tmp.name) / hm.STATE_FILE).read_text(encoding="utf-8"))

    def test_digest_is_sent_then_counters_reset_and_saved(self):
        sent = []

        class Client:
            async def send_message(self, peer, text, **kwargs):
                sent.append((peer, text, kwargs))

        monitor = self.monitor()
        monitor.observe(logging.WARNING, "[WHOLECHAT SENT] reason=live source=-1 dest=-2 source_ids=[1]")
        self.run_one_pass(monitor, Client())
        self.assertEqual(len(sent), 1)
        peer, text, kwargs = sent[0]
        self.assertEqual((peer, kwargs), ("me", {"parse_mode": None, "link_preview": False}))
        self.assertIn("• -2: 1", text)
        self.assertEqual((monitor.state["routes"], monitor.state["restarts"]), ({}, 0))
        self.assertEqual(self.saved_state()["last_digest_day"], "2026-11-05")

    def test_digest_that_cannot_be_sent_keeps_the_counters(self):
        class Client:
            async def send_message(self, peer, text, **kwargs):
                raise ConnectionError("offline")

        monitor = self.monitor()
        monitor.observe(logging.WARNING, "[WHOLECHAT SENT] reason=live source=-1 dest=-2 source_ids=[1]")
        with self.assertLogs("health-monitor", "WARNING") as logs:
            self.run_one_pass(monitor, Client())
        self.assertIn("[HEALTH DIGEST NOT SENT] ConnectionError: offline", logs.output[0])
        self.assertEqual(monitor.state["routes"]["-2"]["ok"], 1)
        saved = self.saved_state()
        self.assertEqual((saved["last_digest_day"], saved["routes"]["-2"]["ok"]), ("2026-11-04", 1))

    def test_cancel_during_a_send_saves_state_and_keeps_the_alert(self):
        class Client:
            async def send_message(self, peer, text, **kwargs):
                raise asyncio.CancelledError  # the task is cancelled while this send is in flight

        monitor = self.monitor()
        monitor.outbox.append("⛔ MAIN: route stopped")
        self.run_one_pass(monitor, Client())
        self.assertEqual(list(monitor.outbox), ["⛔ MAIN: route stopped"])
        self.assertEqual(self.saved_state()["restarts"], 1)

    def test_cancel_while_waiting_saves_the_counters(self):
        monitor = self.monitor()
        monitor.observe(logging.WARNING, "[WHOLECHAT SENT] reason=live source=-1 dest=-2 source_ids=[1]")

        async def cancelled(seconds):
            raise asyncio.CancelledError  # the worker stops while the monitor sleeps

        with patch.object(hm.asyncio, "sleep", cancelled), self.assertRaises(asyncio.CancelledError):
            asyncio.run(monitor.run(object()))
        self.assertEqual(self.saved_state()["routes"]["-2"]["ok"], 1)

    def test_traceback_without_a_marker_still_alerts(self):
        monitor = self.monitor()
        monitor.observe(logging.ERROR, "Task exception was never retrieved", has_exc=True)
        self.assertEqual(len(monitor.outbox), 1)
        self.assertIn("Task exception was never retrieved", monitor.outbox[0])

    def test_state_that_cannot_be_saved_is_reported_not_raised(self):
        monitor = self.monitor(Path(self.tmp.name) / "missing")
        with self.assertLogs("health-monitor", "WARNING") as logs:
            monitor.save()
        self.assertIn("[HEALTH MONITOR SAVE FAILED] FileNotFoundError", logs.output[0])
        self.assertEqual(monitor.last_save, 0.0)

    def test_unmarked_lines_and_failing_extras_are_ignored(self):
        monitor = self.monitor()
        monitor.observe(logging.INFO, "Connection to 149.154.167.51:443/TcpFull complete!")
        self.assertEqual((monitor.state["routes"], list(monitor.outbox)), ({}, []))
        monitor.extras.append(lambda: 1 / 0)
        monitor.extras.append(lambda: "Engine audit: clean")
        digest = monitor.digest().splitlines()
        self.assertEqual(digest[-2:], ["No deliveries recorded.", "Engine audit: clean"])

    def test_log_handler_never_lets_a_monitor_error_reach_the_caller(self):
        class Exploding:
            def observe(self, *args):
                raise RuntimeError("bad pattern")

        handler = hm._LogHandler(Exploding())
        record = logging.LogRecord(
            "imperium-worker", logging.WARNING, __file__, 1, "[FASTVIP COPIED]", (), None
        )
        self.assertIsNone(handler.emit(record))

    def test_install_without_a_restart_marker_queues_nothing(self):
        async def install():
            root = logging.getLogger()
            before = list(root.handlers)
            with self.assertLogs("health-monitor", "WARNING") as logs:
                monitor = hm.install_health_monitor("ExposedFX TEST", object(), self.tmp.name)
            added = [h for h in root.handlers if h not in before]
            for handler in added:
                root.removeHandler(handler)
            monitor.task.cancel()
            return monitor, added, logs.output

        monitor, added, output = asyncio.run(install())
        self.assertEqual(list(monitor.outbox), [])
        self.assertEqual([type(h).__name__ for h in added], ["_LogHandler"])
        self.assertIs(added[0].monitor, monitor)
        self.assertIn(
            "[HEALTH MONITOR READY] service=ExposedFX TEST alerts=saved_messages digest=08:00", output[0]
        )


class StateStoreEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.json = self.dir / "message_map.json"
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def open(self, **kwargs):
        store = SQLiteMap(self.dir / "message_map.sqlite3", **kwargs)
        self.stores.append(store)
        return store

    def test_half_written_json_map_is_reported_and_not_imported(self):
        self.json.write_text('{"a:1:b:2": 7, "a:2', encoding="utf-8")
        with self.assertLogs("state-store", "WARNING") as logs:
            store = self.open(legacy_json=self.json)
        self.assertIn("[STATE STORE LEGACY MAP UNREADABLE] JSONDecodeError", logs.output[0])
        self.assertEqual((store.imported, len(store)), (0, 0))

    def test_missing_keys_raise_like_a_dict(self):
        store = self.open()
        store["a:1:b:2"] = [7, 8]
        with self.assertRaises(KeyError):
            del store["a:9:b:2"]
        with self.assertRaises(KeyError):
            store.pop("a:9:b:2")
        self.assertEqual(store.pop("a:1:b:2"), [7, 8])
        self.assertNotIn("a:1:b:2", store)

    def test_snapshot_needs_a_target(self):
        self.assertFalse(self.open().snapshot_to(force=True))

    def test_second_snapshot_never_starts_while_one_is_running(self):
        store = self.open(legacy_json=self.json)
        store["a:1:b:2"] = 7
        gate = threading.Event()
        write = store._write_snapshot

        def slow_write(target):
            gate.wait(5)
            write(target)

        store._write_snapshot = slow_write
        self.assertTrue(store.snapshot_to(force=True))
        self.assertFalse(store.snapshot_to(force=True), "one writer at a time")
        gate.set()
        store.wait_snapshot()
        self.assertEqual(json.loads(self.json.read_text(encoding="utf-8")), {"a:1:b:2": 7})

    def test_failed_snapshot_is_logged_and_leaves_no_file(self):
        store = self.open()
        store["a:1:b:2"] = 7
        target = self.dir / "missing" / "message_map.json"
        with self.assertLogs("state-store", "WARNING") as logs:
            self.assertTrue(store.snapshot_to(target, force=True))
            store.wait_snapshot()
        self.assertTrue(re.search(r"\[STATE STORE SNAPSHOT FAILED\] FileNotFoundError", logs.output[0]))
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()

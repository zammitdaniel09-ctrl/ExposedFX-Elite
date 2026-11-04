"""Iteration 4 ops: liveness exits, restart notice, JSON logs, instant lock takeover."""

import asyncio
import json
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
try:
    import requests  # noqa: F401  (CI installs only telethon)
except ImportError:
    sys.modules["requests"] = types.ModuleType("requests")
from telegram_worker import health_monitor, liveness, log_setup, runtime_guard


class LoopWatchdogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = [100.0]
        self.exits = []
        self.dog = liveness.LoopWatchdog(
            "ExposedFX TEST",
            self.tmp.name,
            logging.getLogger("t"),
            stall_seconds=120,
            clock=lambda: self.now[0],
            exit_process=self.exits.append,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_healthy_loop_is_left_alone(self):
        self.now[0] += 119
        self.assertFalse(self.dog.check())
        self.assertEqual(self.exits, [])

    def test_stalled_loop_exits_non_zero_and_leaves_a_marker(self):
        self.now[0] += 121
        self.assertTrue(self.dog.check())
        self.assertEqual(self.exits, [liveness.EXIT_STALLED])
        marker = liveness.take_marker(self.tmp.name)
        self.assertIn("stalled", marker["reason"])
        self.assertIsNone(liveness.take_marker(self.tmp.name), "marker is consumed once")

    def test_lost_connection_exits_non_zero(self):
        with self.assertRaises(SystemExit) as caught:
            liveness.exit_disconnected("ExposedFX TEST", self.tmp.name, logging.getLogger("t"))
        self.assertEqual(caught.exception.code, liveness.EXIT_DISCONNECTED)
        self.assertNotEqual(caught.exception.code, 0, "exit 0 would not be restarted by Railway")
        self.assertIn("connection", liveness.take_marker(self.tmp.name)["reason"])


class RestartNoticeTests(unittest.TestCase):
    def test_next_start_tells_the_owner_why_it_restarted(self):
        with tempfile.TemporaryDirectory() as tmp:
            liveness.write_marker(tmp, "ExposedFX TEST", "event loop stalled for 130s")

            async def run():
                monitor = health_monitor.install_health_monitor("ExposedFX TEST", object(), tmp)
                monitor.task.cancel()
                logging.getLogger().removeHandler(logging.getLogger().handlers[-1])
                return list(monitor.outbox)

            outbox = asyncio.run(run())
            self.assertEqual(len(outbox), 1)
            self.assertIn("restarted itself", outbox[0])
            self.assertIn("event loop stalled for 130s", outbox[0])


class DigestTests(unittest.TestCase):
    def test_engine_lag_and_extra_lines_reach_the_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = health_monitor.HealthMonitor("ExposedFX TEST", tmp)
            monitor.observe(
                logging.WARNING,
                "[FASTVIP COPIED] route=Relay7248 To VIP10877 source=-1004367822325_7248 "
                "ids=[1] kind=SINGLE dest=-1003726286301_10877 engine=True lag=0.8s",
            )
            monitor.observe(
                logging.WARNING,
                "[FASTVIP COPIED] route=Relay7248 To VIP10877 source=-1004367822325_7248 "
                "ids=[2] kind=SINGLE dest=-1003726286301_10877",
            )
            monitor.extras.append(lambda: "Engine shadow (match/seen): Relay7248 To VIP10877: 2/2")
            digest = monitor.digest()
            self.assertIn("• Relay7248 To VIP10877: 2 · 0.8s", digest)
            self.assertIn("Engine shadow (match/seen): Relay7248 To VIP10877: 2/2", digest)


class JsonLogTests(unittest.TestCase):
    def test_railway_line_has_real_severity_and_original_text(self):
        formatter = log_setup.RailwayJsonFormatter("imperium-telegram-worker")
        record = logging.LogRecord(
            "imperium-worker",
            logging.WARNING,
            __file__,
            1,
            "[FASTVIP COPIED] route=%s",
            ("Relay508 To VIP7",),
            None,
        )
        line = json.loads(formatter.format(record))
        self.assertEqual(line["level"], "warn")
        self.assertEqual(line["message"], "[FASTVIP COPIED] route=Relay508 To VIP7")
        self.assertEqual(line["service"], "imperium-telegram-worker")

    def test_traceback_stays_in_one_json_line(self):
        formatter = log_setup.RailwayJsonFormatter("svc")
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
        text = formatter.format(record)
        self.assertEqual(text.count("\n"), 0)
        self.assertIn("ValueError: boom", json.loads(text)["message"])


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lock = Path(self.tmp.name) / "svc.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_lock_from_an_earlier_railway_deployment_is_taken_over_at_once(self):
        self.lock.write_text(json.dumps({"pid": 1, "deployment": "old-deploy"}), encoding="utf-8")
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "new-deploy"}):
            self.assertTrue(runtime_guard._previous_holder_is_gone(self.lock))
        self.assertEqual(runtime_guard._lock_pid(self.lock), 1)

    def test_legacy_pid_only_lock_waits_for_two_missed_heartbeats(self):
        self.lock.write_text("1", encoding="utf-8")
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "new-deploy"}):
            self.assertFalse(runtime_guard._previous_holder_is_gone(self.lock, age=6.0))
            self.assertTrue(runtime_guard._previous_holder_is_gone(self.lock, age=12.0))
        self.assertEqual(runtime_guard._lock_pid(self.lock), 1)

    def test_outside_railway_the_lock_is_always_respected(self):
        self.lock.write_text(json.dumps({"pid": 1, "deployment": "old-deploy"}), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(runtime_guard._previous_holder_is_gone(self.lock))

    def test_start_skips_the_45s_wait_after_a_railway_redeploy(self):
        self.lock.write_text(json.dumps({"pid": 1, "deployment": "old-deploy"}), encoding="utf-8")

        async def run():
            with patch.object(runtime_guard, "DATA_DIR", Path(self.tmp.name)), patch.dict(
                os.environ, {"RAILWAY_DEPLOYMENT_ID": "new-deploy"}
            ):
                started = asyncio.get_running_loop().time()
                task = await asyncio.wait_for(runtime_guard.start_runtime_guard("svc"), timeout=5)
                elapsed = asyncio.get_running_loop().time() - started
                if isinstance(task, asyncio.Task):
                    task.cancel()
                return elapsed

        self.assertLess(asyncio.run(run()), 2.0)
        self.assertEqual(json.loads(self.lock.read_text(encoding="utf-8"))["deployment"], "new-deploy")


if __name__ == "__main__":
    unittest.main()

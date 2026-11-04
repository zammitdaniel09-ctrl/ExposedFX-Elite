"""Dead-man's-switch heartbeat, per-deploy replay audit and its mismatch alert."""

import asyncio
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
from telegram_worker import health_monitor, liveness
from telegram_worker.engine import audit

LOG = logging.getLogger("heartbeat-test")


class HeartbeatTests(unittest.TestCase):
    def beat(self, healthy, fetch):
        hb = liveness.Heartbeat(
            "https://hc.example/ping/abc", healthy=lambda: healthy, logger=LOG, fetch=fetch
        )
        return hb, asyncio.run(hb.beat_once())

    def test_pings_only_while_connected(self):
        calls = []
        _, ok = self.beat(True, calls.append)
        self.assertEqual((ok, calls), (True, ["https://hc.example/ping/abc"]))
        calls.clear()
        _, ok = self.beat(False, calls.append)
        self.assertEqual(
            (ok, calls), (False, []), "a disconnected worker must stop pinging so the monitor alerts"
        )

    def test_failed_ping_is_counted_and_never_raises(self):
        def broken(url):
            raise OSError("network down")

        with self.assertLogs("heartbeat-test", "WARNING"):
            hb, ok = self.beat(True, broken)
        self.assertEqual((ok, hb.failures), (False, 1))

    def test_disabled_without_a_url(self):
        async def run():
            with patch.dict(os.environ, {"HEARTBEAT_URL": ""}):
                return liveness.start_heartbeat(
                    "ExposedFX TEST", types.SimpleNamespace(is_connected=lambda: True), LOG
                )

        self.assertIsNone(asyncio.run(run()))

    def test_runs_in_the_background_when_configured(self):
        async def run():
            with patch.dict(os.environ, {"HEARTBEAT_URL": "https://hc.example/ping/abc"}):
                hb = liveness.start_heartbeat(
                    "ExposedFX TEST", types.SimpleNamespace(is_connected=lambda: False), LOG
                )
            await asyncio.sleep(0)
            hb.task.cancel()
            return hb

        self.assertEqual(asyncio.run(run()).interval, 60)


class PerDeployAuditTests(unittest.TestCase):
    def test_every_deployment_gets_its_own_audit(self):
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "deploy-a"}):
            first = audit.audit_key(2)
        with patch.dict(os.environ, {"RAILWAY_DEPLOYMENT_ID": "deploy-b"}):
            second = audit.audit_key(2)
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("2:"))

    def test_mismatch_alerts_the_owner_and_a_clean_audit_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = health_monitor.HealthMonitor("ExposedFX MAIN", tmp)
            monitor.observe(
                logging.INFO,
                "[ENGINE REPLAY AUDIT] route=Relay7248 To VIP10877 checked=25 match=25 mismatch=0",
            )
            self.assertEqual(len(monitor.outbox), 0)
            monitor.observe(
                logging.WARNING,
                "[ALT ENGINE REPLAY AUDIT] route=ALT RELAY 1 checked=24 match=22 mismatch=2 detail={}",
            )
            self.assertEqual(len(monitor.outbox), 1)
            self.assertIn("2 of 24", monitor.outbox[0])


if __name__ == "__main__":
    unittest.main()

"""Health monitor: classifies the workers' real log lines, rate-limits alerts, builds the digest."""
import asyncio
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor as hm

# Real lines from production logs (3-4 Nov 2026).
FORMATTED = "[OWNER FORMATTED POST] contract=-1004367822325:8323:-1003726286301:12289 source_id=8519 outcome=update lag=0.7s"
SKIPPED = "[OWNER FORMATTED POST] contract=-1004367822325:8323:-1003726286301:12289 source_id=8511 outcome=skipped lag=1.4s"
FASTVIP_OK = "[FASTVIP COPIED] route=Relay508 To VIP7 source=-1004367822325_508 ids=[8170] kind=SINGLE"
FASTVIP_FAIL = ("[FASTVIP DEST FAILED] route=Private Live 3838973021 To 9 ids=[24298] dest=-1003726286301_9 "
                "RuntimeError: VIP priority copy returned no destination message")
WHOLECHAT_OK = "[WHOLECHAT SENT] reason=live source=-1004281603170 dest=-1003743381585 source_ids=[2047]"
ALT_OK = "[ALT COPIED] route=NewAccount Select7194 To Relay8323 source_msg=8106 dest_msg=8517 reason=telegram_push"
ALT_FAIL = "[ALT COPY FAILED] route=ALT RELAY 1 ids=[2282, 2283] ChatWriteForbiddenError: no rights"
STOPPED = ("[OWNER ROUTE STOPPED] route=Owner VIP1364 To Partner28 contract=-1003726286301:1364:-1003786932333:28 "
           "reason=ChannelPrivateError RETRY=False NOT_DELIVERED=True")
FLOOD = "Sleeping for 75s (0:01:15) on GetHistoryRequest flood wait"
SMALL_FLOOD = "Sleeping for 3s (0:00:03) on GetRepliesRequest flood wait"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = Clock(datetime(2026, 11, 4, 12, 0, tzinfo=hm.TZ).timestamp())
        self.monitor = hm.HealthMonitor("MAIN", self.tmp.name, clock=self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, *lines, level=logging.WARNING, has_exc=False):
        for line in lines:
            self.monitor.observe(level, line, has_exc)

    def test_successes_counted_per_route_with_lag(self):
        self.feed(FORMATTED, FORMATTED, SKIPPED, FASTVIP_OK, WHOLECHAT_OK, ALT_OK)
        routes = self.monitor.state["routes"]
        formatted = routes["-1004367822325:8323:-1003726286301:12289"]
        self.assertEqual((formatted["ok"], formatted["lag_n"]), (2, 2))
        self.assertEqual(routes["Relay508 To VIP7"]["ok"], 1)
        self.assertEqual(routes["-1003743381585"]["ok"], 1)
        self.assertEqual(routes["NewAccount Select7194 To Relay8323"]["ok"], 1)
        self.assertEqual(list(self.monitor.outbox), [])

    def test_failures_alert_only_after_three_in_ten_minutes_and_reset_on_success(self):
        self.feed(FASTVIP_FAIL, FASTVIP_FAIL)
        self.assertEqual(len(self.monitor.outbox), 0)
        self.feed("[FASTVIP COPIED] route=Private Live 3838973021 To 9 source=-1003838973021_None ids=[1]")
        self.feed(FASTVIP_FAIL, FASTVIP_FAIL)
        self.assertEqual(len(self.monitor.outbox), 0)
        self.clock.now += 700
        self.feed(FASTVIP_FAIL, FASTVIP_FAIL, FASTVIP_FAIL)
        self.assertEqual(len(self.monitor.outbox), 1)
        self.assertIn("Private Live 3838973021 To 9", self.monitor.outbox[0])
        self.assertIn("RuntimeError", self.monitor.outbox[0])

    def test_alerts_are_rate_limited(self):
        self.feed(ALT_FAIL, ALT_FAIL, ALT_FAIL, ALT_FAIL, ALT_FAIL)
        self.assertEqual(len(self.monitor.outbox), 1)
        self.clock.now += hm.ALERT_COOLDOWN_SECONDS + 1
        self.feed(ALT_FAIL, ALT_FAIL, ALT_FAIL)
        self.assertEqual(len(self.monitor.outbox), 2)

    def test_stopped_route_and_long_flood_alert_immediately(self):
        self.feed(STOPPED, SMALL_FLOOD, level=logging.WARNING)
        self.feed(FLOOD, level=logging.INFO)
        self.assertEqual(len(self.monitor.outbox), 2)
        self.assertIn("Owner VIP1364 To Partner28", self.monitor.outbox[0])
        self.assertIn("ChannelPrivateError", self.monitor.outbox[0])
        self.assertIn("75s", self.monitor.outbox[1])

    def test_unexpected_traceback_alerts_but_plain_errors_do_not(self):
        self.feed("[REQUESTED ROUTE MISSING] source=-1 dest=-2", level=logging.ERROR)
        self.assertEqual(len(self.monitor.outbox), 0)
        self.feed("[SOMETHING BROKE] KeyError: 'x'", level=logging.ERROR, has_exc=True)
        self.assertEqual(len(self.monitor.outbox), 1)

    def test_first_install_waits_for_tomorrow(self):
        self.assertFalse(self.monitor.digest_due())
        tomorrow = datetime(2026, 11, 5, 8, 1, tzinfo=hm.TZ).timestamp()
        self.assertTrue(self.monitor.digest_due(tomorrow))

    def test_digest_once_per_day_after_eight_and_resets(self):
        self.feed(FORMATTED, FASTVIP_OK, FASTVIP_FAIL)
        self.monitor.state["last_digest_day"] = "2026-11-03"
        self.assertTrue(self.monitor.digest_due())
        text = self.monitor.digest()
        self.assertIn("Delivered 2", text)
        self.assertIn("Failed 1", text)
        self.monitor.reset_after_digest()
        self.assertFalse(self.monitor.digest_due())
        self.assertEqual(self.monitor.state["routes"], {})
        early = datetime(2026, 11, 5, 7, 59, tzinfo=hm.TZ).timestamp()
        self.assertFalse(self.monitor.digest_due(early))

    def test_counters_survive_restart(self):
        self.feed(FORMATTED)
        self.monitor.save()
        again = hm.HealthMonitor("MAIN", self.tmp.name, clock=self.clock)
        self.assertEqual(again.state["routes"]["-1004367822325:8323:-1003726286301:12289"]["ok"], 1)
        self.assertEqual(again.state["restarts"], 2)


class RunLoopTests(unittest.TestCase):
    def test_alerts_go_to_saved_messages_and_retry_after_failure(self):
        sent = []

        class Client:
            fail = True

            async def send_message(self, peer, text, **kwargs):
                if Client.fail:
                    Client.fail = False
                    raise ConnectionError("not connected yet")
                sent.append((peer, text))

        real_sleep = asyncio.sleep

        async def fast_sleep(_):
            await real_sleep(0)

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                monitor = hm.HealthMonitor("ALT", tmp, clock=lambda: datetime(2026, 11, 4, 7, 0, tzinfo=hm.TZ).timestamp())
                monitor.outbox.append("hello")
                hm.asyncio.sleep = fast_sleep
                try:
                    task = asyncio.create_task(monitor.run(Client()))
                    for _ in range(20):
                        await real_sleep(0)
                    task.cancel()
                finally:
                    hm.asyncio.sleep = real_sleep
        asyncio.run(go())
        self.assertEqual(sent, [("me", "hello")])

    def test_handler_feeds_monitor_from_real_loggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = hm.HealthMonitor("MAIN", tmp)
            handler = hm._LogHandler(monitor)
            logger = logging.getLogger("imperium-worker-test")
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            try:
                logger.warning(FASTVIP_OK)
            finally:
                logger.removeHandler(handler)
            self.assertEqual(monitor.state["routes"]["Relay508 To VIP7"]["ok"], 1)


if __name__ == "__main__":
    unittest.main()

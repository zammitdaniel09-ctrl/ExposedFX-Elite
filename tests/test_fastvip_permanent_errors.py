"""Regression tests for FASTVIP permanent destination errors.

Runs the delivery engine (engine/deliver.py, which delivers every FASTVIP2
post) against a stubbed Telegram client, without credentials or network.

    python -m unittest discover -s tests -v
"""

import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import (
    ChannelPrivateError,
    ChatSendPlainForbiddenError,
    ChatWriteForbiddenError,
    FloodWaitError,
)

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "telegram_worker" / "worker_fixed.py"
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.engine.deliver import Deliverer
from telegram_worker.engine.model import Route
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pde = load_module(
    "permanent_delivery_errors",
    ROOT / "telegram_worker" / "permanent_delivery_errors.py",
)

PARTNER_ROUTE = {
    "name": "VIP1364 To Partner4660",
    "source_chat": -1003726286301,
    "source_topic": 1364,
    "dest_chat": -1003885074241,
    "dest_topic": 4660,
}


def rpc_error(cls):
    return cls(request=None)


def message(msg_id, grouped_id=None):
    return SimpleNamespace(id=msg_id, grouped_id=grouped_id, message="TP1 hit", raw_text="TP1 hit", text="TP1 hit",
                           entities=[], media=None, reply_to=None, reply_to_msg_id=None, fwd_from=None,
                           sender_id=None, from_id=None, date=datetime.now(timezone.utc))


# A destination no owner policy stops (PARTNER_ROUTE's 4660 is a stopped topic).
TEST_ROUTE = dict(PARTNER_ROUTE, name="VIP1364 To TestPartner7", dest_chat=-1009999999999, dest_topic=7)


class Client:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def send_message(self, chat, text, **kwargs):
        self.calls.append(text)
        return self.behaviour()


class Harness:
    """The engine deliverer with a stubbed client, a real ledger and a real stop policy."""

    def __init__(self, tmp, behaviour, route=TEST_ROUTE):
        self.alerts = []
        self.client = Client(behaviour)
        self.message_map = {}
        self.ledger_path = Path(tmp) / "fastvip_permanent_failures_v1.json"
        worker = SimpleNamespace(
            client=self.client, message_map=self.message_map, DATA_DIR=tmp, EMOJI_REGISTRY=None,
            FASTVIP_PERMANENT_FAILURES=pde.PermanentFailureLedger(self.ledger_path),
            FASTVIP_IMPAIRED_ALERTS=pde.Throttle(6 * 3600), save_map=lambda: None,
            remember_message=self.remember,
        )
        planner = Planner(rules=Rules(), maps=MessageMap(self.message_map),
                          permanently_failed=lambda r, ids: worker.FASTVIP_PERMANENT_FAILURES.contains_all(r, ids))

        async def no_sleep(seconds):
            return None
        self.deliverer = Deliverer(worker, planner, attempts=3, sleep=no_sleep, alert=self.alerts.append)
        self.route = route

    def remember(self, src, dst, route, save=True):
        self.message_map[f"{route['source_chat']}:{src.id}:{route['dest_chat']}:{route['dest_topic']}"] = dst.id

    def run(self, unit, live=True):
        return asyncio.run(self.deliverer.deliver(self.route, Route.from_legacy(self.route), unit, live=live))


def raises(exc):
    def behaviour():
        raise exc
    return behaviour


class ClassificationTests(unittest.TestCase):

    def test_permission_errors_are_permanent(self):
        for cls in (ChatSendPlainForbiddenError, ChatWriteForbiddenError, ChannelPrivateError):
            self.assertEqual(
                pde.permanent_error_name(rpc_error(cls), -1003885074241), cls.__name__
            )

    def test_flood_wait_and_transient_errors_are_not_permanent(self):
        flood = FloodWaitError(request=None, capture=30)
        for exc in (flood, ConnectionError("reset"), RuntimeError("no destination message")):
            self.assertIsNone(pde.permanent_error_name(exc, -1003885074241))

    def test_missing_entity_only_counts_for_this_destination(self):
        dest_missing = ValueError(
            "Could not find the input entity for PeerChannel(channel_id=3885074241) (PeerChannel)."
        )
        source_missing = ValueError(
            "Could not find the input entity for PeerChannel(channel_id=3726286301) (PeerChannel)."
        )
        self.assertEqual(
            pde.permanent_error_name(dest_missing, -1003885074241),
            pde.DESTINATION_NOT_RESOLVABLE,
        )
        self.assertIsNone(pde.permanent_error_name(source_missing, -1003885074241))


class LedgerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_survives_restart_and_is_per_route(self):
        ledger = pde.PermanentFailureLedger(self.path)
        self.assertTrue(ledger.record(PARTNER_ROUTE, [12053], "ChatSendPlainForbiddenError"))
        reloaded = pde.PermanentFailureLedger(self.path)
        self.assertTrue(reloaded.contains_all(PARTNER_ROUTE, [12053]))
        self.assertFalse(reloaded.contains_all(PARTNER_ROUTE, [12053, 12054]))
        other = dict(PARTNER_ROUTE, dest_topic=4661)
        self.assertFalse(reloaded.contains_all(other, [12053]))
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())

    def test_cap_keeps_newest_entries(self):
        ledger = pde.PermanentFailureLedger(self.path, max_per_route=3)
        for msg_id, stamp in ((1, 10), (2, 20), (3, 30), (4, 40)):
            ledger.record(PARTNER_ROUTE, [msg_id], "X", now=stamp)
        stored = json.loads(self.path.read_text())[pde.route_key(PARTNER_ROUTE)]
        self.assertEqual(sorted(stored), ["2", "3", "4"])

    def test_unreadable_ledger_is_kept_aside_not_overwritten(self):
        self.path.write_text("{not json")
        ledger = pde.PermanentFailureLedger(self.path)
        self.assertFalse(ledger.contains_all(PARTNER_ROUTE, [1]))
        kept = list(Path(self.tmp.name).glob("ledger.json.unreadable-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), "{not json")

    def test_throttle(self):
        throttle = pde.Throttle(60)
        self.assertTrue(throttle.ready("r", now=0))
        self.assertFalse(throttle.ready("r", now=59))
        self.assertTrue(throttle.ready("r", now=60))


class DeliveryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)
        logging.getLogger("imperium-worker").setLevel(logging.CRITICAL)

    def tearDown(self):
        logging.getLogger("imperium-worker").setLevel(logging.NOTSET)
        policy._RUNTIME, policy._PATH = None, None
        self.tmp.cleanup()

    def test_watchdog_loop_hits_telegram_once_not_forever(self):
        """The production bug: message 12053 re-sent every ~5 s forever."""
        h = Harness(self.tmp.name, raises(rpc_error(ChatSendPlainForbiddenError)))
        results = [h.run([message(12053)]) for _ in range(100)]
        self.assertTrue(all(result is True for result in results))
        self.assertEqual(h.client.calls, ["TP1 hit"])
        self.assertEqual(len(h.alerts), 1)
        self.assertTrue(h.deliverer.worker.FASTVIP_PERMANENT_FAILURES.contains_all(TEST_ROUTE, [12053]))

    def test_refused_destination_stops_the_route_until_the_owner_restores_it(self):
        h = Harness(self.tmp.name, raises(rpc_error(ChatWriteForbiddenError)))
        self.assertIs(h.run([message(1)]), True)
        self.assertTrue(policy.is_stopped(TEST_ROUTE), "lost access is a route stop, as in production")
        self.assertIs(h.run([message(2)]), True)
        self.assertEqual(len(h.client.calls), 1, "a stopped route gets no further attempts")
        restored = Harness(self.tmp.name, lambda: SimpleNamespace(id=900, message="TP1 hit", raw_text="TP1 hit",
                                                                 text="TP1 hit", media=None))
        policy._RUNTIME.clear()  # runbook: delete the entry from the stop ledger
        self.assertIs(restored.run([message(3)]), True)
        self.assertEqual(restored.client.calls, ["TP1 hit"])

    def test_skip_survives_worker_restart(self):
        Harness(self.tmp.name, raises(rpc_error(ChatSendPlainForbiddenError))).run([message(12053)])
        policy._RUNTIME.clear()  # isolate the FASTVIP ledger from the route stop
        restarted = Harness(self.tmp.name, raises(AssertionError("must not send")))
        self.assertIs(restarted.run([message(12053)]), True)
        self.assertEqual(restarted.client.calls, [])

    def test_transient_errors_keep_watchdog_recovery(self):
        h = Harness(self.tmp.name, raises(ConnectionError("server disconnected")))
        self.assertIs(h.run([message(10)]), False)
        self.assertIs(h.run([message(10)]), False)
        self.assertEqual(len(h.client.calls), 6, "3 attempts per delivery, then the watchdog retries")
        self.assertFalse(h.ledger_path.exists())

    def test_backfill_callers_keep_old_behaviour(self):
        h = Harness(self.tmp.name, raises(rpc_error(ChatSendPlainForbiddenError)))
        self.assertIs(h.run([message(11)], live=False), False)
        self.assertFalse(h.ledger_path.exists())
        self.assertEqual(h.alerts, [])

    def test_stopped_destination_topic_is_never_written(self):
        h = Harness(self.tmp.name, raises(AssertionError("must not send")), route=PARTNER_ROUTE)
        self.assertIs(h.run([message(13)]), True)
        self.assertEqual(h.client.calls, [])

    def test_successful_copy_unchanged(self):
        h = Harness(self.tmp.name, lambda: SimpleNamespace(id=1, message="TP1 hit", raw_text="TP1 hit",
                                                           text="TP1 hit", media=None))
        self.assertIs(h.run([message(12)]), True)
        self.assertFalse(h.ledger_path.exists())
        self.assertEqual(h.message_map, {"-1003726286301:12:-1009999999999:7": 1})


class CallSiteTests(unittest.TestCase):

    def test_only_live_paths_opt_in(self):
        source = WORKER.read_text(encoding="utf-8")
        calls = source.split("fast_vip_deliver_route(")[2:]  # skip the definition
        opted_in = [call[:200].count("skip_permanent_errors=True") for call in calls]
        # The only caller (one-time backfills retired 4 Nov 2026) is the live
        # FASTVIP2 fan-out, and it opts in.
        self.assertEqual(len(calls), 1)
        self.assertEqual(opted_in, [1], "the live FASTVIP2 fan-out opts in")


if __name__ == "__main__":
    unittest.main()

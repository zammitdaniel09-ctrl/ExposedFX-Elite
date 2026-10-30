"""Regression tests for FASTVIP permanent destination errors.

Runs the REAL fast_vip_deliver_route source from worker_fixed.py against
stubbed Telegram calls, without credentials, network or importing the worker.

    python -m unittest discover -s tests -v
"""

import ast
import asyncio
import importlib.util
import json
import logging
import tempfile
import unittest
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


def message(msg_id):
    return SimpleNamespace(id=msg_id, grouped_id=None)


class Harness:
    """Executes fast_vip_deliver_route from the worker source with stubs."""

    def __init__(self, ledger_path, copy_behaviour):
        self.copy_calls = []
        self.alerts = []
        self.copy_behaviour = copy_behaviour

        tree = ast.parse(WORKER.read_text(encoding="utf-8"))
        target = [
            node for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "fast_vip_deliver_route"
        ]
        assert len(target) == 1, "fast_vip_deliver_route not found exactly once"

        async def copy_one_with_retry(msg, route, edited=False):
            self.copy_calls.append(msg.id)
            return self.copy_behaviour(msg, route)

        async def copy_album_with_retry(messages, route):
            self.copy_calls.append(tuple(m.id for m in messages))
            return self.copy_behaviour(messages[0], route)

        self.namespace = {
            "asyncio": asyncio,
            "log": logging.getLogger("test-fastvip"),
            "existing_destination_ids": lambda item, route: False,
            "should_hard_block_sender": lambda item, ctx, route: False,
            "should_hard_block_2521699926_from_vip": lambda item, route: False,
            "is_fast_vip_pair_route": lambda route: False,
            "should_block_private_peer_loop": lambda item, route: False,
            "fast_vip_effective_route": lambda route: route,
            "text_of": lambda item: "",
            "is_recent_duplicate": lambda route, first, text: False,
            "copy_one_with_retry": copy_one_with_retry,
            "copy_album_with_retry": copy_album_with_retry,
            "FASTVIP_PERMANENT_FAILURES": pde.PermanentFailureLedger(ledger_path),
            "FASTVIP_IMPAIRED_ALERTS": pde.Throttle(6 * 3600),
            "permanent_error_name": pde.permanent_error_name,
            "permanent_route_key": pde.route_key,
            "send_alert": self.alerts.append,
        }
        exec(compile(ast.Module(body=target, type_ignores=[]), str(WORKER), "exec"), self.namespace)
        self.deliver = self.namespace["fast_vip_deliver_route"]

    def run(self, unit, route=PARTNER_ROUTE, live=True):
        return asyncio.run(self.deliver(route, unit, skip_permanent_errors=live))


def raises(exc):
    def behaviour(msg, route):
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
        self.ledger = Path(self.tmp.name) / "fastvip_permanent_failures_v1.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_watchdog_loop_hits_telegram_once_not_forever(self):
        """The production bug: message 12053 re-sent every ~5 s forever."""
        h = Harness(self.ledger, raises(rpc_error(ChatSendPlainForbiddenError)))
        results = [h.run([message(12053)]) for _ in range(100)]
        self.assertTrue(all(result is True for result in results))
        self.assertEqual(h.copy_calls, [12053])
        self.assertEqual(len(h.alerts), 1)

    def test_skip_survives_worker_restart(self):
        Harness(self.ledger, raises(rpc_error(ChatSendPlainForbiddenError))).run([message(12053)])
        restarted = Harness(self.ledger, raises(AssertionError("must not send")))
        self.assertIs(restarted.run([message(12053)]), True)
        self.assertEqual(restarted.copy_calls, [])

    def test_new_messages_still_get_one_attempt_and_route_recovers(self):
        state = {"allowed": False}

        def behaviour(msg, route):
            if not state["allowed"]:
                raise rpc_error(ChatSendPlainForbiddenError)
            return SimpleNamespace(id=900 + msg.id)

        h = Harness(self.ledger, behaviour)
        self.assertIs(h.run([message(1)]), True)
        self.assertIs(h.run([message(2)]), True)
        state["allowed"] = True
        self.assertIs(h.run([message(3)]), True)
        self.assertEqual(h.copy_calls, [1, 2, 3])
        self.assertEqual(len(h.alerts), 1, "alert is throttled per route")

    def test_album_permanent_failure_is_recorded_for_every_item(self):
        h = Harness(self.ledger, raises(rpc_error(ChatWriteForbiddenError)))
        album = [SimpleNamespace(id=i, grouped_id=77) for i in (5, 6, 7)]
        self.assertIs(h.run(album), True)
        self.assertIs(h.run(album), True)
        self.assertEqual(h.copy_calls, [(5, 6, 7)])

    def test_transient_errors_keep_watchdog_recovery(self):
        h = Harness(self.ledger, raises(ConnectionError("server disconnected")))
        self.assertIs(h.run([message(10)]), False)
        self.assertIs(h.run([message(10)]), False)
        self.assertEqual(h.copy_calls, [10, 10])
        self.assertFalse(self.ledger.exists())

    def test_backfill_callers_keep_old_behaviour(self):
        h = Harness(self.ledger, raises(rpc_error(ChatSendPlainForbiddenError)))
        self.assertIs(h.run([message(11)], live=False), False)
        self.assertFalse(self.ledger.exists())
        self.assertEqual(h.alerts, [])

    def test_successful_copy_unchanged(self):
        h = Harness(self.ledger, lambda msg, route: SimpleNamespace(id=1))
        self.assertIs(h.run([message(12)]), True)
        self.assertFalse(self.ledger.exists())


class CallSiteTests(unittest.TestCase):

    def test_only_live_paths_opt_in(self):
        source = WORKER.read_text(encoding="utf-8")
        calls = source.split("fast_vip_deliver_route(")[2:]  # skip the definition
        opted_in = [call[:200].count("skip_permanent_errors=True") for call in calls]
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(opted_in), 2, "only the two live FASTVIP fan-out calls opt in")


if __name__ == "__main__":
    unittest.main()

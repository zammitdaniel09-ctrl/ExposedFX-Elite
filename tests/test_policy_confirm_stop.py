"""Owner stop policy: transient-looking reasons need repeated strikes before a route stops."""

import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "policy_confirm_test", ROOT / "telegram_worker/owner_forwarding_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

ROUTE = {"name": "r", "source_chat": -100111, "source_topic": 5, "dest_chat": -100222, "dest_topic": 9}


class ConfirmStopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy._STRIKES.clear()
        policy.configure(Path(self.tmp.name))
        self.log = logging.getLogger("policy-test")

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_access_loss_stops_at_once(self):
        policy.stop_route(ROUTE, "ChannelPrivateError", self.log)
        self.assertTrue(policy.is_stopped(ROUTE))

    def test_entity_unavailable_needs_three_strikes_over_ten_minutes(self):
        now = [1000.0]
        with patch.object(policy.time, "time", lambda: now[0]):
            for _ in range(5):
                policy.stop_route(ROUTE, "EntityUnavailable", self.log)
                now[0] += 60
            self.assertFalse(policy.is_stopped(ROUTE))
            now[0] += 600
            policy.stop_route(ROUTE, "EntityUnavailable", self.log)
        self.assertTrue(policy.is_stopped(ROUTE))

    def test_topic_unavailable_is_confirmed_too(self):
        policy.stop_route(ROUTE, "TopicUnavailable", self.log)
        self.assertFalse(policy.is_stopped(ROUTE))

    def test_a_success_in_between_clears_the_strikes(self):
        now = [1000.0]
        with patch.object(policy.time, "time", lambda: now[0]):
            for _ in range(3):
                policy.stop_route(ROUTE, "EntityUnavailable", self.log)
                now[0] += 400
                policy.note_success(ROUTE)  # the route worked again: a passing glitch
            policy.stop_route(ROUTE, "EntityUnavailable", self.log)
        self.assertFalse(policy.is_stopped(ROUTE))

    def test_not_found_is_no_strike_while_the_chat_list_is_still_loading(self):
        now = [1000.0]
        policy.ENTITY_CACHE_READY = False
        self.addCleanup(setattr, policy, "ENTITY_CACHE_READY", True)
        with patch.object(policy.time, "time", lambda: now[0]):
            with self.assertLogs("policy-test", "WARNING") as logs:
                for _ in range(4):
                    policy.stop_route(ROUTE, "EntityUnavailable", self.log)
                    now[0] += 600
            self.assertFalse(policy.is_stopped(ROUTE))
            self.assertIn("entity_cache_loading=True", logs.output[-1])
            policy.stop_route(ROUTE, "TopicUnavailable", self.log)
            self.assertEqual(len(policy._STRIKES[policy.identity(ROUTE)]), 1, "other reasons still count")
            policy.stop_route(ROUTE, "ChannelPrivateError", self.log)
        self.assertTrue(policy.is_stopped(ROUTE), "real access loss still stops at once")


if __name__ == "__main__":
    unittest.main()

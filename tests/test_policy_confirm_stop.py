"""Owner stop policy: transient-looking reasons need repeated strikes before a route stops."""
import importlib.util
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("policy_confirm_test", ROOT / "telegram_worker/owner_forwarding_policy.py")
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


if __name__ == "__main__":
    unittest.main()

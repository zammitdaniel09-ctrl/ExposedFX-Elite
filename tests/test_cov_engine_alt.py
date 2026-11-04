"""ALT engine: failure contract, skip bookkeeping, start-up and the read-only replay audit."""

import asyncio
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.errors import ChatWriteForbiddenError
from telethon.tl.types import MessageEntityBold, MessageMediaPhoto, Photo

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import health_monitor
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.engine import alt as alt_engine

RELAY = -1004367822325
SELECT = {
    "name": "NewAccount Select7194 To Relay8323",
    "source_chat": -1004410975295,
    "source_topic": 7194,
    "dest_chat": RELAY,
    "dest_topic": 8323,
    "loop_guard_peers": [RELAY],
}
ARABIC = {
    "name": "ARABIC VIP EN -> RELAY115",
    "source_chat": -1002211486103,
    "source_topic": None,
    "dest_chat": RELAY,
    "dest_topic": 115,
}
BROKEN = {
    "name": "ALT Broken Source",
    "source_chat": -1001111111111,
    "source_topic": None,
    "dest_chat": RELAY,
    "dest_topic": 999,
}
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
ALT_LOG = "imperium-alt-worker"


def post(msg_id, text="", media=False, grouped=None, date=NOW):
    m = SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=[MessageEntityBold(0, 1)] if text else None,
        media=None,
        grouped_id=grouped,
        reply_to=None,
        reply_to_msg_id=None,
        fwd_from=None,
        sender_id=None,
        from_id=None,
        date=date,
    )
    if media:
        m.media = MessageMediaPhoto(
            photo=Photo(id=msg_id, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
        )
    return m


def copy_in_relay(msg_id, text, reply_to, entities):
    """A message as it sits in the relay group (what get_messages returns for the destination)."""
    return SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=entities,
        media=None,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to),
    )


class Client:
    def __init__(self):
        self.calls, self.fail, self.next_id = [], [], 9000
        self.stored = {}  # (chat, id) -> message, for get_messages
        self.broken = set()  # chats whose reads fail
        self.reads = []

    def _result(self, text, media):
        self.next_id += 1
        return SimpleNamespace(
            id=self.next_id,
            message=text,
            raw_text=text,
            text=text,
            media=MessageMediaPhoto() if media else None,
        )

    async def send_message(self, chat, text, **kwargs):
        self.calls.append(("text", chat, text, kwargs))
        if self.fail:
            raise self.fail.pop(0)
        return self._result(text, False)

    async def send_file(self, chat, file, **kwargs):
        self.calls.append(("file", chat, file, kwargs))
        if self.fail:
            raise self.fail.pop(0)
        if isinstance(file, list):
            return [self._result(kwargs.get("caption") if i == 0 else "", True) for i in range(len(file))]
        return self._result(kwargs.get("caption") or "", True)

    async def get_messages(self, chat, ids):
        self.reads.append((chat, list(ids)))
        if chat in self.broken:
            raise ConnectionError("chat unavailable")
        return [self.stored.get((chat, i)) for i in ids]


class AltHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)
        self.checkpoints, self.filtered, self.translated = {}, [], []
        self.client = Client()
        legacy = SimpleNamespace(
            client=self.client,
            DATA_DIR=self.tmp.name,
            message_map={},
            ROUTES=[SELECT, ARABIC],
            FILTER_USERNAME_MENTIONS=True,
            FILTER_LINKS=True,
        )
        legacy.alt_arabic115_route = lambda route: route["name"] == ARABIC["name"]

        async def translated_payload(route, message):
            self.translated.append(message.id)
            return "EN: " + message.message, [MessageEntityBold(0, 3)]

        def remember(route, src, dst):
            items = dst if isinstance(dst, list) else [dst]
            key = f"{route['source_chat']}:{src.id}:{route['dest_chat']}:{route['dest_topic']}"
            legacy.message_map[key] = items[0].id
            self.checkpoints[route["name"]] = max(self.checkpoints.get(route["name"], 0), src.id)

        legacy.translated_payload = translated_payload
        legacy.remember = remember
        legacy.update_checkpoint = lambda route, mid: self.checkpoints.__setitem__(
            route["name"], max(self.checkpoints.get(route["name"], 0), mid)
        )
        legacy.log_username_filter_alt = lambda messages, ctx, route=None: self.filtered.extend(
            m.id for m in messages
        )
        self.legacy = legacy
        self.reply_target = None

        async def resolve(worker, route, message, logger=None):
            return self.reply_target or route["dest_topic"]

        patcher = patch.object(alt_engine, "resolve_reply_target", resolve)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = alt_engine.AltEngine(legacy, clock=lambda: NOW.timestamp() + 12.5)

    def tearDown(self):
        policy._RUNTIME, policy._PATH = None, None
        self.tmp.cleanup()

    def deliver(self, route, unit, reason="telegram_push"):
        return asyncio.run(self.engine.deliver(route, unit, reason))


class AltWorkerAdapterTests(AltHarness):
    def test_worker_view_reads_through_to_the_legacy_module(self):
        worker = alt_engine.AltWorker(self.legacy)
        self.assertIs(worker.client, self.client)
        self.assertEqual(worker.DATA_DIR, self.tmp.name)
        self.assertIs(worker.message_map, self.legacy.message_map)
        self.assertIsNone(worker.EMOJI_REGISTRY)
        worker.remember_message(post(5, "TP1"), SimpleNamespace(id=777), SELECT, save=False)
        self.assertEqual(self.legacy.message_map, {f"{SELECT['source_chat']}:5:{RELAY}:8323": 777})
        self.assertEqual(self.checkpoints[SELECT["name"]], 5, "remember() also advances the checkpoint")
        worker.save_map()
        self.assertEqual(len(self.legacy.message_map), 1, "save_map is a no-op: remember() saved")

    def test_install_registers_the_engine_on_the_legacy_worker(self):
        with self.assertLogs(ALT_LOG, "WARNING") as logs:
            engine = alt_engine.install_alt_engine(self.legacy)
        self.assertIs(self.legacy.DELIVERY_ENGINE, engine)
        self.assertEqual(engine.deliverer.attempts, 1, "ALT's watchdog retries, not the engine")
        self.assertIn("[ALT DELIVERY ENGINE READY] routes=2 formatters=['copy', 'translate']", logs.output[0])

    def test_link_filter_follows_the_legacy_flag(self):
        self.legacy.FILTER_LINKS = False
        engine = alt_engine.AltEngine(self.legacy)
        with self.assertLogs(ALT_LOG, "WARNING") as logs:
            self.assertTrue(asyncio.run(engine.deliver(SELECT, [post(7, "chart at example.com")], "push")))
        self.assertIn("[ALT COPIED]", logs.output[0])
        self.assertEqual([c[2] for c in self.client.calls], ["chart at example.com"])
        self.assertEqual(self.filtered, [])


class AltFailureContractTests(AltHarness):
    def test_unexpected_send_error_is_logged_once_and_left_for_the_watchdog(self):
        self.client.fail = [RuntimeError("unexpected payload")]
        with self.assertLogs(ALT_LOG, "ERROR") as logs:
            self.assertFalse(self.deliver(SELECT, [post(40, "TP3 hit")]))
        self.assertEqual(len(self.client.calls), 1, "one attempt per call")
        self.assertEqual((self.legacy.message_map, self.checkpoints), ({}, {}))
        line = logs.records[0].getMessage()
        self.assertEqual(
            line,
            f"[ALT COPY FAILED] route={SELECT['name']} ids=[40] RuntimeError: unexpected payload engine=True",
        )
        with tempfile.TemporaryDirectory() as tmp:
            monitor = health_monitor.HealthMonitor("ExposedFX ALT", tmp)
            monitor.observe(logging.ERROR, line)
            self.assertEqual(monitor.state["routes"][SELECT["name"]]["fail"], 1, "health monitor sees it")

    def test_lost_write_access_stops_the_route_and_later_posts_are_only_checkpointed(self):
        self.client.fail = [ChatWriteForbiddenError(request=None)]
        with self.assertLogs(level="WARNING") as logs:  # the stop is logged by the shared Deliverer
            self.assertFalse(self.deliver(SELECT, [post(41, "TP1 hit")]))
        self.assertTrue(any("[OWNER ROUTE STOPPED]" in line for line in logs.output))
        self.assertTrue(any("[ALT COPY FAILED]" in line for line in logs.output))
        self.assertTrue(policy.is_stopped(SELECT))
        with self.assertLogs(ALT_LOG, "INFO") as logs:
            self.assertTrue(self.deliver(SELECT, [post(42, "TP2 hit")]))
        self.assertEqual(len(self.client.calls), 1, "nothing more is sent to a stopped route")
        self.assertIn(
            f"[ALT ENGINE SKIP] route={SELECT['name']} ids=[42] reason=owner_stopped", logs.output[0]
        )
        self.assertEqual(self.checkpoints[SELECT["name"]], 42)

    def test_partially_mapped_album_fails_closed(self):
        self.legacy.message_map[f"{SELECT['source_chat']}:61:{RELAY}:8323"] = 9500
        album = [post(61, "", media=True, grouped=3), post(60, "chart", media=True, grouped=3)]
        with self.assertLogs(ALT_LOG, "ERROR") as logs:
            self.assertFalse(self.deliver(SELECT, album))
        self.assertIn("[ALT PARTIAL ALBUM MAP - FAIL CLOSED] ids=[60, 61]", logs.output[0])
        self.assertEqual((self.client.calls, self.checkpoints), ([], {}), "not sent, not checkpointed")

    def test_empty_post_is_skipped_and_checkpointed(self):
        with self.assertLogs(ALT_LOG, "INFO") as logs:
            self.assertTrue(self.deliver(SELECT, [post(70)]))
        self.assertIn("[ALT EMPTY MESSAGE SKIPPED] source_msg=70 engine=True", logs.output[0])
        self.assertEqual((self.client.calls, self.checkpoints[SELECT["name"]]), ([], 70))

    def test_already_copied_post_is_checkpointed_silently(self):
        self.legacy.message_map[f"{SELECT['source_chat']}:80:{RELAY}:8323"] = 9800
        with self.assertNoLogs(ALT_LOG, "INFO"):
            self.assertTrue(self.deliver(SELECT, [post(80, "TP1 hit")]))
        self.assertEqual((self.client.calls, self.checkpoints[SELECT["name"]]), ([], 80))

    def test_copied_line_carries_lag_and_preserved_reply(self):
        self.reply_target = 8400
        with self.assertLogs(ALT_LOG, "WARNING") as logs:
            self.assertTrue(self.deliver(SELECT, [post(90, "TP1 hit")], reason="watchdog"))
        self.assertIn(
            f"[ALT COPIED] route={SELECT['name']} ids=[90] kind=SINGLE reason=watchdog engine=True lag=12.5s",
            logs.output[0],
        )
        self.assertIn(
            f"[ALT REPLY PRESERVED] route={SELECT['name']} source_child=90 dest_parent=8400", logs.output[1]
        )
        self.assertEqual(self.client.calls[0][3]["reply_to"], 8400)

    def test_post_without_a_date_reports_zero_lag(self):
        with self.assertLogs(ALT_LOG, "WARNING") as logs:
            self.assertTrue(self.deliver(SELECT, [post(91, "TP2 hit", date=None)]))
        self.assertIn("lag=0.0s", logs.output[0])
        self.assertEqual(len(logs.output), 1, "topic-root reply is not reported as preserved")


class AltReplayAuditTests(AltHarness):
    def setUp(self):
        super().setUp()
        self.legacy.ROUTES = [SELECT, ARABIC, BROKEN]
        src, dst = SELECT["source_chat"], RELAY
        for source_id, dest_id in ((10, 9010), (11, 9011), (12, 9012), (13, 9013)):
            self.legacy.message_map[f"{src}:{source_id}:{RELAY}:8323"] = dest_id
        self.legacy.message_map[f"{ARABIC['source_chat']}:20:{RELAY}:115"] = [9020, 9021]
        self.legacy.message_map[f"{BROKEN['source_chat']}:5:{RELAY}:999"] = 9005
        bold = [MessageEntityBold(0, 1)]
        self.client.stored.update(
            {
                (src, 10): post(10, "Buy XAUUSD 4180"),
                (dst, 9010): copy_in_relay(9010, "Buy XAUUSD 4180", 8323, bold),
                (src, 11): post(11, "TP1 hit"),
                (dst, 9011): copy_in_relay(9011, "TP1", 8323, bold),
                (src, 12): post(12, "", media=True, grouped=4),
                (dst, 9012): copy_in_relay(9012, "", 8323, None),
                (src, 13): post(13, "TP2 hit"),
                (ARABIC["source_chat"], 20): post(20, "شراء الذهب"),
                (dst, 9020): copy_in_relay(9020, "EN: شراء الذهب", 115, [MessageEntityBold(0, 3)]),
            }
        )
        self.client.broken.add(BROKEN["source_chat"])
        self.sleeps = []

    def audit(self, **kwargs):
        async def fake_sleep(seconds):
            self.sleeps.append(seconds)

        with patch("asyncio.sleep", fake_sleep):
            return asyncio.run(alt_engine.run_alt_replay_audit(self.legacy, self.engine, **kwargs))

    def test_audit_compares_engine_plans_with_relay_copies_once(self):
        with self.assertLogs(ALT_LOG, "INFO") as logs:
            report = self.audit(delay_seconds=60.0)
        self.assertEqual(
            report[SELECT["name"]],
            {
                "checked": 2,
                "match": 1,
                "mismatch": 1,
                "unavailable_or_album": 2,
                "examples": [{"source": 11, "dest": 9011, "fields": ["text"]}],
            },
        )
        self.assertEqual(
            report[ARABIC["name"]],
            {"checked": 1, "match": 1, "mismatch": 0, "unavailable_or_album": 0, "examples": []},
        )
        self.assertEqual(self.translated, [20], "the T4T copy is compared with the translation")
        self.assertEqual(report[BROKEN["name"]]["error"], "ConnectionError: chat unavailable")
        self.assertEqual(report[BROKEN["name"]]["checked"], 0)
        self.assertEqual(self.sleeps, [60.0, 1.0, 1.0, 1.0], "settle first, then pace the routes")
        self.assertEqual(self.client.calls, [], "read-only: nothing is sent")
        levels = {r.getMessage().split(" checked=")[0]: r.levelno for r in logs.records}
        self.assertEqual(levels[f"[ALT ENGINE REPLAY AUDIT] route={SELECT['name']}"], logging.WARNING)
        self.assertEqual(levels[f"[ALT ENGINE REPLAY AUDIT] route={ARABIC['name']}"], logging.INFO)
        self.assertEqual(levels[f"[ALT ENGINE REPLAY AUDIT] route={BROKEN['name']}"], logging.WARNING)
        self.assertIn("[ALT ENGINE REPLAY AUDIT DONE] routes=3 checked=3 match=2", logs.output[-1])

        reads = len(self.client.reads)
        self.assertIsNone(self.audit(), "already audited for this version and deployment")
        self.assertEqual((len(self.client.reads), self.sleeps[4:]), (reads, []), "no reads, no wait")

    def test_audit_reads_only_the_newest_posts_and_ten_on_the_translated_route(self):
        for source_id in range(30, 42):
            self.legacy.message_map[f"{ARABIC['source_chat']}:{source_id}:{RELAY}:115"] = 9000 + source_id
        with self.assertLogs(ALT_LOG, "INFO"):
            report = self.audit(per_route=2)
        reads = dict(self.client.reads[::2])
        self.assertEqual(reads[SELECT["source_chat"]], [12, 13])
        self.assertEqual(
            reads[ARABIC["source_chat"]], list(range(32, 42)), "translation calls are capped at 10"
        )
        self.assertEqual(report[ARABIC["name"]]["unavailable_or_album"], 10)

    def test_audit_runs_again_when_its_state_cannot_be_saved(self):
        self.legacy.DATA_DIR = str(Path(self.tmp.name) / "missing" / "dir")
        with self.assertLogs(ALT_LOG, "WARNING") as logs:
            self.assertIsNotNone(self.audit(delay_seconds=5.0))
            self.assertIsNotNone(self.audit(delay_seconds=5.0), "no saved state: audits again next start")
        self.assertEqual(sum("[ALT ENGINE REPLAY AUDIT DONE]" in line for line in logs.output), 2)
        self.assertEqual(self.sleeps.count(5.0), 2)


if __name__ == "__main__":
    unittest.main()

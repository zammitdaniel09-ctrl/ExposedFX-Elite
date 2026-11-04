"""Engine executor edge cases: repairs, media fallback, VIP7 parent recovery and impaired-route alerts."""

import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import MessageMediaPhoto, Photo

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
if importlib.util.find_spec("requests") is None:  # CI installs only telethon; runtime_guard imports it
    sys.modules["requests"] = types.ModuleType("requests")
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import reply_hardening, runtime_guard
from telegram_worker.community_emoji_registry import CommunityEmojiRegistry
from telegram_worker.engine.deliver import Deliverer, structure_problem
from telegram_worker.engine.model import Route
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules
from telegram_worker.permanent_delivery_errors import PermanentFailureLedger, Throttle

VIP, RELAY = -1003726286301, -1004367822325
BLOCKED = 7556281143
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
VIP7 = {
    "name": "Relay508 To VIP7",
    "source_chat": RELAY,
    "source_topic": 508,
    "dest_chat": VIP,
    "dest_topic": 7,
}
SIGNAL = "Buy xauusd now\n\nTp 4324\nTp 4337\n\nSl 4308"
NOW = datetime(2026, 11, 4, 14, 0, tzinfo=timezone.utc)
LOG = "imperium-worker"
UNRESOLVABLE = "Could not find the input entity for PeerChannel(channel_id=3726286301)"


def message(msg_id, text="", media=False, grouped=None, date=NOW, reply_to=None, topic=2, download="file"):
    m = SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=[],
        media=None,
        grouped_id=grouped,
        reply_to=None,
        reply_to_msg_id=None,
        fwd_from=None,
        sender_id=None,
        from_id=None,
        date=date,
    )
    if reply_to:
        m.reply_to = SimpleNamespace(reply_to_msg_id=reply_to, reply_to_top_id=topic, top_msg_id=None)
        m.reply_to_msg_id = reply_to
    if media:
        m.media = MessageMediaPhoto(
            photo=Photo(id=msg_id, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
        )

        async def download_media(file):
            if download == "file":
                Path(file).write_bytes(b"jpeg")
                return file
            if download == "dir":
                Path(file).mkdir()
                return file
            return None

        m.download_media = download_media
    return m


# Scripted send outcomes (anything else in Client.script is raised).
def short_album(client, kind, payload, kwargs):
    return [client.make(kwargs.get("caption"), True)]


def without_media(client, kind, payload, kwargs):
    return client.make(kwargs.get("caption") or "", False)


def wrong_text(client, kind, payload, kwargs):
    return client.make("TP1", False)


def nothing(client, kind, payload, kwargs):
    return None


class Client:
    def __init__(self):
        self.calls, self.deleted, self.script, self.reads = [], [], [], []
        self.next_id = 9000
        self.delete_error = None
        self.parents = {}

    def make(self, text, media):
        self.next_id += 1
        return SimpleNamespace(
            id=self.next_id,
            message=text,
            raw_text=text,
            text=text,
            media=MessageMediaPhoto() if media else None,
        )

    async def _send(self, kind, chat, payload, kwargs):
        self.calls.append((kind, chat, payload, kwargs))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, BaseException):
            raise step
        if step is not None:
            return step(self, kind, payload, kwargs)
        if kind == "text":
            return self.make(payload, False)
        if isinstance(payload, list):
            return [self.make(kwargs.get("caption") if i == 0 else "", True) for i in range(len(payload))]
        return self.make(kwargs.get("caption") or "", True)

    async def send_message(self, chat, text, **kwargs):
        return await self._send("text", chat, text, kwargs)

    async def send_file(self, chat, file, **kwargs):
        return await self._send("file", chat, file, kwargs)

    async def delete_messages(self, chat, ids):
        if self.delete_error:
            raise self.delete_error
        self.deleted.extend(ids)

    async def get_messages(self, chat, ids):
        self.reads.append((chat, ids))
        value = self.parents.get(ids)
        if isinstance(value, BaseException):
            raise value
        return value


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)
        self.client = Client()
        self.message_map = {}
        self.saves, self.sleeps, self.alerts = [], [], []
        self.worker = SimpleNamespace(
            client=self.client,
            message_map=self.message_map,
            DATA_DIR=self.tmp.name,
            EMOJI_REGISTRY=None,
            FASTVIP_PERMANENT_FAILURES=PermanentFailureLedger(Path(self.tmp.name) / "ledger.json"),
            FASTVIP_IMPAIRED_ALERTS=Throttle(3600),
            remember_message=self.remember,
            save_map=lambda: self.saves.append("save"),
        )
        self.owner_routes = SimpleNamespace(mapped_ids=self.mapped_ids)
        self.deliverer = self.make_deliverer()
        self.route = Route.from_legacy(ROUTE)

    def tearDown(self):
        policy._RUNTIME, policy._PATH = None, None
        policy._STRIKES.pop(policy.identity(ROUTE), None)
        self.tmp.cleanup()

    def make_deliverer(self, **options):
        planner = Planner(
            rules=Rules(blocked_senders=frozenset({BLOCKED})), maps=MessageMap(self.message_map)
        )

        async def sleep(seconds):
            self.sleeps.append(seconds)

        settings = dict(
            attempts=3, sleep=sleep, clock=lambda: NOW.timestamp() + 3.5, alert=self.alerts.append
        )
        settings.update(options)
        return Deliverer(self.worker, planner, **settings)

    def remember(self, src, dst, route, save=True):
        items = dst if isinstance(dst, list) else [dst]
        ids = [int(i.id) for i in items if getattr(i, "id", None)]
        if ids:
            key = f"{route['source_chat']}:{src.id}:{route['dest_chat']}:{route['dest_topic']}"
            self.message_map[key] = ids[0] if len(ids) == 1 else ids
            if save:
                self.saves.append("save")

    def mapped_ids(self, worker, legacy_route, unit):
        out = []
        for m in unit:
            key = f"{legacy_route['source_chat']}:{m.id}:{legacy_route['dest_chat']}:{legacy_route['dest_topic']}"
            if key in self.message_map:
                out.append(self.message_map[key])
        return out

    def deliver(self, unit, live=True, deliverer=None):
        return asyncio.run((deliverer or self.deliverer).deliver(ROUTE, self.route, unit, live=live))

    def key(self, source_id, route=ROUTE):
        return f"{route['source_chat']}:{source_id}:{route['dest_chat']}:{route['dest_topic']}"


class DeliverContractTests(Harness):
    def test_partially_mapped_album_is_refused_for_a_later_retry(self):
        self.message_map[self.key(11)] = 9500
        unit = [message(11, "", media=True, grouped=5), message(10, "chart", media=True, grouped=5)]
        with self.assertLogs(LOG, "ERROR") as logs:
            self.assertFalse(self.deliver(unit))
        self.assertIn(
            f"[FASTVIP DEST FAILED] route={ROUTE['name']} ids=[10, 11] dest={VIP}_1364 RuntimeError: "
            "partial album mapping engine=True",
            logs.output[0],
        )
        self.assertEqual(self.client.calls, [])

    def test_copied_line_reports_lag_from_the_clock_or_zero_without_a_date(self):
        with self.assertLogs(LOG, "WARNING") as logs:
            self.assertTrue(self.deliver([message(1, "TP1 hit")]))
            self.assertTrue(self.deliver([message(2, "TP2 hit", date=None)]))
        self.assertIn(f"ids=[1] kind=SINGLE dest={VIP}_1364 engine=True lag=3.5s", logs.output[0])
        self.assertIn(f"ids=[2] kind=SINGLE dest={VIP}_1364 engine=True lag=0.0s", logs.output[1])

    def test_owner_copy_on_a_stopped_route_sends_nothing(self):
        policy.stop_route(ROUTE, "ChatWriteForbiddenError")
        result = asyncio.run(
            self.deliverer.copy_unit(self.owner_routes, ROUTE, self.route, [message(8, "TP5")])
        )
        self.assertEqual(result, ("filtered", []))
        self.assertEqual(self.client.calls, [])

    def test_parent_recovered_by_reply_hardening_becomes_the_reply_target(self):
        seen = []

        async def recovered(worker, msg, route, registry=None, logger=None):
            seen.append((worker, msg.id, route["name"]))
            self.message_map[self.key(90)] = 7777
            return 7777

        with patch.object(reply_hardening, "ensure_reply_mapping", recovered), self.assertLogs(
            LOG, "WARNING"
        ):
            self.assertTrue(self.deliver([message(91, "TP1 hit", reply_to=90)]))
        self.assertEqual(seen, [(self.worker, 91, ROUTE["name"])])
        self.assertEqual(self.client.calls[0][3]["reply_to"], 7777)
        self.assertEqual(self.client.reads, [], "copy routes never import a missed parent")


class StructureRepairTests(Harness):
    def test_album_missing_items_is_deleted_and_resent_whole(self):
        self.client.script = [short_album]
        unit = [message(11, "", media=True, grouped=5), message(10, "chart", media=True, grouped=5)]
        with self.assertLogs(LOG, "WARNING") as logs:
            self.assertTrue(self.deliver(unit))
        self.assertEqual(self.client.deleted, [9001])
        self.assertEqual(self.sleeps, [2], "retried as a transient structure error")
        self.assertEqual(self.message_map, {self.key(10): 9002, self.key(11): 9003})
        self.assertEqual(self.saves, ["save"], "album map saved once")
        self.assertTrue(any("reason=album_item_count_mismatch" in line for line in logs.output))
        self.assertTrue(
            any("mirror album structure mismatch: expected=2 sent=1" in line for line in logs.output)
        )

    def test_media_copy_that_arrives_without_media_is_repaired_once(self):
        self.client.script = [without_media]
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(self.deliver([message(6, "chart", media=True)]))
        self.assertEqual((self.client.deleted, self.message_map), ([9001], {self.key(6): 9002}))
        self.assertTrue(any("reason=missing_media" in line for line in logs.output))
        self.assertTrue(
            any(
                f"[mirror structure repair ok] route={ROUTE['name']} source_msg=6" in line
                for line in logs.output
            )
        )

    def test_text_copy_with_the_wrong_text_is_replaced(self):
        self.client.script = [wrong_text]
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(self.deliver([message(7, "TP1 hit")]))
        self.assertEqual((self.client.deleted, self.message_map), ([9001], {self.key(7): 9002}))
        self.assertTrue(any("reason=text_or_caption_mismatch" in line for line in logs.output))

    def test_send_without_a_result_is_resent_without_deleting_anything(self):
        self.client.script = [nothing]
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(self.deliver([message(8, "TP2 hit")]))
        self.assertEqual((len(self.client.calls), self.client.deleted), (2, []))
        self.assertFalse(any("deleted bad copy" in line for line in logs.output))
        self.assertEqual(self.message_map, {self.key(8): 9001})

    def test_failed_cleanup_of_a_bad_copy_is_logged_and_repair_continues(self):
        self.client.script = [without_media]
        self.client.delete_error = RuntimeError("MESSAGE_DELETE_FORBIDDEN")
        with self.assertLogs(LOG, "WARNING") as logs:
            self.assertTrue(self.deliver([message(9, "chart", media=True)]))
        self.assertIn(
            f"[mirror structure repair delete failed] ids=[9001] route={ROUTE['name']} reason=missing_media: "
            "MESSAGE_DELETE_FORBIDDEN engine=True",
            "\n".join(logs.output),
        )
        self.assertEqual(self.message_map, {self.key(9): 9002})

    def test_structure_check_names_each_problem(self):
        text_post, media_post = message(1, "TP1  hit"), message(2, "chart", media=True)
        arrived = SimpleNamespace(
            message="TP1 hit", raw_text="TP1 hit", text="TP1 hit", media=MessageMediaPhoto()
        )
        self.assertEqual(structure_problem(text_post, arrived, "TP1  hit"), "unexpected_media")
        self.assertEqual(structure_problem(media_post, [None], "chart"), "no_sent_message")
        arrived = SimpleNamespace(message="chart\n", raw_text="", text="", media=MessageMediaPhoto())
        self.assertIsNone(structure_problem(media_post, [arrived, None], "  chart "), "whitespace is ignored")
        self.assertEqual(structure_problem(media_post, [arrived], "chart 2"), "text_or_caption_mismatch")


class MediaFallbackTests(Harness):
    def cache(self):
        return Path(self.tmp.name) / "media_cache"

    def test_fallback_that_downloads_nothing_fails_the_post(self):
        self.client.script = [RuntimeError("file reference expired")]
        with self.assertLogs(LOG, "ERROR") as logs:
            self.assertFalse(self.deliver([message(20, "chart", media=True, download=None)]))
        self.assertIn("RuntimeError: media fallback downloaded no files", logs.output[-1])
        self.assertEqual((len(self.client.calls), self.message_map), (1, {}), "not retried, not mapped")

    def test_album_fallback_reuploads_every_item_and_cleans_up(self):
        self.client.script = [RuntimeError("file reference expired")]
        unit = [message(11, "", media=True, grouped=5), message(10, "chart", media=True, grouped=5)]
        with self.assertLogs(LOG, "INFO") as logs:
            self.assertTrue(self.deliver(unit))
        kind, chat, files, kwargs = self.client.calls[1]
        self.assertEqual([Path(f).name for f in files], ["engine_1364_10", "engine_1364_11"])
        self.assertEqual((chat, kwargs["caption"], kwargs["reply_to"]), (VIP, "chart", 1364))
        self.assertEqual(self.message_map, {self.key(10): 9001, self.key(11): 9002})
        self.assertEqual(list(self.cache().iterdir()), [], "downloads are removed")
        self.assertTrue(
            any(f"[media fallback reupload ok] route={ROUTE['name']} items=2" in x for x in logs.output)
        )

    def test_download_that_cannot_be_removed_does_not_fail_a_delivered_post(self):
        self.client.script = [RuntimeError("file reference expired")]
        with self.assertLogs(LOG, "WARNING"):
            self.assertTrue(self.deliver([message(21, "chart", media=True, download="dir")]))
        self.assertEqual(self.message_map, {self.key(21): 9001})
        self.assertTrue((self.cache() / "engine_1364_21").is_dir())


class VipParentRecoveryTests(Harness):
    def registry(self):
        registry = CommunityEmojiRegistry(Path(self.tmp.name))
        registry.entries = {
            alias: {"type": "custom", "document_id": 5000 + i, "fallback": "✅"}
            for i, alias in enumerate(("Boom", "GreenTick", "RedCross", "Warning"))
        }
        return registry

    def reply_with_parent(self, parent):
        self.client.parents[40] = parent
        reply = message(41, "Tp1 hit", reply_to=40, topic=508)

        async def no_mapping(*args, **kwargs):
            return None

        route = Route.from_legacy(VIP7, formatter="imperium_vip")
        with patch.object(reply_hardening, "ensure_reply_mapping", no_mapping), self.assertLogs(LOG) as logs:
            self.assertTrue(asyncio.run(self.deliverer.deliver(VIP7, route, [reply])))
        self.assertEqual(self.client.reads, [(RELAY, 40)], "only the replied-to parent is read")
        self.assertEqual(len(self.client.calls), 1, "the parent is not imported")
        self.assertEqual(self.client.calls[0][3]["reply_to"], 7, "the reply goes to the topic")
        self.assertNotIn(self.key(40, VIP7), self.message_map)
        self.assertFalse(any("RECOVERED BY COPY" in line for line in logs.output))
        return logs.output

    def test_parent_that_cannot_be_read_is_skipped(self):
        output = self.reply_with_parent(ConnectionError("timed out"))
        self.assertIn(
            "[IMPERIUM VIP REPLY PARENT FETCH FAILED] parent=40 ConnectionError: timed out engine=True",
            output[0],
        )

    def test_missing_or_non_signal_parent_is_skipped(self):
        self.worker.EMOJI_REGISTRY = self.registry()
        self.reply_with_parent(None)
        self.client.reads.clear()
        self.client.calls.clear()
        self.message_map.clear()
        self.reply_with_parent(message(40, "Good morning traders", date=NOW - timedelta(hours=1), topic=508))

    def test_parent_without_a_date_counts_as_too_old(self):
        self.worker.EMOJI_REGISTRY = self.registry()
        output = self.reply_with_parent(message(40, SIGNAL, date=None))
        self.assertIn("[IMPERIUM VIP REPLY PARENT TOO OLD] parent=40 age=None max=86400", output[0])

    def test_signal_parent_is_not_imported_without_the_house_emoji(self):
        self.reply_with_parent(message(40, SIGNAL, date=NOW - timedelta(hours=2)))

    def test_signal_parent_the_rules_would_skip_is_not_imported(self):
        self.worker.EMOJI_REGISTRY = self.registry()
        parent = message(40, SIGNAL, date=NOW - timedelta(hours=2))
        parent.sender_id = BLOCKED
        self.reply_with_parent(parent)


class ImpairedRouteTests(Harness):
    def test_unresolvable_destination_is_recorded_and_alerted_once(self):
        with self.assertLogs(level="WARNING") as logs:
            for msg_id in (30, 31):
                self.client.script = [ValueError(UNRESOLVABLE)]
                self.assertTrue(self.deliver([message(msg_id, f"TP{msg_id}")]))
        ledger = self.worker.FASTVIP_PERMANENT_FAILURES
        self.assertTrue(ledger.contains_all(ROUTE, [30, 31]))
        self.assertEqual(len(self.client.calls), 2, "one attempt per post, nothing retried")
        self.assertEqual(len(self.alerts), 1, "the owner is alerted once per interval")
        self.assertIn(ROUTE["name"], self.alerts[0])
        self.assertIn(f"DestinationNotResolvable</code> at {VIP}_1364", self.alerts[0])
        impaired = [line for line in logs.output if "[FASTVIP ROUTE IMPAIRED - NOT DELIVERED]" in line]
        self.assertEqual(len(impaired), 2)
        self.assertIn("LEDGER_SAVED=True", impaired[0])
        self.assertFalse(policy.is_stopped(ROUTE), "an unresolved entity needs confirmation before a stop")

    def test_failing_alert_never_changes_the_outcome(self):
        def broken_alert(text):
            raise RuntimeError("bot api down")

        deliverer = self.make_deliverer(alert=broken_alert)
        self.client.script = [ValueError(UNRESOLVABLE)]
        with self.assertLogs(LOG, "ERROR"):
            self.assertTrue(self.deliver([message(32, "TP1")], deliverer=deliverer))
        self.assertTrue(self.worker.FASTVIP_PERMANENT_FAILURES.contains_all(ROUTE, [32]))

    def test_default_alert_goes_through_the_runtime_guard(self):
        sent = []
        deliverer = Deliverer(self.worker, Planner(rules=Rules(), maps=MessageMap(self.message_map)))
        self.client.script = [ValueError(UNRESOLVABLE)]
        with patch.object(runtime_guard, "send_alert", sent.append), self.assertLogs(LOG, "ERROR"):
            self.assertTrue(self.deliver([message(33, "TP1")], deliverer=deliverer))
        self.assertEqual(len(sent), 1)
        self.assertIn("Route impaired", sent[0])

    def test_offline_replay_does_not_record_permanent_failures(self):
        self.client.script = [ValueError(UNRESOLVABLE)]
        with self.assertLogs(LOG, "ERROR") as logs:
            self.assertFalse(self.deliver([message(34, "TP1")], live=False))
        self.assertIn("[FASTVIP DEST FAILED]", logs.output[-1])
        self.assertFalse(self.worker.FASTVIP_PERMANENT_FAILURES.contains_all(ROUTE, [34]))
        self.assertEqual(self.alerts, [])


if __name__ == "__main__":
    unittest.main()

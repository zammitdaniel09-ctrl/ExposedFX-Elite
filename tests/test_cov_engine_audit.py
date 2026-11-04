"""MAIN replay audit: field comparison, mapped-pair selection and the once-per-deploy run."""

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from telethon.tl.types import (
    Document,
    MessageEntityBold,
    MessageEntityItalic,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
)

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker.engine import audit
from telegram_worker.engine.model import Route, Send, Skip
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules

RELAY, VIP = -1004367822325, -1003726286301
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
BROKEN = {"name": "Broken Source To VIP99", "source_chat": -1001234567890, "dest_chat": VIP, "dest_topic": 99}
PHOTO = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
DOCUMENT = MessageMediaDocument(
    document=Document(
        id=2,
        access_hash=1,
        file_reference=b"",
        date=None,
        mime_type="application/pdf",
        size=10,
        dc_id=1,
        attributes=[],
    )
)


def msg(msg_id, text="", media=None, entities=None, reply_to=None, grouped=None):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=entities,
        media=media,
        grouped_id=grouped,
        reply_to=SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None,
        reply_to_msg_id=None,
        fwd_from=None,
        sender_id=None,
        from_id=None,
    )


def send(kind="text", text="TP1", entities=(), reply_to=1364):
    return Send(kind=kind, dest_chat=VIP, reply_to=reply_to, text=text, entities=entities)


class Reader:
    """get_messages over a dict of stored messages; some chats can be made unreadable."""

    def __init__(self, stored, broken=()):
        self.stored, self.broken, self.reads = stored, set(broken), []

    async def get_messages(self, chat, ids):
        self.reads.append((chat, list(ids)))
        if chat in self.broken:
            raise ConnectionError("source chat unavailable")
        return [self.stored.get((chat, i)) for i in ids]


class CompareTests(unittest.TestCase):
    def test_engine_skip_is_reported_as_the_only_field(self):
        self.assertEqual(
            audit.compare(Skip("mention_or_link"), msg(1), msg(2)), ["engine_skip:mention_or_link"]
        )

    def test_identical_media_copy_matches(self):
        source, dest = msg(1, "chart", PHOTO), msg(2, "chart", PHOTO, reply_to=1364)
        self.assertEqual(audit.compare(send("media", "chart"), source, dest), [])

    def test_each_differing_field_is_named(self):
        bold = (("MessageEntityBold", 0, 2, None),)
        dest = msg(2, "TP1", entities=[MessageEntityItalic(0, 2)], reply_to=1364)
        self.assertEqual(audit.compare(send(entities=bold), msg(1, "TP1"), dest), ["entities"])
        dest = msg(2, "TP1", PHOTO, reply_to=1364)
        self.assertEqual(
            audit.compare(send(), msg(1, "TP1"), dest), ["media"], "text planned, media delivered"
        )
        dest = msg(2, "chart", DOCUMENT, reply_to=1364)
        self.assertEqual(
            audit.compare(send("media", "chart"), msg(1, "chart", PHOTO), dest),
            ["media"],
            "photo planned, document delivered",
        )
        dest = msg(2, "", None, reply_to=None)
        self.assertEqual(sorted(audit.compare(send(), msg(1, "TP1"), dest)), ["reply_to", "text"])

    def test_entities_of_an_empty_copy_are_ignored(self):
        dest = msg(2, "", entities=[MessageEntityBold(0, 1)], reply_to=1364)
        self.assertEqual(audit.compare(send(text=""), msg(1), dest), [])


class NewestMappedTests(unittest.TestCase):
    def test_only_this_routes_valid_pairs_newest_last(self):
        route = Route.from_legacy(ROUTE)
        mapping = {
            f"{RELAY}:30:{VIP}:1364": 9030,
            f"{RELAY}:10:{VIP}:1364": [9010, 9011],
            f"{RELAY}:20:{VIP}:1364": "9020",
            f"{RELAY}:40:{VIP}:1364": [],
            f"{RELAY}:50:{VIP}:1364": "not-an-id",
            f"{RELAY}:x:{VIP}:1364": 1,
            f"{RELAY}:60:{VIP}:7": 9060,
            f"-100999:70:{VIP}:1364": 9070,
        }
        self.assertEqual(audit.newest_mapped(mapping, route, 25), [(10, 9010), (20, 9020), (30, 9030)])
        self.assertEqual(audit.newest_mapped(mapping, route, 2), [(20, 9020), (30, 9030)])


class AuditRouteTests(unittest.TestCase):
    def planner(self, mapping):
        return Planner(rules=Rules(), maps=MessageMap(mapping))

    def test_route_without_mapped_posts_reads_nothing(self):
        reader = Reader({})
        result = asyncio.run(audit.audit_route(reader, self.planner({}), ROUTE, "copy"))
        self.assertEqual((result, reader.reads), ({"checked": 0}, []))

    def test_unavailable_album_and_skipped_posts_are_counted(self):
        mapping = {f"{RELAY}:{i}:{VIP}:1364": 9000 + i for i in (1, 2, 3, 4)}
        stored = {
            (RELAY, 1): msg(1, "TP1", grouped=77),
            (VIP, 9001): msg(9001, "TP1", reply_to=1364),
            (RELAY, 2): msg(2, "TP2"),
            (RELAY, 3): msg(3, "join t.me/x"),
            (VIP, 9003): msg(9003, "join t.me/x", reply_to=1364),
            (RELAY, 4): msg(4, "TP4"),
            (VIP, 9004): msg(9004, "TP4", reply_to=1364),
        }
        result = asyncio.run(audit.audit_route(Reader(stored), self.planner(mapping), ROUTE, "copy"))
        self.assertEqual(result["unavailable_or_album"], 2, "album item and missing copy")
        self.assertEqual((result["checked"], result["match"], result["mismatch"]), (2, 1, 1))
        self.assertEqual(result["field:engine_skip:mention_or_link"], 1)
        self.assertEqual(
            result["examples"],
            [
                {
                    "source": 3,
                    "dest": 9003,
                    "fields": ["engine_skip:mention_or_link"],
                    "engine": "Skip(reason='mention_or_link')",
                    "delivered": "join t.me/x",
                }
            ],
        )


class EditedAfterCopyTests(unittest.TestCase):
    """A post edited after it was copied (4 Nov 2026: Arabic VIP posts 33400/33405 had media changed)."""

    def test_posts_edited_after_the_copy_are_left_to_the_edit_path(self):
        sent = datetime(2026, 11, 4, 15, 1, tzinfo=timezone.utc)
        mapping = {f"{RELAY}:{i}:{VIP}:1364": 9000 + i for i in (1, 2)}
        edited = msg(1, "TP1 with chart", media=PHOTO)
        edited.edit_date = sent + timedelta(minutes=9)
        unchanged = msg(2, "TP2")
        unchanged.edit_date = None
        copies = {9001: msg(9001, "TP1", reply_to=1364), 9002: msg(9002, "TP2", reply_to=1364)}
        for copy in copies.values():
            copy.date = sent
        stored = {(RELAY, 1): edited, (RELAY, 2): unchanged, **{(VIP, i): m for i, m in copies.items()}}
        planner = Planner(rules=Rules(), maps=MessageMap(mapping))
        result = asyncio.run(audit.audit_route(Reader(stored), planner, ROUTE, "copy"))
        self.assertEqual((result["edited_after_copy"], result["checked"], result["match"]), (1, 1, 1))
        self.assertNotIn("mismatch", result)

    def test_edit_before_the_copy_is_still_audited(self):
        sent = datetime(2026, 11, 4, 15, 1, tzinfo=timezone.utc)
        source = SimpleNamespace(edit_date=sent - timedelta(seconds=5))
        self.assertFalse(audit.edited_after_copy(source, SimpleNamespace(date=sent)))
        self.assertFalse(audit.edited_after_copy(SimpleNamespace(), SimpleNamespace(date=sent)))


class RunReplayAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = object()
        mapping = {f"{RELAY}:5:{VIP}:1364": 9005, f"{BROKEN['source_chat']}:6:{VIP}:99": 9006}
        self.reader = Reader(
            {(RELAY, 5): msg(5, "TP1 hit"), (VIP, 9005): msg(9005, "TP1 hit", reply_to=1364)},
            broken=[BROKEN["source_chat"]],
        )
        self.worker = SimpleNamespace(
            DATA_DIR=self.tmp.name, ROUTES=[ROUTE, BROKEN], client=self.reader, EMOJI_REGISTRY=self.registry
        )
        self.planner = Planner(rules=Rules(), maps=MessageMap(mapping))
        self.sleeps = []

    def tearDown(self):
        self.tmp.cleanup()

    def run_audit(self, delay=90.0):
        async def fake_sleep(seconds):
            self.sleeps.append(seconds)

        with patch.object(audit.asyncio, "sleep", fake_sleep):
            return asyncio.run(audit.run_replay_audit(self.worker, self.planner, delay_seconds=delay))

    def test_audit_reports_each_route_once_per_deploy(self):
        with self.assertLogs("engine-audit", "INFO") as logs:
            report = self.run_audit()
        self.assertEqual(report[ROUTE["name"]], {"checked": 1, "match": 1, "examples": []})
        self.assertEqual(report[BROKEN["name"]], {"error": "ConnectionError: source chat unavailable"})
        self.assertIs(self.planner.context["emoji_registry"], self.registry)
        self.assertEqual(self.sleeps, [90.0, 1.0, 1.0])
        by_route = {r.getMessage().split(" checked=")[0]: r.levelno for r in logs.records}
        self.assertEqual(by_route[f"[ENGINE REPLAY AUDIT] route={ROUTE['name']}"], logging.INFO)
        self.assertEqual(by_route[f"[ENGINE REPLAY AUDIT] route={BROKEN['name']}"], logging.WARNING)
        self.assertIn('detail={"error": "ConnectionError: source chat unavailable"}', logs.output[1])
        self.assertIn("[ENGINE REPLAY AUDIT DONE] routes=2 checked=1 match=1", logs.output[-1])

        reads = len(self.reader.reads)
        with self.assertLogs("engine-audit", "INFO") as logs:
            self.assertIsNone(self.run_audit())
        self.assertIn("[ENGINE REPLAY AUDIT SKIPPED]", logs.output[0])
        self.assertEqual(
            (len(self.reader.reads), len(self.sleeps)), (reads, 3), "skipped without reads or wait"
        )

    def test_audit_that_cannot_save_its_state_runs_again(self):
        self.worker.DATA_DIR = str(Path(self.tmp.name) / "no" / "such" / "dir")
        with self.assertLogs("engine-audit", "WARNING"):
            self.assertIsNotNone(self.run_audit(delay=1.5))
            self.assertIsNotNone(self.run_audit(delay=1.5))
        self.assertEqual(self.sleeps.count(1.5), 2)


if __name__ == "__main__":
    unittest.main()

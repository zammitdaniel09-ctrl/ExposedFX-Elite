"""Engine executor: the legacy delivery guarantees, with a fake Telegram client."""

import ast
import asyncio
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import ChatWriteForbiddenError, FloodWaitError
from telethon.tl.types import MessageEntityBold, MessageMediaPhoto, Photo

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import owner_requested_routes_20261102 as owner_routes
from telegram_worker.engine.deliver import Deliverer, structure_problem
from telegram_worker.engine.model import Route
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules
from telegram_worker.permanent_delivery_errors import PermanentFailureLedger, Throttle

VIP, RELAY = -1003726286301, -1004367822325
ROUTE = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
    "loop_guard_peers": [RELAY, VIP],
}


def legacy_map_functions(ns):
    """remember_message/map_key exactly as the worker defines them."""
    tree = ast.parse((ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8"))
    nodes = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in ("map_key", "remember_message", "mapped_ids_from_value", "existing_destination_ids")
    ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "worker_fixed.py", "exec"), ns)
    return ns


def message(msg_id, text="", media=False, grouped=None):
    m = SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=[MessageEntityBold(0, 1)] if text else [],
        media=None,
        grouped_id=grouped,
        reply_to=None,
        reply_to_msg_id=None,
        fwd_from=None,
        sender_id=None,
        from_id=None,
        date=datetime.now(timezone.utc),
    )
    if media:
        m.media = MessageMediaPhoto(
            photo=Photo(id=msg_id, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
        )

        async def download_media(file):
            Path(file).write_bytes(b"jpeg")
            return file

        m.download_media = download_media
    return m


class Client:
    def __init__(self):
        self.calls = []
        self.fail = []  # exceptions to raise on the next calls, in order
        self.deleted = []
        self.next_id = 9000
        self.drop_media = False

    def _result(self, text, media):
        self.next_id += 1
        return SimpleNamespace(
            id=self.next_id,
            message=text,
            raw_text=text,
            text=text,
            media=None if (self.drop_media or not media) else MessageMediaPhoto(),
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

    async def delete_messages(self, chat, ids):
        self.deleted.extend(ids)


class DelivererTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = Client()
        self.saved = []
        self.sleeps = []
        worker = SimpleNamespace(
            client=self.client,
            message_map={},
            DATA_DIR=self.tmp.name,
            EMOJI_REGISTRY=None,
            FASTVIP_PERMANENT_FAILURES=PermanentFailureLedger(Path(self.tmp.name) / "l.json"),
            FASTVIP_IMPAIRED_ALERTS=Throttle(3600),
            send_alert=lambda text: None,
        )
        ns = legacy_map_functions(
            {"message_map": worker.message_map, "save_map": lambda: self.saved.append(1)}
        )
        worker.remember_message = ns["remember_message"]
        worker.save_map = ns["save_map"]
        worker.existing_destination_ids = ns["existing_destination_ids"]
        self.worker = worker
        planner = Planner(
            rules=Rules(blocked_senders=frozenset({7556281143})), maps=MessageMap(worker.message_map)
        )

        async def sleep(seconds):
            self.sleeps.append(seconds)

        self.alerts = []
        self.deliverer = Deliverer(worker, planner, attempts=3, sleep=sleep, alert=self.alerts.append)
        self.route = Route.from_legacy(ROUTE)
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)

    def tearDown(self):
        policy._RUNTIME, policy._PATH = None, None
        self.tmp.cleanup()

    def deliver(self, unit, live=True):
        return asyncio.run(self.deliverer.deliver(ROUTE, self.route, unit, live=live))

    def test_text_post_is_sent_once_and_mapped(self):
        self.assertTrue(self.deliver([message(1, "TP1 hit")]))
        kind, chat, text, kwargs = self.client.calls[0]
        self.assertEqual(
            (kind, chat, text, kwargs["reply_to"], kwargs["link_preview"]),
            ("text", VIP, "TP1 hit", 1364, True),
        )
        self.assertEqual(self.worker.message_map, {f"{RELAY}:1:{VIP}:1364": 9001})
        self.assertTrue(self.deliver([message(1, "TP1 hit")]), "already copied is handled")
        self.assertEqual(len(self.client.calls), 1, "never sent twice")

    def test_media_falls_back_to_reupload_once(self):
        self.client.fail = [RuntimeError("file reference expired")]
        self.assertTrue(self.deliver([message(2, "chart", media=True)]))
        self.assertEqual(len(self.client.calls), 2)
        self.assertTrue(str(self.client.calls[1][2]).endswith("engine_1364_2"))
        self.assertFalse(list(Path(self.tmp.name, "media_cache").iterdir()), "downloads are cleaned up")

    def test_album_is_sent_whole_and_every_item_mapped(self):
        unit = [message(11, "", media=True, grouped=5), message(10, "caption", media=True, grouped=5)]
        self.assertTrue(self.deliver(unit))
        kind, _, files, kwargs = self.client.calls[0]
        self.assertEqual((kind, len(files), kwargs["caption"]), ("file", 2, "caption"))
        self.assertEqual(
            sorted(self.worker.message_map), [f"{RELAY}:10:{VIP}:1364", f"{RELAY}:11:{VIP}:1364"]
        )
        self.assertEqual(self.saved, [1], "album map saved once")

    def test_flood_wait_is_slept_then_retried(self):
        self.client.fail = [FloodWaitError(request=None, capture=7)]
        self.assertTrue(self.deliver([message(3, "TP2 hit")]))
        self.assertEqual(self.sleeps, [8])

    def test_permanent_refusal_is_recorded_and_not_retried(self):
        self.client.fail = [ChatWriteForbiddenError(request=None)]
        with self.assertLogs("imperium-worker", "ERROR") as logs:
            self.assertTrue(self.deliver([message(4, "TP3 hit")]))
        self.assertIn("ROUTE IMPAIRED", logs.output[-1])
        self.assertTrue(self.worker.FASTVIP_PERMANENT_FAILURES.contains_all(ROUTE, [4]))
        self.client.fail = [ChatWriteForbiddenError(request=None)]
        self.assertTrue(self.deliver([message(4, "TP3 hit")]))
        self.assertEqual(len(self.client.calls), 1, "recalled failure is not retried")

    def test_transient_error_retries_then_reports_failure(self):
        self.client.fail = [ConnectionError("connection reset")] * 3
        with self.assertLogs("imperium-worker", "ERROR"):
            self.assertFalse(self.deliver([message(5, "TP4 hit")]))
        self.assertEqual(len(self.client.calls), 3)
        self.assertEqual(self.worker.message_map, {})

    def test_wrong_structure_is_deleted_resent_and_never_mapped_if_still_wrong(self):
        self.client.drop_media = True
        with self.assertLogs("imperium-worker", "ERROR"):
            self.assertFalse(self.deliver([message(6, "chart", media=True)], live=False))
        # As in legacy, a structure mismatch is retryable: 3 attempts x (copy + repair), all deleted.
        self.assertEqual(len(self.client.deleted), 6)
        self.assertEqual(self.worker.message_map, {})

    def test_retired_paths_are_never_written(self):
        vip5 = dict(ROUTE, name="probe VIP5", dest_topic=5)
        relay7250 = dict(ROUTE, name="probe 7250", source_topic=7250)
        for legacy in (vip5, relay7250):
            with self.assertLogs("imperium-worker", "WARNING") as logs:
                self.assertTrue(
                    asyncio.run(
                        self.deliverer.deliver(legacy, Route.from_legacy(legacy), [message(80, "TP1")])
                    )
                )
            self.assertTrue(any("BLOCK" in line for line in logs.output))
        self.assertEqual(self.client.calls, [])
        allowed = dict(ROUTE, name="Owner Relay7250 To VIP12161", source_topic=7250, dest_topic=12161)
        self.assertTrue(
            asyncio.run(self.deliverer.deliver(allowed, Route.from_legacy(allowed), [message(81, "TP1")]))
        )
        self.assertEqual(len(self.client.calls), 1, "7250 -> VIP12161 is the one allowed path")

    def test_unsupported_post_is_handled_without_sending(self):
        self.assertTrue(self.deliver([message(70, "")]))
        self.assertEqual(self.client.calls, [])

    def test_media_fallback_never_masks_flood_or_lost_access(self):
        self.client.fail = [ChatWriteForbiddenError(request=None)]
        with self.assertLogs("imperium-worker", "ERROR"):
            self.deliver([message(71, "chart", media=True)])
        self.assertEqual(len(self.client.calls), 1, "no re-upload after a permanent refusal")
        policy._RUNTIME.clear()
        self.client.calls.clear()
        self.client.fail = [FloodWaitError(request=None, capture=3)]
        self.assertTrue(self.deliver([message(72, "chart", media=True)]))
        self.assertEqual(
            [c[2] for c in self.client.calls][0], self.client.calls[1][2], "flood: same media retried"
        )

    def test_blocked_posts_are_skipped_without_sending(self):
        post = message(7, "join t.me/x")
        self.assertTrue(self.deliver([post]))
        self.assertEqual(self.client.calls, [])

    def test_owner_route_contract(self):
        route = dict(
            ROUTE,
            name="Owner VIP1364 To Partner28",
            source_chat=VIP,
            source_topic=1364,
            dest_chat=-1003786932333,
            dest_topic=28,
            owner_managed=True,
        )
        route.pop("loop_guard_peers")
        engine_route = Route.from_legacy(route)
        status, ids = asyncio.run(
            self.deliverer.copy_unit(owner_routes, route, engine_route, [message(8, "TP5")])
        )
        self.assertEqual((status, ids), ("sent", [9001]))
        status, ids = asyncio.run(
            self.deliverer.copy_unit(owner_routes, route, engine_route, [message(8, "TP5")])
        )
        self.assertEqual(status, "existing")
        status, _ = asyncio.run(
            self.deliverer.copy_unit(owner_routes, route, engine_route, [message(9, "t.me/x")])
        )
        self.assertEqual(status, "filtered")

    def test_vip7_reply_recovers_a_missed_signal_parent_first(self):
        from datetime import timedelta
        from telegram_worker import reply_hardening
        from telegram_worker.community_emoji_registry import CommunityEmojiRegistry

        registry = CommunityEmojiRegistry(Path(self.tmp.name))
        registry.entries = {
            a: {"type": "custom", "document_id": 5000 + i, "fallback": "✅"}
            for i, a in enumerate(("Boom", "GreenTick", "RedCross", "Warning"))
        }
        self.worker.EMOJI_REGISTRY = registry
        vip7 = {
            "name": "Relay508 To VIP7",
            "source_chat": RELAY,
            "source_topic": 508,
            "dest_chat": VIP,
            "dest_topic": 7,
        }
        route = Route.from_legacy(vip7, formatter="imperium_vip")
        parent = message(40, "Buy xauusd now\n\nTp 4324\nTp 4337\n\nSl 4308")
        parent.date = datetime.now(timezone.utc) - timedelta(hours=2)
        reply = message(41, "Tp1 hit ✅")
        reply.reply_to = SimpleNamespace(reply_to_msg_id=40, reply_to_top_id=508, top_msg_id=None)
        reply.reply_to_msg_id = 40

        async def get_messages(chat, ids):
            return parent if ids == 40 else None

        async def no_mapping(*args, **kwargs):
            return None

        self.client.get_messages = get_messages
        original = reply_hardening.ensure_reply_mapping
        reply_hardening.ensure_reply_mapping = no_mapping
        try:
            self.assertTrue(asyncio.run(self.deliverer.deliver(vip7, route, [reply])))
        finally:
            reply_hardening.ensure_reply_mapping = original
        self.assertEqual(len(self.client.calls), 2, "parent signal first, then the reply")
        parent_id = self.worker.message_map[f"{RELAY}:40:{VIP}:7"]
        self.assertEqual(self.client.calls[1][3]["reply_to"], parent_id)
        self.assertNotEqual(self.client.calls[0][2], parent.message, "the parent is restyled for VIP 7")

    def test_vip7_does_not_import_an_old_or_non_signal_parent(self):
        from datetime import timedelta
        from telegram_worker import reply_hardening

        vip7 = {
            "name": "Relay508 To VIP7",
            "source_chat": RELAY,
            "source_topic": 508,
            "dest_chat": VIP,
            "dest_topic": 7,
        }
        route = Route.from_legacy(vip7, formatter="imperium_vip")
        old_parent = message(50, "Buy xauusd now\n\nTp 4324\n\nSl 4308")
        old_parent.date = datetime.now(timezone.utc) - timedelta(days=2)
        reply = message(51, "Tp1 hit")
        reply.reply_to = SimpleNamespace(reply_to_msg_id=50, reply_to_top_id=508, top_msg_id=None)
        reply.reply_to_msg_id = 50

        async def get_messages(chat, ids):
            return old_parent

        async def no_mapping(*args, **kwargs):
            return None

        self.client.get_messages = get_messages
        original = reply_hardening.ensure_reply_mapping
        reply_hardening.ensure_reply_mapping = no_mapping
        try:
            with self.assertLogs("imperium-worker", "WARNING"):
                self.assertTrue(asyncio.run(self.deliverer.deliver(vip7, route, [reply])))
        finally:
            reply_hardening.ensure_reply_mapping = original
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.client.calls[0][3]["reply_to"], 7, "falls back to the topic")

    def test_structure_check(self):
        source = message(1, "a  b", media=True)
        self.assertIsNone(
            structure_problem(
                source,
                SimpleNamespace(message="a b", raw_text="a b", text="a b", media=MessageMediaPhoto()),
                "a  b",
            )
        )
        self.assertEqual(structure_problem(source, None, "a"), "no_sent_message")


if __name__ == "__main__":
    unittest.main()

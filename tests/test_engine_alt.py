"""ALT on the delivery engine: ALT's own rules, translation, checkpoints and markers."""

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import FloodWaitError
from telethon.tl.types import MessageEntityBold, MessageMediaPhoto, Photo, PeerChannel

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
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


def post(msg_id, text="", media=False, grouped=None, sender=None, fwd=None):
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
        fwd_from=fwd,
        sender_id=sender,
        from_id=None,
        date=datetime.now(timezone.utc),
    )
    if media:
        m.media = MessageMediaPhoto(
            photo=Photo(id=msg_id, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
        )
    return m


class Client:
    def __init__(self):
        self.calls, self.fail, self.next_id = [], [], 9000

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


class AltEngineTests(unittest.TestCase):
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
            if legacy.alt_arabic115_route(route) and message.message:  # like the real one: no text, no call
                return "EN: " + message.message, [MessageEntityBold(0, 3)]
            return message.message, message.entities

        def remember(route, src, dst):
            items = dst if isinstance(dst, list) else [dst]
            legacy.message_map[
                f"{route['source_chat']}:{src.id}:{route['dest_chat']}:{route['dest_topic']}"
            ] = items[0].id
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

        self._resolve = alt_engine.resolve_reply_target
        alt_engine.resolve_reply_target = resolve
        self.engine = alt_engine.AltEngine(legacy)

    def tearDown(self):
        alt_engine.resolve_reply_target = self._resolve
        policy._RUNTIME, policy._PATH = None, None
        self.tmp.cleanup()

    def deliver(self, route, unit, reason="telegram_push"):
        return asyncio.run(self.engine.deliver(route, unit, reason))

    def test_copy_is_exact_and_advances_the_checkpoint(self):
        self.assertTrue(self.deliver(SELECT, [post(10, "Buy XAUUSD 4180")]))
        kind, chat, text, kwargs = self.client.calls[0]
        self.assertEqual(
            (kind, chat, text, kwargs["reply_to"], kwargs["link_preview"]),
            ("text", RELAY, "Buy XAUUSD 4180", 8323, True),
        )
        self.assertEqual(self.checkpoints[SELECT["name"]], 10)
        self.assertTrue(self.deliver(SELECT, [post(10, "Buy XAUUSD 4180")]), "already copied is handled")
        self.assertEqual(len(self.client.calls), 1)

    def test_reply_target_comes_from_alt_reply_recovery(self):
        self.reply_target = 8400
        self.deliver(SELECT, [post(11, "TP1 hit")])
        self.assertEqual(self.client.calls[0][3]["reply_to"], 8400)

    def test_mentions_and_links_are_skipped_and_checkpointed(self):
        self.assertTrue(self.deliver(SELECT, [post(12, "join t.me/x")]))
        self.assertEqual((self.client.calls, self.filtered, self.checkpoints[SELECT["name"]]), ([], [12], 12))

    def test_alt_never_blocks_senders_or_forward_loops(self):
        forwarded = SimpleNamespace(from_id=PeerChannel(4367822325), saved_from_peer=None)
        self.assertTrue(self.deliver(SELECT, [post(13, "TP2 hit", sender=7556281143, fwd=forwarded)]))
        self.assertEqual(len(self.client.calls), 1, "MAIN's sender block and loop guard do not apply to ALT")

    def test_t4t_route_sends_the_translation_and_only_translates_what_legacy_did(self):
        self.assertTrue(self.deliver(ARABIC, [post(20, "شراء الذهب")]))
        self.assertEqual(self.client.calls[0][2], "EN: شراء الذهب")
        self.client.calls.clear()
        self.translated.clear()
        album = [post(22, "", media=True, grouped=7), post(21, "تحليل", media=True, grouped=7)]
        self.assertTrue(self.deliver(ARABIC, album))
        self.assertEqual(self.translated, [21], "only the album caption is translated")
        self.assertEqual(self.client.calls[0][3]["caption"], "EN: تحليل")
        self.assertEqual(self.translated.count(21), 1)

    def test_untranslated_select_route_makes_no_translation_call(self):
        self.deliver(SELECT, [post(30, "Sell now")])
        self.assertEqual(self.translated, [])

    def test_flood_wait_hands_the_post_back_to_the_watchdog_at_once(self):
        self.client.fail = [FloodWaitError(request=None, capture=40)]
        with self.assertLogs("imperium-alt-worker", "WARNING") as logs:
            self.assertFalse(self.deliver(SELECT, [post(40, "TP3 hit")]))
        self.assertTrue(any("[ALT FLOODWAIT]" in line for line in logs.output))
        self.assertNotIn(SELECT["name"], self.checkpoints, "not handled, so the watchdog retries it")

    def test_album_maps_every_item(self):
        album = [post(51, "", media=True, grouped=9), post(50, "chart", media=True, grouped=9)]
        self.assertTrue(self.deliver(SELECT, album))
        keys = sorted(k for k in self.legacy.message_map)
        self.assertEqual(keys, [f"-1004410975295:50:{RELAY}:8323", f"-1004410975295:51:{RELAY}:8323"])
        self.assertEqual(self.checkpoints[SELECT["name"]], 51)


if __name__ == "__main__":
    unittest.main()


class AltDifferentialTests(unittest.TestCase):
    """The engine makes exactly the Telegram call ALT's legacy send_single made (frozen oracle)."""

    def legacy_send_single(self, client, translated_payload):
        import ast as _ast

        tree = _ast.parse((ROOT / "tests/fixtures/legacy_alt_send_20261104.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, _ast.AsyncFunctionDef) and n.name == "send_single")
        from telegram_worker.alt_reply_hardening import _reply_target_invalid, _reupload_cannot_help

        async def resolve(worker, route, message, logger=None):
            return route["dest_topic"]

        ns = {
            "owner_policy": policy,
            "alt_vip5_block": lambda route: False,
            "client": client,
            "has_username_mention": lambda m: False,
            "translated_payload": translated_payload,
            "real_media": lambda m: m.media is not None,
            "resolve_reply_target": resolve,
            "_reply_target_invalid": _reply_target_invalid,
            "_reupload_cannot_help": _reupload_cannot_help,
            "_WORKER": None,
            "log": __import__("logging").getLogger("alt-diff"),
            "Path": Path,
        }
        exec(compile(_ast.Module(body=[node], type_ignores=[]), "alt_worker_legacy.py", "exec"), ns)
        return ns["send_single"]

    def test_engine_matches_legacy_send_single(self):
        from telegram_worker.engine.model import entity_keys

        cases = [
            post(100, "Buy XAUUSD 4180"),
            post(101, "chart", media=True),
            post(102, "", media=True),
            post(103, "SELL LIMIT 4192.4\nTP1 4188"),
        ]
        for route in (SELECT, ARABIC):
            for case in cases:
                legacy_client, engine_client = Client(), Client()
                with tempfile.TemporaryDirectory() as tmp:
                    harness = AltEngineTests("test_copy_is_exact_and_advances_the_checkpoint")
                    harness.setUp()
                    try:
                        harness.legacy.client = legacy_client
                        legacy_single = self.legacy_send_single(
                            legacy_client, harness.legacy.translated_payload
                        )
                        asyncio.run(legacy_single(route, case))
                        harness.client = engine_client
                        harness.legacy.client = engine_client
                        harness.engine = alt_engine.AltEngine(harness.legacy)
                        asyncio.run(harness.engine.deliver(route, [case], "diff"))
                    finally:
                        harness.tearDown()

                def normal(call):
                    kind, chat, payload, kwargs = call
                    text = payload if kind == "text" else kwargs.get("caption") or ""
                    entities = kwargs.get("formatting_entities")
                    return (kind, chat, text, entity_keys(entities if text else None), kwargs["reply_to"])

                self.assertEqual(
                    [normal(c) for c in engine_client.calls],
                    [normal(c) for c in legacy_client.calls],
                    (route["name"], case.id),
                )

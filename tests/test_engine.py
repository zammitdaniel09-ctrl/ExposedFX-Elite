"""Delivery engine: differential tests against the legacy pipeline's own code.

The legacy rules (frozen in tests/fixtures/legacy_rules_20261104.py) and the
reply functions in reply_hardening.py are executed and compared with the
engine on a generated corpus; the VIP7 formatter and the replay audit too.
"""

import asyncio
import itertools
import logging
import random
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCustomEmoji,
    MessageEntityMention,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageMediaPhoto,
    MessageMediaWebPage,
    PeerChannel,
    PeerUser,
    Photo,
)

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import reply_hardening
from telegram_worker.engine.model import Route, Send, Skip, entity_keys
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap, reply_target
from telegram_worker.engine.rules import Rules, forward_origins, sender_ids

VIP, RELAY = -1003726286301, -1004367822325
LEGACY_ORACLE = ROOT / "tests" / "fixtures" / "legacy_rules_20261104.py"


def legacy_worker():
    """Execute the frozen legacy rule definitions into a namespace."""
    ns = {
        "os": __import__("os"),
        "re": re,
        "log": logging.getLogger("legacy-test"),
        "BLOCKED_SENDER_IDS": {7556281143},
    }
    exec(compile(LEGACY_ORACLE.read_text(encoding="utf-8"), str(LEGACY_ORACLE), "exec"), ns)
    return ns


LEGACY = legacy_worker()
logging.getLogger("legacy-test").setLevel(logging.CRITICAL)
RULES = Rules(blocked_senders=frozenset({7556281143}))

TEXTS = [
    "",
    "XAUUSD BUY NOW 4180.250",
    "TP1 4185 ✅",
    "Join @goldsignals for more",
    "@abc hi",
    "contact x@yahoo.com",
    "www.goldsite.com",
    "see t.me/joinchat/abc",
    "https://example.org/x",
    "Gold.Analysis today",
    "SL 4170.50 | TP 4200",
    "visit goldfx.io/vip now",
    "mail@site.co",
    "4180.250/4183.955",
    "SELL LIMIT 4192.4\nTP1 4188",
    "@Gold_Trader_VIP",
    "e.g. close 50%",
]
FWD = [None, PeerChannel(2521699926), PeerChannel(4367822325), PeerChannel(3838973021), PeerUser(7556281143)]


def make_entities(text, rnd):
    out = []
    if text and rnd.random() < 0.4:
        out.append(MessageEntityBold(0, min(3, len(text))))
    if text and rnd.random() < 0.2:
        out.append(MessageEntityCustomEmoji(0, 1, document_id=5398001711786762757))
    if text and rnd.random() < 0.15:
        out.append(MessageEntityMention(0, min(4, len(text))))
    if text and rnd.random() < 0.15:
        out.append(MessageEntityUrl(0, min(4, len(text))))
    if text and rnd.random() < 0.15:
        out.append(MessageEntityTextUrl(0, 1, url=rnd.choice(["mailto:a@b.c", "https://x.y"])))
    return out


def corpus(n=600, seed=7):
    rnd = random.Random(seed)
    for i in range(n):
        text = rnd.choice(TEXTS)
        fwd = rnd.choice(FWD)
        yield SimpleNamespace(
            id=1000 + i,
            message=text,
            raw_text=text,
            text=text,
            entities=make_entities(text, rnd),
            fwd_from=SimpleNamespace(from_id=fwd, saved_from_peer=rnd.choice([None, fwd])) if fwd else None,
            sender_id=rnd.choice([None, 7556281143, 123]),
            from_id=rnd.choice([None, PeerUser(7556281143), PeerChannel(4367822325)]),
            media=None,
            grouped_id=None,
            reply_to=None,
        )


ROUTE_DICTS = [
    {
        "name": "Relay508 To VIP7",
        "source_chat": RELAY,
        "source_topic": 508,
        "dest_chat": VIP,
        "dest_topic": 7,
        "loop_guard_peers": [RELAY, VIP],
    },
    {
        "name": "Private Live 3838973021 To 9",
        "source_chat": -1003838973021,
        "source_topic": None,
        "dest_chat": VIP,
        "dest_topic": 9,
        "loop_guard_peers": [VIP],
    },
    {
        "name": "Owner VIP1364 To Partner28",
        "source_chat": VIP,
        "source_topic": 1364,
        "dest_chat": -1003786932333,
        "dest_topic": 28,
    },
    {
        "name": "Hard block probe",
        "source_chat": -1002521699926,
        "source_topic": None,
        "dest_chat": VIP,
        "dest_topic": 11,
    },
]


class RuleEquivalenceTests(unittest.TestCase):
    """Each engine rule agrees with the legacy function on every corpus message."""

    def test_content_filter_matches_legacy(self):
        for m in corpus():
            self.assertEqual(RULES.blocked_content(m), LEGACY["has_username_mention"](m), m.message)

    def test_sender_and_origin_helpers_match_legacy(self):
        for m in corpus():
            self.assertEqual(sender_ids(m), LEGACY["sender_ids_for_message"](m))
            self.assertEqual(forward_origins(m), LEGACY["forward_origin_chat_ids"](m))
            self.assertEqual(RULES.blocked_sender(m), LEGACY["should_hard_block_sender"](m, "t"))

    def test_route_rules_match_legacy(self):
        for m, route in itertools.product(corpus(200), ROUTE_DICTS):
            engine_route = Route.from_legacy(route)
            self.assertEqual(
                RULES.vip_hard_block(m, engine_route),
                LEGACY["should_hard_block_2521699926_from_vip"](m, route),
            )
            self.assertEqual(RULES.loop(m, engine_route), LEGACY["should_block_private_peer_loop"](m, route))


def reply_message(msg_id, parent=None, top=None):
    reply = (
        SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=top, top_msg_id=None)
        if parent or top
        else None
    )
    return SimpleNamespace(
        id=msg_id,
        reply_to=reply,
        reply_to_msg_id=parent,
        message="x",
        raw_text="x",
        text="x",
        entities=[],
        media=None,
        grouped_id=None,
        fwd_from=None,
        sender_id=None,
        from_id=None,
    )


class ReplyEquivalenceTests(unittest.TestCase):
    def test_reply_target_matches_reply_hardening(self):
        route = ROUTE_DICTS[0]
        message_map = {
            f"{RELAY}:100:{VIP}:7": 9100,
            f"{RELAY}:101:{VIP}:7": [9101, 9102],
            f"{RELAY}:102:{VIP}:7": {"a": 9103},
            f"{RELAY}:103:{VIP}:7": 0,
        }
        worker = SimpleNamespace(
            message_map=message_map,
            map_key=LEGACY["map_key"],
            mapped_ids_from_value=LEGACY["mapped_ids_from_value"],
        )
        maps = MessageMap(message_map)
        cases = [
            reply_message(1),
            reply_message(2, parent=100),
            reply_message(3, parent=101, top=508),
            reply_message(4, parent=102),
            reply_message(5, parent=103),
            reply_message(6, parent=999, top=508),
            reply_message(7, parent=508),
            reply_message(8, top=100),
        ]
        for m in cases:
            parents = reply_hardening.real_reply_source_ids(m, route)
            legacy = (
                (reply_hardening.mapped_reply_id(worker, m, route) or route["dest_topic"])
                if parents
                else route["dest_topic"]
            )
            self.assertEqual(reply_target(m, Route.from_legacy(route), maps), legacy, m.id)


def photo_message(msg_id, caption="", grouped=None):
    m = reply_message(msg_id)
    m.message = m.raw_text = m.text = caption
    m.media = MessageMediaPhoto(
        photo=Photo(id=msg_id * 10, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
    )
    m.grouped_id = grouped
    return m


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.message_map = {}
        self.planner = Planner(rules=RULES, maps=MessageMap(self.message_map))
        self.route = Route.from_legacy(ROUTE_DICTS[2])

    def test_text_post_keeps_text_entities_and_goes_to_the_topic(self):
        m = reply_message(10)
        m.message = m.raw_text = m.text = "TP1 hit ✅"
        m.entities = [MessageEntityBold(0, 3)]
        plan = self.planner.plan(self.route, [m])
        self.assertEqual(plan, Send("text", -1003786932333, 28, "TP1 hit ✅", entity_keys(m.entities)))

    def test_album_is_one_send_with_the_first_caption(self):
        unit = [photo_message(21, "", 5), photo_message(20, "Chart", 5)]
        plan = self.planner.plan(self.route, unit)
        self.assertEqual(
            (plan.kind, plan.text, plan.media), ("album", "Chart", (("photo", 200), ("photo", 210)))
        )

    def test_already_copied_and_partial_albums_are_not_resent(self):
        m = reply_message(30)
        self.message_map[self.route.map_key(30)] = 777
        self.assertEqual(self.planner.plan(self.route, [m]), Skip("already_copied"))
        self.message_map[self.route.map_key(40)] = 778
        self.assertEqual(
            self.planner.plan(self.route, [photo_message(40, "", 9), photo_message(41, "", 9)]),
            Skip("partial_album_mapping"),
        )

    def test_web_preview_only_post_is_text(self):
        m = reply_message(50)
        m.message = m.raw_text = m.text = "Analysis"
        m.media = MessageMediaWebPage(webpage=None)
        self.assertEqual(self.planner.plan(self.route, [m]).kind, "text")

    def test_empty_unsupported_post_is_skipped(self):
        m = reply_message(60)
        m.message = m.raw_text = m.text = ""
        self.assertEqual(self.planner.plan(self.route, [m]), Skip("empty_unsupported"))


class ImperiumFormatterTests(unittest.TestCase):
    """VIP topic 7: the engine renders exactly what the legacy inline hook sends."""

    def test_signal_and_update_match_the_inline_hook(self):
        from telegram_worker import imperium_vip_inline_hook as hook
        from telegram_worker.community_emoji_registry import CommunityEmojiRegistry

        with tempfile.TemporaryDirectory() as tmp:
            registry = CommunityEmojiRegistry(Path(tmp))
            registry.entries = {
                alias: {"type": "custom", "document_id": 5000 + i, "fallback": "✅"}
                for i, alias in enumerate(("Boom", "GreenTick", "RedCross", "Warning"))
            }
            route = Route.from_legacy(ROUTE_DICTS[0], formatter="imperium_vip")
            planner = Planner(rules=RULES, maps=MessageMap({}), context={"emoji_registry": registry})
            for i, text in enumerate(
                (
                    "Buy xauusd now\n\nTp 4324\nTp 4337\nTp open\n\nSl 4308",
                    "Tp1 hit ✅",
                    "Set breakeven",
                    "Plain chat, not a signal",
                )
            ):
                m = reply_message(700 + i)
                m.message = m.raw_text = m.text = text
                legacy_message, _ = hook._clone_message_with_format(m, registry)
                plan = planner.plan(route, [m])
                self.assertEqual(
                    (plan.text, plan.entities),
                    (legacy_message.message, entity_keys(legacy_message.entities)),
                    text,
                )


class ReplayAuditTests(unittest.TestCase):
    """The audit compares the engine with copies already in the destination."""

    def run_audit(self, source, dest, message_map):
        from telegram_worker.engine import audit

        class Reader:
            async def get_messages(self, chat, ids):
                return [source if chat == route["source_chat"] else dest for _ in ids]

        route = ROUTE_DICTS[2]
        planner = Planner(rules=RULES, maps=MessageMap(message_map))
        return asyncio.run(audit.audit_route(Reader(), planner, route, "copy"))

    def delivered(self, source, reply_to):
        dest = reply_message(9000)
        dest.message, dest.entities = source.message, list(source.entities)
        dest.reply_to = SimpleNamespace(reply_to_msg_id=reply_to)
        return dest

    def test_identical_copy_matches(self):
        source = reply_message(20)
        source.message = source.raw_text = source.text = "TP1 ✅"
        source.entities = [MessageEntityBold(0, 3)]
        mapping = {f"{VIP}:20:-1003786932333:28": 9000}
        result = self.run_audit(source, self.delivered(source, 28), mapping)
        self.assertEqual((result["checked"], result["match"]), (1, 1))

    def test_wrong_topic_or_text_is_reported(self):
        source = reply_message(21)
        source.message = source.raw_text = source.text = "TP2 ✅"
        dest = self.delivered(source, 999)
        dest.message = "TP2"
        result = self.run_audit(source, dest, {f"{VIP}:21:-1003786932333:28": 9000})
        self.assertEqual(result["mismatch"], 1)
        self.assertEqual(sorted(result["examples"][0]["fields"]), ["reply_to", "text"])


if __name__ == "__main__":
    unittest.main()

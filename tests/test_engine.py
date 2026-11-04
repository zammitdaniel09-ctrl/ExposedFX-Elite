"""Delivery engine: differential tests against the legacy pipeline's own code.

The legacy rule and reply functions are executed straight from worker_fixed.py
and reply_hardening.py (no worker import, no credentials) and compared with the
engine on a generated corpus, then shadow mode is exercised end to end.
"""
import ast
import asyncio
import itertools
import json
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
    MessageEntityBold, MessageEntityCustomEmoji, MessageEntityMention, MessageEntityTextUrl,
    MessageEntityUrl, MessageMediaPhoto, MessageMediaWebPage, PeerChannel, PeerUser, Photo,
)

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import reply_hardening
from telegram_worker.engine import shadow as shadow_mod
from telegram_worker.engine.model import Route, Send, Skip, entity_keys
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap, reply_target
from telegram_worker.engine.rules import Rules, forward_origins, sender_ids

VIP, RELAY = -1003726286301, -1004367822325
LEGACY_NAMES = {
    "text_of", "FILTER_USERNAME_MENTIONS", "USERNAME_HANDLE_RE", "has_username_mention", "FILTER_LINKS",
    "URL_SCHEME_RE", "BARE_DOMAIN_RE", "_original_has_username_mention_v2", "has_link",
    "unit_has_username_mention", "sender_ids_for_message", "_marked_peer_id", "forward_origin_chat_ids",
    "should_block_private_peer_loop", "VIP_HARD_BLOCK_CHAT", "VIP_HARD_BLOCKED_SOURCE",
    "should_hard_block_2521699926_from_vip", "should_hard_block_sender", "map_key",
    "mapped_ids_from_value",
}


def legacy_worker():
    """Execute the legacy rule definitions, in file order, into a namespace."""
    tree = ast.parse((ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8"))
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in LEGACY_NAMES:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in LEGACY_NAMES
                                                  for t in node.targets):
            body.append(node)
    ns = {"os": __import__("os"), "re": re, "log": logging.getLogger("legacy-test"),
          "BLOCKED_SENDER_IDS": {7556281143}}
    exec(compile(ast.Module(body=body, type_ignores=[]), "worker_fixed.py", "exec"), ns)
    return ns


LEGACY = legacy_worker()
logging.getLogger("legacy-test").setLevel(logging.CRITICAL)
RULES = Rules(blocked_senders=frozenset({7556281143}))

TEXTS = [
    "", "XAUUSD BUY NOW 4180.250", "TP1 4185 ✅", "Join @goldsignals for more", "@abc hi",
    "contact x@yahoo.com", "www.goldsite.com", "see t.me/joinchat/abc", "https://example.org/x",
    "Gold.Analysis today", "SL 4170.50 | TP 4200", "visit goldfx.io/vip now", "mail@site.co",
    "4180.250/4183.955", "SELL LIMIT 4192.4\nTP1 4188", "@Gold_Trader_VIP", "e.g. close 50%",
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
            id=1000 + i, message=text, raw_text=text, text=text,
            entities=make_entities(text, rnd),
            fwd_from=SimpleNamespace(from_id=fwd, saved_from_peer=rnd.choice([None, fwd])) if fwd else None,
            sender_id=rnd.choice([None, 7556281143, 123]),
            from_id=rnd.choice([None, PeerUser(7556281143), PeerChannel(4367822325)]),
            media=None, grouped_id=None, reply_to=None,
        )


ROUTE_DICTS = [
    {"name": "Relay508 To VIP7", "source_chat": RELAY, "source_topic": 508, "dest_chat": VIP, "dest_topic": 7,
     "loop_guard_peers": [RELAY, VIP]},
    {"name": "Private Live 3838973021 To 9", "source_chat": -1003838973021, "source_topic": None,
     "dest_chat": VIP, "dest_topic": 9, "loop_guard_peers": [VIP]},
    {"name": "Owner VIP1364 To Partner28", "source_chat": VIP, "source_topic": 1364,
     "dest_chat": -1003786932333, "dest_topic": 28},
    {"name": "Hard block probe", "source_chat": -1002521699926, "source_topic": None, "dest_chat": VIP, "dest_topic": 11},
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
            self.assertEqual(RULES.vip_hard_block(m, engine_route),
                             LEGACY["should_hard_block_2521699926_from_vip"](m, route))
            self.assertEqual(RULES.loop(m, engine_route), LEGACY["should_block_private_peer_loop"](m, route))


def reply_message(msg_id, parent=None, top=None):
    reply = SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=top, top_msg_id=None) if parent or top else None
    return SimpleNamespace(id=msg_id, reply_to=reply, reply_to_msg_id=parent, message="x", raw_text="x",
                           text="x", entities=[], media=None, grouped_id=None, fwd_from=None,
                           sender_id=None, from_id=None)


class ReplyEquivalenceTests(unittest.TestCase):
    def test_reply_target_matches_reply_hardening(self):
        route = ROUTE_DICTS[0]
        message_map = {
            f"{RELAY}:100:{VIP}:7": 9100,
            f"{RELAY}:101:{VIP}:7": [9101, 9102],
            f"{RELAY}:102:{VIP}:7": {"a": 9103},
            f"{RELAY}:103:{VIP}:7": 0,
        }
        worker = SimpleNamespace(message_map=message_map, map_key=LEGACY["map_key"],
                                 mapped_ids_from_value=LEGACY["mapped_ids_from_value"])
        maps = MessageMap(message_map)
        cases = [reply_message(1), reply_message(2, parent=100), reply_message(3, parent=101, top=508),
                 reply_message(4, parent=102), reply_message(5, parent=103), reply_message(6, parent=999, top=508),
                 reply_message(7, parent=508), reply_message(8, top=100)]
        for m in cases:
            parents = reply_hardening.real_reply_source_ids(m, route)
            legacy = (reply_hardening.mapped_reply_id(worker, m, route) or route["dest_topic"]) if parents \
                else route["dest_topic"]
            self.assertEqual(reply_target(m, Route.from_legacy(route), maps), legacy, m.id)


def photo_message(msg_id, caption="", grouped=None):
    m = reply_message(msg_id)
    m.message = m.raw_text = m.text = caption
    m.media = MessageMediaPhoto(photo=Photo(id=msg_id * 10, access_hash=1, file_reference=b"", date=None,
                                            sizes=[], dc_id=1))
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
        self.assertEqual((plan.kind, plan.text, plan.media), ("album", "Chart", (("photo", 200), ("photo", 210))))

    def test_already_copied_and_partial_albums_are_not_resent(self):
        m = reply_message(30)
        self.message_map[self.route.map_key(30)] = 777
        self.assertEqual(self.planner.plan(self.route, [m]), Skip("already_copied"))
        self.message_map[self.route.map_key(40)] = 778
        self.assertEqual(self.planner.plan(self.route, [photo_message(40, "", 9), photo_message(41, "", 9)]),
                         Skip("partial_album_mapping"))

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
            registry.entries = {alias: {"type": "custom", "document_id": 5000 + i, "fallback": "✅"}
                                for i, alias in enumerate(("Boom", "GreenTick", "RedCross", "Warning"))}
            route = Route.from_legacy(ROUTE_DICTS[0], formatter="imperium_vip")
            planner = Planner(rules=RULES, maps=MessageMap({}), context={"emoji_registry": registry})
            for i, text in enumerate(("Buy xauusd now\n\nTp 4324\nTp 4337\nTp open\n\nSl 4308",
                                      "Tp1 hit ✅", "Set breakeven", "Plain chat, not a signal")):
                m = reply_message(700 + i)
                m.message = m.raw_text = m.text = text
                legacy_message, _ = hook._clone_message_with_format(m, registry)
                plan = planner.plan(route, [m])
                self.assertEqual((plan.text, plan.entities),
                                 (legacy_message.message, entity_keys(legacy_message.entities)), text)


class FakeClient:
    def __init__(self):
        self.sent = []

    async def send_message(self, entity, message, **kwargs):
        self.sent.append(("text", entity, message, kwargs))
        return SimpleNamespace(id=5000 + len(self.sent))

    async def send_file(self, entity, file, **kwargs):
        self.sent.append(("file", entity, file, kwargs))
        return SimpleNamespace(id=5000 + len(self.sent))


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client = FakeClient()
        self.worker = SimpleNamespace(client=self.client, message_map={}, DATA_DIR=self.tmp.name,
                                      FASTVIP_PERMANENT_FAILURES=None, EMOJI_REGISTRY=None)
        self.behaviour = None

        async def legacy_deliver(route, unit, skip_permanent_errors=False):
            return await self.behaviour(route, unit)

        self.worker.fast_vip_deliver_route = legacy_deliver
        self.shadow = shadow_mod.install_shadow(self.worker)
        self.route = dict(ROUTE_DICTS[2], name="Owner VIP1364 To Partner28")

    def tearDown(self):
        self.tmp.cleanup()

    def deliver(self, unit):
        async def run():
            result = await self.worker.fast_vip_deliver_route(self.route, unit, skip_permanent_errors=True)
            await asyncio.sleep(0)
            return result
        asyncio.run(run())
        return self.shadow.totals.get(self.route["name"], {})

    def post(self, msg_id, text):
        m = reply_message(msg_id)
        m.message = m.raw_text = m.text = text
        m.entities = [MessageEntityBold(0, 2)]
        return m

    def test_identical_send_is_a_match_and_the_send_still_happens(self):
        async def legacy(route, unit):
            await self.client.send_message(route["dest_chat"], unit[0].message, formatting_entities=unit[0].entities,
                                           parse_mode=None, reply_to=route["dest_topic"], link_preview=True)
            return True
        self.behaviour = legacy
        totals = self.deliver([self.post(1, "TP2 hit")])
        self.assertEqual(totals, {"match_send": 1})
        self.assertEqual(len(self.client.sent), 1, "shadow never blocks or duplicates a send")

    def test_wrong_reply_target_is_reported_with_the_field(self):
        async def legacy(route, unit):
            await self.client.send_message(route["dest_chat"], unit[0].message, formatting_entities=unit[0].entities,
                                           reply_to=99999)
            return True
        self.behaviour = legacy
        with self.assertLogs("engine-shadow", "WARNING") as logs:
            totals = self.deliver([self.post(2, "TP3 hit")])
        self.assertEqual(totals, {"mismatch": 1})
        self.assertIn("detail=reply_to", logs.output[0])

    def test_both_skipping_a_link_post_is_a_match(self):
        async def legacy(route, unit):
            return True
        self.behaviour = legacy
        self.assertEqual(self.deliver([self.post(3, "join t.me/x")]), {"match_skip": 1})

    def test_legacy_failure_is_not_counted_as_a_logic_mismatch(self):
        async def legacy(route, unit):
            raise ConnectionError("reset")
        self.behaviour = legacy
        with self.assertRaises(ConnectionError):
            self.deliver([self.post(4, "TP1 hit")])
        self.assertEqual(self.shadow.totals[self.route["name"]], {"legacy_failed": 1})

    def test_sends_outside_a_tracked_delivery_are_ignored(self):
        asyncio.run(self.client.send_message("me", "health digest"))
        self.assertEqual(self.shadow.totals, {})

    def test_totals_survive_restart(self):
        async def legacy(route, unit):
            return True
        self.behaviour = legacy
        self.deliver([self.post(5, "join t.me/x")])
        self.shadow._save(force=True)
        reloaded = shadow_mod.Shadow(self.worker, self.tmp.name)
        self.assertEqual(reloaded.totals, {self.route["name"]: {"match_skip": 1}})
        self.assertEqual(json.loads((Path(self.tmp.name) / shadow_mod.STATE_FILE).read_text())
                         [self.route["name"]]["match_skip"], 1)


if __name__ == "__main__":
    unittest.main()

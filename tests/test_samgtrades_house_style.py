"""Exposed | VIP library style for the SamGtrades relay, and the one-time last-100 restyle."""

import asyncio
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import samgtrades_house_formatter as house
from telegram_worker import wholechat_relay_4281603170_to_3743381585 as relay
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
)

FALLBACK = {"Boom": "\U0001f4a5", "GreenTick": "✅", "RedCross": "❌", "Warning": "⚠️"}
SIGNAL = """🎯 SIGNAL ALERT
SELL LIMIT — XAUUSD

📊 Timeframe: 3

💰 Entry: 4183.955
🛑 SL: 4187.810 (38.6 pips)

✅ TP1: 4180.100 (38.6 pips, 1R)
✅ TP2: 4174.317 (96.4 pips, 2.5R)
✅ TP3: 4159.320 (246.4 pips, 6.39R)

Trade responsibly! 📈

🔑 Ref: 1790863020000S"""
TP1 = "✅ TP1 HIT — XAUUSD\n\nTake Profit reached at 4180.100 (+38.6 pips)\n\nConsider moving SL to break even 🔒"


class FakeRegistry:
    def get(self, alias):
        return {"type": "custom", "document_id": 1} if alias in FALLBACK else None

    def render(self, template):
        parts, entities = [], []
        for token in re.split(r"(\{\w+\})", template):
            alias = token[1:-1] if token.startswith("{") else None
            if alias in FALLBACK:
                offset = house._utf16_len("".join(parts))
                entities.append(MessageEntityCustomEmoji(offset, house._utf16_len(FALLBACK[alias]), 7))
                parts.append(FALLBACK[alias])
            else:
                parts.append(token)
        return "".join(parts), entities


def spans(text, entities, kind):
    raw = text.encode("utf-16-le")
    return [
        raw[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
        for e in entities
        if isinstance(e, kind)
    ]


class StyleTests(unittest.TestCase):
    registry = FakeRegistry()

    def test_signal_keeps_layout_and_adds_formatting(self):
        text, entities, kind = house.format_samgtrades_text(SIGNAL, None, self.registry)
        self.assertEqual(kind, "signal")
        self.assertEqual(
            text,
            "\U0001f4a5 XAUUSD SELL LIMIT\nPending · wait for price to rise to entry\n\n"
            "ENTRY = 4183.955\n\n"
            "TP1✅ = 4180.100 (+1.0 RR)\nTP2✅ = 4174.317 (+2.5 RR)\n"
            "TP3✅ = 4159.320 (+6.39 RR)\n\nSL❌ = 4187.810 (-1.0 RR)\n\n"
            "⚠️ NOT FINANCIAL ADVICE ⚠️",
        )
        self.assertEqual(
            spans(text, entities, MessageEntityBold),
            ["XAUUSD SELL LIMIT", "ENTRY", "TP1", "TP2", "TP3", "SL", "NOT FINANCIAL ADVICE"],
        )
        self.assertEqual(
            spans(text, entities, MessageEntityCode),
            ["4183.955", "4180.100", "4174.317", "4159.320", "4187.810"],
        )
        self.assertEqual(
            spans(text, entities, MessageEntityItalic), ["Pending · wait for price to rise to entry"]
        )
        self.assertEqual(len(spans(text, entities, MessageEntityCustomEmoji)), 7)

    def test_market_signal_has_no_pending_note(self):
        text, entities, _ = house.format_samgtrades_text(
            SIGNAL.replace("SELL LIMIT", "BUY NOW"), None, self.registry
        )
        self.assertTrue(text.startswith("\U0001f4a5 XAUUSD BUY NOW\n\nENTRY"))
        self.assertEqual(spans(text, entities, MessageEntityItalic), [])

    def test_every_update_matches_library(self):
        cases = {
            "{GreenTick} ZONE TRIGGERED/ACTIVATED": (
                "✅ ZONE TRIGGERED/ACTIVATED\nOrder filled · trade is live",
                [],
                [],
            ),
            "{GreenTick} TP1 HIT — +38.6 PIPS (+1.0 RR)": (
                "✅ TP1 HIT — +38.6 PIPS (+1.0 RR)\nSecure partials · consider SL to breakeven",
                ["+38.6 PIPS"],
                [],
            ),
            "{GreenTick} TP2 HIT — +96.4 PIPS (+2.5 RR)": (
                "✅ TP2 HIT — +96.4 PIPS (+2.5 RR)\nRunner continues toward TP3",
                ["+96.4 PIPS"],
                [],
            ),
            "{GreenTick} TP3 HIT — +246.4 PIPS (+6.39 RR)": (
                "✅ TP3 HIT — +246.4 PIPS (+6.39 RR)\nFinal target reached · trade closed",
                ["+246.4 PIPS"],
                [],
            ),
            "{GreenTick} TP3 HIT — (RR N/A)": (
                "✅ TP3 HIT (RR N/A)\nFinal target reached · trade closed",
                [],
                [],
            ),
            "{GreenTick} TP2 & TP3 HIT — +290 PIPS": (
                "✅ TP2 & TP3 HIT — +290 PIPS\nBoth targets reached",
                ["+290 PIPS"],
                [],
            ),
            "{GreenTick} +220 PIPS RUNNING": ("✅ +220 PIPS RUNNING\nTrade in profit · protect it", [], []),
            "{GreenTick} +220 PIPS": ("✅ +220 PIPS\nTrade in profit · protect it", [], []),
            "{GreenTick} MOVE SL TO BREAKEVEN": ("✅ MOVE SL TO BREAKEVEN\nTrade is now risk-free", [], []),
            "{GreenTick} SECURE PARTIAL PROFITS": (
                "✅ SECURE PARTIAL PROFITS\nClose part, let the rest run",
                [],
                [],
            ),
            "{GreenTick} SECURE PARTIAL PROFITS\n\nMOVE SL TO BREAKEVEN": (
                "✅ SECURE PARTIAL PROFITS\nMOVE SL TO BREAKEVEN\nClose part · trade is now risk-free",
                [],
                [],
            ),
            "{Warning} MOVE SL = 4335": ("⚠️ MOVE SL = 4335\nUpdate your stop loss now", [], ["4335"]),
            "{GreenTick} ENTRY STILL VALID": (
                "✅ ENTRY STILL VALID\nSetup unchanged · entry can still be taken",
                [],
                [],
            ),
            "{GreenTick} TRADE STILL ACTIVE": ("✅ TRADE STILL ACTIVE\nHold · targets still in play", [], []),
            "{Warning} LAYER ENTRY NOW": ("⚠️ LAYER ENTRY NOW\nAdd a second entry at current price", [], []),
            "{Warning} DELETE PENDING ORDER\n\nENTER MARKET NOW": (
                "⚠️ DELETE PENDING ORDER\nENTER MARKET NOW\nRemove the order and enter at market",
                [],
                [],
            ),
            "{Warning} TP1 MISSED BY 10 PIPS": (
                "⚠️ TP1 MISSED BY 10 PIPS\nPrice turned before the target",
                [],
                [],
            ),
            "{Warning} CLOSE TRADE NOW": ("⚠️ CLOSE TRADE NOW\nExit at market price", [], []),
            "{Warning} CLOSE AT ENTRY NOW": ("⚠️ CLOSE AT ENTRY NOW\nExit at entry · no loss taken", [], []),
            "{RedCross} SL HIT — -38.6 PIPS (-1.0 RR)": (
                "❌ SL HIT — -38.6 PIPS (-1.0 RR)\nTrade closed · on to the next",
                ["-38.6 PIPS"],
                [],
            ),
            "{RedCross} SL HIT — -1.0 RR": ("❌ SL HIT (-1.0 RR)\nTrade closed · on to the next", [], []),
            "{RedCross} TRADE CANCELLED": ("❌ TRADE CANCELLED\nSetup no longer valid", [], []),
            "{RedCross} PENDING ORDER CANCELLED": (
                "❌ PENDING ORDER CANCELLED\nRemove the pending order",
                [],
                [],
            ),
        }
        for template, (expected, pips, prices) in cases.items():
            text, entities = house.style_update(self.registry, template)
            self.assertEqual(text, expected, template)
            self.assertEqual(spans(text, entities, MessageEntityCode), pips + prices, template)
            self.assertEqual(len(spans(text, entities, MessageEntityItalic)), 1, template)
            self.assertTrue(spans(text, entities, MessageEntityBold), template)

    def test_real_tp_update_uses_parent_rr(self):
        text, _, kind = house.format_samgtrades_text(TP1, None, self.registry, parent_text=SIGNAL)
        self.assertEqual((kind, text.splitlines()[0]), ("update", "✅ TP1 HIT — +38.6 PIPS (+1.0 RR)"))

    def test_already_formatted_text_is_not_reformatted(self):
        text, _, _ = house.format_samgtrades_text(SIGNAL, None, self.registry)
        self.assertIsNone(house.format_samgtrades_text(text, None, self.registry)[2])

    def test_startup_self_test_still_passes(self):
        self.assertEqual(house.run_self_test(), (1, 6))


def msg(mid, text, reply_to=None, entities=None):
    reply = SimpleNamespace(reply_to_msg_id=reply_to) if reply_to else None
    return SimpleNamespace(id=mid, message=text, entities=entities, reply_to=reply, media=None)


class FakeClient:
    def __init__(self, dest, source):
        self.dest, self.source, self.edits = dest, source, []
        self.fail_ids = set()

    async def get_messages(self, chat, ids=None, limit=None):
        if chat == relay.DEST_CHAT:
            return sorted(self.dest.values(), key=lambda m: -m.id)[:limit]
        if isinstance(ids, list):
            return [self.source.get(i) for i in ids]
        return self.source.get(ids)

    async def edit_message(self, chat, mid, text, formatting_entities=None):
        if mid in self.fail_ids:
            raise RuntimeError("MESSAGE_EDIT_TIME_EXPIRED")
        self.edits.append(mid)
        self.dest[mid] = msg(mid, text, entities=formatting_entities)


class RestyleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        relay._SIGNAL_TEXTS.clear()
        source = {
            10: msg(10, SIGNAL),
            11: msg(11, TP1, reply_to=10),
            12: msg(12, "Good morning team 🔥"),
            13: msg(13, "✅ ZONE ACTIVE — XAUUSD SELL\n\nFilled at 4183.955.", reply_to=10),
        }
        old_signal = "\U0001f4a5 XAUUSD SELL LIMIT\n\nENTRY = 4183.955"
        dest = {
            500: msg(500, "Admin note, not relayed"),
            501: msg(501, old_signal),
            502: msg(502, "✅ TP1 HIT"),
            503: msg(503, "Good morning team 🔥"),
            504: msg(504, "✅ ZONE TRIGGERED/ACTIVATED"),
        }
        self.client = FakeClient(dest, source)
        self.map = {"10": 501, "11": 502, "12": 503, "13": 504}

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self):
        return asyncio.run(
            relay._restyle_recent_once(self.client, self.map, Path(self.tmp.name), FakeRegistry(), relay.log)
        )

    def test_edits_only_relayed_formatted_posts_and_never_twice(self):
        self.client.fail_ids = {504}
        state = self.run_once()
        self.assertEqual(self.client.edits, [501, 502])
        self.assertEqual(state["counts"], {"edited": 2, "passthrough": 1, "not_editable": 1})
        self.assertIn("Secure partials", self.client.dest[502].message)
        self.assertTrue(self.client.dest[501].message.startswith("\U0001f4a5 XAUUSD SELL LIMIT\nPending"))
        self.assertEqual(self.client.dest[500].message, "Admin note, not relayed")
        self.run_once()
        self.assertEqual(self.client.edits, [501, 502])

    def test_already_restyled_posts_are_left_alone(self):
        self.run_once()
        Path(self.tmp.name, relay.RESTYLE_FILENAME).unlink()
        state = self.run_once()
        self.assertEqual(state["counts"].get("edited", 0), 0)
        self.assertEqual(state["counts"]["unchanged"], 3)


if __name__ == "__main__":
    unittest.main()

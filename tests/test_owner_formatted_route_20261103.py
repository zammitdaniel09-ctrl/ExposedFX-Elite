"""Library-format route 8323 -> VIP12289: parsing, rendering and replay of real provider posts."""
import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import library_signal_format as fmt
from telegram_worker import owner_formatted_route_20261103 as route_module
from telegram_worker import owner_forwarding_policy as policy
from telethon.tl.types import MessageEntityCustomEmoji

SOURCE_CHAT, SOURCE_TOPIC = -1004367822325, 8323
DEST_CHAT, DEST_TOPIC = -1003726286301, 12289

BUY_3895 = "🔥 GOLD TRADE IDEA\n\n🟢 BUY GOLD NOW @ 3895\n\n❌ SL: 3885\n✅ TP1: 3899\n✅ TP2: 3909\n✅ TP3: 3913\n✅ TP4: 3917\n✅ TP5: 3928"
BUY_3892 = "🔥 GOLD TRADE IDEA\n\n🟢 BUY GOLD NOW @ 3892\n\n❌ SL: 3889\n✅ TP1: 3907\n✅ TP2: 3915\n✅ TP3: 3923\n✅ TP4: 3929\n✅ TP5: 3936"
BUY_3904 = "🔥 GOLD TRADE IDEA\n\n🟢 BUY XAUUSD NOW @ 3904\n\n❌ SL: 3889\n✅ TP1: 3911\n✅ TP2: 3917\n✅ TP3: 3923\n✅ TP4: 3928\n✅ TP5: 3937"
BUY_3942 = "💰 Trade idea – GOLD (XAUUSD)\n\n🟢 BUY GOLD @ 3942\n\n❌ SL: 3924\n✅ TP1: 3949\n✅ TP2: 3958\n✅ TP3: 3965\n✅ TP4: 3976\n✅ TP5: 3985"
SELL_3928 = "💰 Trade idea – GOLD (XAUUSD)\n\n🔴 SELL XAUUSD NOW @ 3928\n\n❌ SL: 3945\n✅ TP1: 3920\n✅ TP2: 3914\n✅ TP3: 3910\n✅ TP4: 3902\n✅ TP5: 3897"
RECAP = ("📊 Today’s Recap – Mon 2 Nov\n\n✅ Wins: 8 ❌ Losses: 0\n🎯 2 trade hit all targets\n💰 Net: + 810 pips\n\n"
         "A positive day – rest up, we go again tomorrow 🙌\nResults use the posted levels, before spreads and fees.")
CHATTER = [
    RECAP,
    "Hey traders, good morning! How's everyone feeling about today? 💪",
    "Who got in this trade select ?",
    "Sorry when I type then so got the entry price wrong. \n\nWhen my buy signal comes in just buy straight away",
    "I am doing alot bigger stop losses due to market conditions 📈\n\nTry get in at a better entry if you can 🔥",
    "📸 PROFIT SHOTS\n\nIf you’ve made any profits from today’s trades, send your screenshots over to @SelectAssistance 📈",
    "https://us06web.zoom.us/j/86014625622",
    "100 PIPS 🥇",
    "TP1 + 150 PIPS 🔥",
    "Wow trade ideas what a day 🫣",
]


def message(mid, text, parent=None, minutes=0):
    reply = (SimpleNamespace(reply_to_top_id=SOURCE_TOPIC, reply_to_msg_id=parent) if parent
             else SimpleNamespace(reply_to_top_id=None, reply_to_msg_id=SOURCE_TOPIC))
    when = datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(minutes=minutes)
    return SimpleNamespace(id=mid, message=text, media=None, date=when, reply_to=reply)


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.sent = []

    async def get_messages(self, chat, ids=None, reply_to=None, limit=None, **kwargs):
        if ids is not None:
            return SimpleNamespace(id=ids, action=type("MessageActionTopicCreate", (), {})())
        return sorted(self.rows, key=lambda m: -m.id)[:limit]

    async def iter_messages(self, chat, reply_to=None, min_id=0, reverse=False, limit=50):
        for row in sorted(self.rows, key=lambda m: m.id):
            if row.id > min_id:
                yield row

    async def send_message(self, chat, text, formatting_entities=None, reply_to=None, link_preview=True):
        self.sent.append(SimpleNamespace(chat=chat, text=text, entities=formatting_entities, reply_to=reply_to,
                                         id=50000 + len(self.sent)))
        return self.sent[-1]


def replay_rows():
    """Provider's 2-3 Nov thread shape (the newest provider history the owner pasted)."""
    rows = []

    def add(text, parent=None):
        rows.append(message(9000 + len(rows), text, parent, minutes=len(rows)))
        return rows[-1].id

    a = add(BUY_3895)
    add("✅ TP1 HIT 🔥", a)
    add("🔒 SL to breakeven (3895) – let the rest run", a)
    b = add(BUY_3892)
    add("100 PIPS 🥇", b)
    add("✅ TP1 HIT! 💰", b)
    add("🔒 Move SL to entry (3982) 🙌", b)
    add("TP1 + 150 PIPS 🔥", b)
    c = add(BUY_3904)
    add("✅ TP1 HIT! 💰", c)
    add("✅ TP2 HIT! 💰", c)
    add("✅ TP3 HIT! 💰", c)
    add("🔒 Move stop loss to TP1 (3911) – profit locked in", c)
    add("✅ TP4 smashed! 🚀", c)
    add("🎯 TP5 HIT – all targets smashed! 💰", c)
    d = add(BUY_3942)
    add("✅ TP1 HIT 🔥", d)
    add(RECAP)
    add("✅ TP1 HIT! 💰", d)  # provider repeated TP1 hours later
    add("🔒 Move SL to entry (3942) 🙌", d)
    add("✅ TP2 smashed! 🚀", d)
    e = add(SELL_3928)
    add("✅ TP1 HIT 🔥", e)
    add("🔒 Set stop loss to entry (3928) – protecting the trade", e)
    add("✅ TP2 smashed! 🚀", e)
    add("Good morning traders, who's set for the session? ☀️")
    add("✅ TP3 smashed! 🚀", e)
    add("🔒 Move stop loss to TP1 (3920) – profit locked in", e)
    return rows


class ParsingTests(unittest.TestCase):
    def test_signals_parse(self):
        self.assertEqual(fmt.parse_signal(BUY_3895), {
            "symbol": "XAUUSD", "direction": "BUY", "order_type": "MARKET", "entry": ["3895"],
            "sl": "3885", "tps": ["3899", "3909", "3913", "3917", "3928"]})
        sell = fmt.parse_signal(SELL_3928)
        self.assertEqual((sell["direction"], sell["entry"], sell["sl"], sell["tps"][-1]),
                         ("SELL", ["3928"], "3945", "3897"))
        self.assertEqual(fmt.parse_signal(BUY_3942)["entry"], ["3942"])

    def test_pending_order_types(self):
        limit = fmt.parse_signal("BUY LIMIT GOLD @ 3935\nSL: 3927\nTP1: 3949\nTP2: 3964")
        stop = fmt.parse_signal("SELL STOP XAUUSD @ 3940\nSTOP LOSS: 3948\nTP1: 3928")
        self.assertEqual((limit["order_type"], stop["order_type"]), ("LIMIT", "STOP"))
        self.assertEqual(fmt.parse_signal(BUY_3895)["order_type"], "MARKET")

    def test_updates_classify(self):
        cases = {
            "✅ TP1 HIT 🔥": {"kind": "tp", "tp": 1, "all": False},
            "✅ TP2 smashed! 🚀": {"kind": "tp", "tp": 2, "all": False},
            "🎯 TP5 HIT – all targets smashed! 💰": {"kind": "tp", "tp": 5, "all": True},
            "🔒 SL to breakeven (3895) – let the rest run": {"kind": "set_be"},
            "🔒 Move SL to entry (3982) 🙌": {"kind": "set_be"},
            "🔒 Set stop loss to entry (3928) – protecting the trade": {"kind": "set_be"},
            "🔒 Move stop loss to TP1 (3911) – profit locked in": {"kind": "move_sl", "tp": 1},
            "🔒 SL to TP1 (3950). Profit secured, let the rest run 🙌": {"kind": "move_sl", "tp": 1},
            "SL hit ❌": {"kind": "sl_hit"},
        }
        for text, expected in cases.items():
            self.assertEqual(fmt.classify_update(text), expected, text)

    def test_chatter_is_never_a_signal_or_update(self):
        for text in CHATTER:
            self.assertIsNone(fmt.parse_signal(text), text)
            self.assertIsNone(fmt.classify_update(text), text)
            if text is not RECAP:
                self.assertIsNone(fmt.parse_recap(text), text)

    def test_recap_parses(self):
        self.assertEqual(fmt.parse_recap(RECAP), {
            "period": "DAILY", "label": "MON 2 NOV", "wins": 8, "losses": 0,
            "all_targets": 2, "breakevens": None, "net_pips": 810.0})
        week = fmt.parse_recap("📊 Weekly Recap\nWins: 20 Losses: 5\nBreakevens: 3\nNet: −1,240 pips")
        self.assertEqual((week["period"], week["breakevens"], week["net_pips"]), ("WEEKLY", 3, -1240.0))


class RenderTests(unittest.TestCase):
    def assert_entities_valid(self, text, entities):
        size = fmt.utf16_len(text)
        for entity in entities:
            self.assertGreater(entity.length, 0)
            self.assertLessEqual(entity.offset + entity.length, size)
            if isinstance(entity, MessageEntityCustomEmoji):
                units = text.encode("utf-16-le")[entity.offset * 2:(entity.offset + entity.length) * 2]
                self.assertIn(units.decode("utf-16-le"), {char for char, _ in fmt.EMOJI.values()})

    def test_signal_matches_library_layout(self):
        text, entities = fmt.render_signal(fmt.parse_signal(BUY_3895))
        self.assertEqual(text, "☄️ XAUUSD — BUY NOW ☄️\n\nEntry: 3895\nStop Loss: 3885\n\n"
                               "⭐️ TP1: 3899\n⭐️ TP2: 3909\n⭐️ TP3: 3913\n"
                               "⭐️ TP4: 3917\n⭐️ TP5: 3928\n\n"
                               "☠️ Breakeven: SL to entry at TP1\n"
                               "⚠️ Risk max 1% · Not financial advice")
        self.assert_entities_valid(text, entities)
        self.assertEqual(sum(isinstance(e, MessageEntityCustomEmoji) for e in entities), 9)

    def test_pending_signal_notes(self):
        text, entities = fmt.render_signal(fmt.parse_signal("SELL LIMIT GOLD @ 3970\nSL: 3978\nTP1: 3958"))
        self.assertIn("XAUUSD — SELL LIMIT", text)
        self.assertIn("Pending · wait for price to rise to entry", text)
        self.assert_entities_valid(text, entities)

    def test_update_pips_and_breakeven_uses_signal_entry(self):
        signal = fmt.parse_signal(BUY_3892)
        progress = {}
        tp1 = fmt.resolve_update({"kind": "tp", "tp": 1, "all": False}, signal, progress)
        self.assertEqual(fmt.render_update(tp1, signal)[0].splitlines(),
                         ["✅ TP1 HIT · 3907", "\U0001f381 +150 pips secured"])
        be = fmt.resolve_update(fmt.classify_update("🔒 Move SL to entry (3982) 🙌"), signal, progress)
        self.assertEqual(be["price"], "3892")  # provider typo 3982 ignored
        sell = fmt.parse_signal(SELL_3928)
        hit = fmt.resolve_update({"kind": "tp", "tp": 2, "all": False}, sell, {})
        self.assertIn("+140 pips secured", fmt.render_update(hit, sell)[0])
        stopped = fmt.resolve_update({"kind": "sl_hit"}, sell, {})
        self.assertIn("SL HIT · 3945", fmt.render_update(stopped, sell)[0])
        self.assertIn("-170 pips", fmt.render_update(stopped, sell)[0])

    def test_recap_matches_library_style(self):
        text, entities = fmt.render_recap(fmt.parse_recap(RECAP))
        self.assertEqual(text, "☄️ DAILY RECAP · MON 2 NOV ☄️\n\n"
                               "✅ Wins: 8\n❌ Losses: 0\n\U0001f4a5 All targets hit: 2\n"
                               "⭐️ Win rate: 100%\n\U0001f381 Net: +810 pips\n\n"
                               "A green day · rest up, we go again\n"
                               "⚠️ Posted levels, before spreads and fees · Not financial advice")
        self.assert_entities_valid(text, entities)
        red, _ = fmt.render_recap({"period": "WEEKLY", "label": "", "wins": 1, "losses": 3,
                                   "all_targets": None, "breakevens": 2, "net_pips": -120.0})
        self.assertIn("WEEKLY RECAP ☄", red)
        self.assertIn("Net: -120 pips", red)
        self.assertIn("A red week", red)

    def test_every_update_kind_renders(self):
        signal = fmt.parse_signal(BUY_3904)
        for update in ({"kind": "tp", "tp": 2, "price": "3917"}, {"kind": "all", "tp": 5, "price": "3937"},
                       {"kind": "set_be", "price": "3904"}, {"kind": "move_sl", "tp": 1, "price": "3911"},
                       {"kind": "move_sl", "tp": None, "price": "3900"}, {"kind": "be_hit", "price": "3904"},
                       {"kind": "sl_hit", "price": "3889"}, {"kind": "entry_triggered", "price": "3904"},
                       {"kind": "close", "price": "3920"}, {"kind": "close", "price": None},
                       {"kind": "cancel", "price": "3889"}, {"kind": "entry_updated", "price": "3900"}):
            text, entities = fmt.render_update(update, signal)
            self.assert_entities_valid(text, entities)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        policy._RUNTIME, policy._PATH = None, None
        policy.configure(self.tmp.name)
        self.rows = replay_rows()
        self.worker = SimpleNamespace(client=FakeClient(self.rows), DATA_DIR=self.tmp.name)
        self.path = Path(self.tmp.name) / route_module.STATE_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def run_route(self, state):
        async def go():
            if not state.get("initialized"):
                await route_module.initialize(self.worker, state, self.path)
            await route_module.cycle(self.worker, state, self.path)
        asyncio.run(go())

    def new_state(self):
        return {"signals": {}, "thread": {}, "posted": {}, "counts": {}}

    def test_last30_backfill_formats_threads_and_drops_noise(self):
        state = self.new_state()
        self.run_route(state)
        sent = self.worker.client.sent
        self.assertEqual(state["checkpoint"], self.rows[-1].id)
        self.assertEqual(state["backfill_selected"], 28)  # fewer than 30 posts exist
        headers = [s.text.splitlines()[0] for s in sent]
        signals = [s for s in sent if "— " in s.text.splitlines()[0]]
        self.assertEqual(len(signals), 5)
        self.assertTrue(all(s.reply_to == DEST_TOPIC and s.chat == DEST_CHAT for s in signals))
        updates = [s for s in sent if s.reply_to != DEST_TOPIC]
        self.assertEqual(len(updates), 18)
        signal_ids = {s.id for s in signals}
        self.assertTrue(all(u.reply_to in signal_ids for u in updates))
        # Repeated TP1, pip brags and greetings are not posted; the recap is reformatted.
        self.assertEqual(state["counts"], {"signal": 5, "update": 18, "skipped": 3, "repeat": 1, "recap": 1})
        self.assertIn("☄️ DAILY RECAP · MON 2 NOV ☄️", headers)
        self.assertIn("✅ TP1 HIT · 3907", headers)
        self.assertIn("☠️ SET BREAKEVEN · SL to 3892", headers)
        self.assertIn("\U0001f4a5 ALL TARGETS HIT · 3937 \U0001f4a5", headers)
        self.assertIn("☠️ MOVE SL · 3920", headers)

    def test_backfill_takes_only_latest_30(self):
        for extra in range(10):
            self.rows.insert(0, message(8000 + extra, "Old chatter %s" % extra))
        state = self.new_state()
        self.run_route(state)
        self.assertEqual(state["backfill_selected"], 30)
        self.assertEqual(state["checkpoint"], self.rows[-1].id)
        self.assertTrue(all(int(k) >= 9000 for k in state["posted"]))

    def test_restart_resumes_without_duplicates(self):
        state = self.new_state()
        self.run_route(state)
        count = len(self.worker.client.sent)
        resumed = route_module.load_state(self.path)
        self.run_route(resumed)
        self.assertEqual(len(self.worker.client.sent), count)
        self.rows.append(message(9999, "✅ TP4 smashed! 🚀", parent=self.rows[21].id, minutes=40))
        self.run_route(resumed)
        self.assertEqual(self.worker.client.sent[-1].text.splitlines()[0], "✅ TP4 HIT · 3902")
        self.assertEqual(len(self.worker.client.sent), count + 1)

    def test_orphan_update_attaches_to_recent_latest_signal_only(self):
        state = self.new_state()
        self.rows[:] = [message(9000, BUY_3904), message(9001, "✅ TP3 HIT 🥇", minutes=50)]
        self.run_route(state)
        self.assertEqual(self.worker.client.sent[-1].reply_to, self.worker.client.sent[0].id)
        self.rows.append(message(9002, "✅ TP4 HIT 🥇", minutes=60 * 30))
        self.run_route(state)
        self.assertEqual(state["counts"]["orphan"], 1)


if __name__ == "__main__":
    unittest.main()

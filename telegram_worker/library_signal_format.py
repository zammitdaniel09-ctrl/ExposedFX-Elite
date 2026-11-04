"""Owner's library format (Saved Messages, 3 Nov 2026) for signals, trade updates and recaps.

Pure parsing/rendering: no Telegram client, no I/O. Rendered text is returned
with Telethon entities (bold, italic, monospace prices, the owner's custom
emoji) so it can be sent with formatting_entities and parse_mode=None.
"""

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityCode,
    MessageEntityCustomEmoji,
    MessageEntityItalic,
)

# Owner's custom emoji: fallback character + document id.
EMOJI = {
    "lightning": ("☄️", 5224607267797606837),
    "star": ("⭐️", 5438496463044752972),
    "scythe": ("☠️", 6118209143972040877),
    "present": ("\U0001f381", 6283073379184415506),
    "tick": ("✅", 5398001711786762757),
    "boom": ("\U0001f4a5", 5276032951342088188),
    "cross": ("❌", 5210952531676504517),
    "warning": ("⚠️", 5447644880824181073),
}

PRICE = r"\d{1,7}(?:\.\d+)?"
DIRECTION_RE = re.compile(r"\b(BUY|SELL|LONG|SHORT)\b")
ORDER_TYPE_RE = re.compile(r"\b(BUY|SELL)[ \t]+(?:\w+[ \t]+){0,2}?(LIMIT|STOP)\b(?![ \t]*LOSS)")
ENTRY_AT_RE = re.compile(rf"@\s*({PRICE})(?:\s*(?:-|/|TO)\s*({PRICE}))?")
ENTRY_WORD_RE = re.compile(
    rf"\bENTRY(?:\s*(?:ZONE|PRICE|POINT))?\s*[:@=\-]?\s*({PRICE})(?:\s*(?:-|/|TO)\s*({PRICE}))?"
)
SL_RE = re.compile(rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b\s*[:@=\-]?\s*({PRICE})")
TP_RE = re.compile(rf"\bTP\s*([1-9])\s*[:@=\-]?\s*({PRICE}|OPEN)\b")
PAIR_RE = re.compile(r"\b([A-Z]{3}(?:USD|JPY|GBP|EUR|CHF|CAD|AUD|NZD))\b")
INDEX_RE = re.compile(r"\b(US30|US100|NAS100|NAS|SPX500|US500|GER40|DE40|UK100|BTCUSD|ETHUSD)\b")

SL_WORD = r"(?:SL|S/L|STOP\s*LOSS|STOPLOSS)"
SL_MOVE_RE = re.compile(
    rf"\b{SL_WORD}\s+(?:TO|AT|ON)\s+(BREAK\s*EVEN|BREAKEVEN|BE|ENTRY|TP\s*[1-9]|{PRICE})\b"
)
SET_BE_RE = re.compile(r"\b(?:SET|MOVE|PUT)\s+(?:TO\s+)?(?:BE|BREAK\s*EVEN|BREAKEVEN)\b")
SL_HIT_RE = re.compile(rf"\b{SL_WORD}\s*(?:WAS\s+|IS\s+|GOT\s+)?(?:HIT|TRIGGERED|TAKEN)\b|\bSTOPPED\s+OUT\b")
BE_HIT_RE = re.compile(
    r"\b(?:BE|BREAK\s*EVEN|BREAKEVEN)\s+(?:HIT|TRIGGERED|TAKEN)\b|\bCLOSED\s+(?:AT\s+)?(?:BE|BREAK\s*EVEN|BREAKEVEN)\b"
)
ALL_TP_RE = re.compile(r"\bALL\s+(?:TARGETS?|TPS?)\b")
TP_HIT_RE = re.compile(
    r"\b(?:TP|TARGET)\s*([1-9])\b\s*(?:[:\-]\s*)?(?:IS\s+|WAS\s+|GOT\s+)?(HIT|SMASHED|REACHED|DONE|SECURED|BOOKED|ACHIEVED)\b"
)
ENTRY_TRIGGERED_RE = re.compile(
    r"\b(?:ENTRY|ORDER|LIMIT|POSITION)\s+(?:IS\s+|GOT\s+|HAS\s+BEEN\s+)?(?:TRIGGERED|ACTIVATED|FILLED|ACTIVE)\b"
)
CLOSE_RE = re.compile(r"\bCLOSE\s+(?:NOW|ALL|EARLY|FULL|IT|THE\s+TRADE|TRADE|POSITION)\b")
CANCEL_RE = re.compile(r"\b(?:CANCEL|CANCELLED|CANCELED|DELETE|DELETED|REMOVE|REMOVED)\b")
CANCEL_OBJECT_RE = re.compile(r"\b(?:ORDER|LIMIT|SETUP|SIGNAL|TRADE|PENDING)\b")
ENTRY_UPDATED_RE = re.compile(rf"\b(?:NEW\s+ENTRY|ENTRY\s+(?:UPDATED|CHANGED|MOVED))\b\D{{0,12}}({PRICE})")
RECAP_TITLE_RE = re.compile(r"^.*\b(?:RECAP|RESULTS|SUMMARY)\b[ \t]*[-:|]?[ \t]*(.*)$", re.MULTILINE)
WINS_RE = re.compile(r"\bWINS?\b\s*[:=\-]?\s*(\d+)")
LOSSES_RE = re.compile(r"\bLOSS(?:ES)?\b\s*[:=\-]?\s*(\d+)")
ALL_TARGETS_COUNT_RE = re.compile(r"(\d+)\s+TRADES?\s+(?:HIT\s+|SMASHED\s+)?ALL\s+TARGETS")
BREAKEVEN_COUNT_RE = re.compile(r"\bBREAK\s*EVENS?\b\s*[:=\-]?\s*(\d+)")
NET_PIPS_RE = re.compile(r"\bNET\b\s*[:=\-]?\s*([+\-]?)\s*(\d[\d,]*(?:\.\d+)?)\s*PIPS")

# Recaps, promos and links mention "all targets"/"wins" but are not trade updates.
NOISE_RE = re.compile(
    r"\b(?:RECAP|WINS?|LOSSES|WIN\s+RATE|RESULTS|SUMMARY|SCREENSHOTS?|ZOOM)\b|HTTPS?://|T\.ME/|@\w"
)
UPDATE_MAX_CHARS = 160


def utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def _upper(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "")
    value = value.replace("–", "-").replace("—", "-").replace("\xa0", " ")
    return value.upper()


def _num(raw: str) -> float:
    return float(raw)


def symbol_of(up: str, entry: float) -> Optional[str]:
    if "GOLD" in up or "XAU" in up:
        return "XAUUSD"
    if "SILVER" in up or "XAG" in up:
        return "XAGUSD"
    match = PAIR_RE.search(up) or INDEX_RE.search(up)
    if match:
        return match.group(1)
    # Provider posts gold only; a bare 4-digit gold price keeps its symbol.
    return "XAUUSD" if 1000 <= entry <= 10000 else None


def pip_size(symbol: str, price: float) -> float:
    if symbol.startswith("XAU"):
        return 0.1
    if symbol.startswith("XAG") or symbol.endswith("JPY"):
        return 0.01
    return 0.0001 if price < 20 else 1.0


def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    up = _upper(text)
    trade_line = next(
        (line for line in up.splitlines() if DIRECTION_RE.search(line) and re.search(PRICE, line)), None
    )
    if not trade_line:
        return None
    direction = DIRECTION_RE.search(trade_line).group(1)
    direction = {"LONG": "BUY", "SHORT": "SELL"}.get(direction, direction)
    entry = ENTRY_AT_RE.search(trade_line) or ENTRY_WORD_RE.search(up)
    sl = SL_RE.search(up)
    tps = {}
    for number, value in TP_RE.findall(up):
        tps.setdefault(int(number), value)
    if not entry or not sl or not tps:
        return None
    entry_raw = [entry.group(1)] + ([entry.group(2)] if entry.group(2) else [])
    symbol = symbol_of(up, _num(entry_raw[0]))
    if not symbol:
        return None
    order = ORDER_TYPE_RE.search(up)
    return {
        "symbol": symbol,
        "direction": direction,
        "order_type": order.group(2) if order else "MARKET",
        "entry": entry_raw,
        "sl": sl.group(1),
        "tps": [tps[n] for n in sorted(tps)],
    }


def parse_recap(text: str) -> Optional[Dict[str, Any]]:
    """Provider's end-of-day/week results post; their own counts and pips."""
    up = _upper(text).replace("−", "-")
    title = RECAP_TITLE_RE.search(up)
    wins, losses = WINS_RE.search(up), LOSSES_RE.search(up)
    if not title or not wins or not losses:
        return None
    label = re.sub(r"[^A-Z0-9 /.]", "", title.group(1)).strip(" .")[:24]
    all_targets = ALL_TARGETS_COUNT_RE.search(up)
    breakevens = BREAKEVEN_COUNT_RE.search(up)
    net = NET_PIPS_RE.search(up)
    return {
        "period": "WEEKLY" if "WEEK" in title.group(0) else "DAILY",
        "label": label,
        "wins": int(wins.group(1)),
        "losses": int(losses.group(1)),
        "all_targets": int(all_targets.group(1)) if all_targets else None,
        "breakevens": int(breakevens.group(1)) if breakevens else None,
        "net_pips": (
            float(net.group(2).replace(",", "")) * (-1 if net.group(1) == "-" else 1) if net else None
        ),
    }


def classify_update(text: str) -> Optional[Dict[str, Any]]:
    up = _upper(text)
    if not up.strip() or len(up) > UPDATE_MAX_CHARS or NOISE_RE.search(up):
        return None
    if CANCEL_RE.search(up) and CANCEL_OBJECT_RE.search(up):
        return {"kind": "cancel"}
    move = SL_MOVE_RE.search(up)
    if move:
        target = re.sub(r"\s+", "", move.group(1))
        if target in ("BREAKEVEN", "BE", "ENTRY"):
            return {"kind": "set_be"}
        if target.startswith("TP"):
            return {"kind": "move_sl", "tp": int(target[2:])}
        return {"kind": "move_sl", "price": target}
    if SET_BE_RE.search(up):
        return {"kind": "set_be"}
    if BE_HIT_RE.search(up):
        return {"kind": "be_hit"}
    if SL_HIT_RE.search(up):
        return {"kind": "sl_hit"}
    tp_hit = TP_HIT_RE.search(up)
    if ALL_TP_RE.search(up) and re.search(r"\b(HIT|SMASHED|REACHED|DONE|COMPLETED?)\b", up):
        return {"kind": "tp", "tp": int(tp_hit.group(1)) if tp_hit else None, "all": True}
    if tp_hit:
        return {"kind": "tp", "tp": int(tp_hit.group(1)), "all": False}
    if ENTRY_TRIGGERED_RE.search(up):
        return {"kind": "entry_triggered"}
    if CLOSE_RE.search(up):
        price = re.search(PRICE, up)
        return {"kind": "close", "price": price.group(0) if price else None}
    updated = ENTRY_UPDATED_RE.search(up)
    if updated:
        return {"kind": "entry_updated", "price": updated.group(1)}
    return None


class _Builder:
    def __init__(self):
        self.parts: List[str] = []
        self.entities: list = []
        self.offset = 0

    def add(self, text: str, entity=None, **kwargs):
        length = utf16_len(text)
        if entity is not None and length:
            self.entities.append(entity(self.offset, length, **kwargs))
        self.parts.append(text)
        self.offset += length
        return self

    def bold(self, text):
        return self.add(text, MessageEntityBold)

    def italic(self, text):
        return self.add(text, MessageEntityItalic)

    def mono(self, text):
        return self.add(text, MessageEntityCode)

    def emoji(self, key):
        char, document_id = EMOJI[key]
        return self.add(char, MessageEntityCustomEmoji, document_id=document_id)

    def result(self) -> Tuple[str, list]:
        return "".join(self.parts), self.entities


def _entry_text(signal) -> str:
    return " – ".join(signal["entry"])


def entry_price(signal) -> float:
    return _num(signal["entry"][0])


def pips_between(signal, start: float, end: float) -> float:
    sign = 1 if signal["direction"] == "BUY" else -1
    return (end - start) * sign / pip_size(signal["symbol"], start)


def format_pips(value: float) -> str:
    rounded = round(value, 1)
    text = f"{abs(rounded):,.0f}" if rounded == int(rounded) else f"{abs(rounded):,.1f}"
    return ("+" if rounded > 0 else "-" if rounded < 0 else "") + text


def render_signal(signal) -> Tuple[str, list]:
    b = _Builder()
    action = signal["direction"] + (
        " NOW" if signal["order_type"] == "MARKET" else " " + signal["order_type"]
    )
    b.emoji("lightning").add(" ").bold(f"{signal['symbol']} — {action}").add(" ").emoji("lightning").add("\n")
    if signal["order_type"] == "LIMIT":
        way = "drop" if signal["direction"] == "BUY" else "rise"
        b.italic(f"Pending · wait for price to {way} to entry").add("\n")
    elif signal["order_type"] == "STOP":
        way = "above" if signal["direction"] == "BUY" else "below"
        b.italic(f"Only enter on a break {way} entry").add("\n")
    b.add("\n").bold("Entry:").add(" ").mono(_entry_text(signal)).add("\n")
    b.bold("Stop Loss:").add(" ").mono(signal["sl"]).add("\n\n")
    for number, tp in enumerate(signal["tps"], 1):
        b.emoji("star").add(f" TP{number}: ")
        if tp == "OPEN":
            b.add("Open")
        else:
            b.mono(tp)
        b.add("\n")
    b.add("\n").emoji("scythe").add(" ").italic("Breakeven: SL to entry at TP1").add("\n")
    b.emoji("warning").add(" ").italic("Risk max 1% · Not financial advice")
    return b.result()


def render_recap(recap) -> Tuple[str, list]:
    b = _Builder()
    title = recap["period"] + " RECAP" + (" · " + recap["label"] if recap["label"] else "")
    b.emoji("lightning").add(" ").bold(title).add(" ").emoji("lightning").add("\n\n")
    rows = [
        ("tick", "Wins:", recap["wins"]),
        ("cross", "Losses:", recap["losses"]),
        ("scythe", "Breakeven:", recap["breakevens"]),
        ("boom", "All targets hit:", recap["all_targets"]),
    ]
    for emoji, label, value in rows:
        if value is not None:
            b.emoji(emoji).add(" ").bold(label).add(" ").mono(str(value)).add("\n")
    closed = recap["wins"] + recap["losses"]
    if closed:
        b.emoji("star").add(" ").bold("Win rate:").add(" ").mono(
            f"{round(100 * recap['wins'] / closed)}%"
        ).add("\n")
    net = recap["net_pips"]
    if net is not None:
        b.emoji("present").add(" ").bold("Net:").add(" ").mono(f"{format_pips(net)} pips").add("\n")
    score = net if net is not None else recap["wins"] - recap["losses"]
    span = "week" if recap["period"] == "WEEKLY" else "day"
    mood = (
        f"A green {span} · rest up, we go again"
        if score > 0
        else f"A red {span} · reset, we go again" if score < 0 else f"A flat {span} · capital protected"
    )
    b.add("\n").italic(mood).add("\n")
    b.emoji("warning").add(" ").italic("Posted levels, before spreads and fees · Not financial advice")
    return b.result()


def resolve_update(update, signal, progress) -> Optional[Dict[str, Any]]:
    """Attach signal prices to an update and drop repeats. progress is mutated."""
    kind = update["kind"]
    priced = [tp for tp in signal["tps"] if tp != "OPEN"]
    hits = progress.setdefault("tp_hits", [])
    if kind == "tp":
        number = update.get("tp") or len(signal["tps"])
        if update.get("all") or number >= len(signal["tps"]):
            if progress.get("all_hit"):
                return None
            progress["all_hit"] = True
            number = len(priced)
            kind = "all"
        elif number in hits:
            return None
        if not 1 <= number <= len(priced):
            return None
        hits.append(number)
        return {"kind": kind, "tp": number, "price": priced[number - 1]}
    if kind == "set_be":
        if progress.get("sl") == signal["entry"][0]:
            return None
        progress["sl"] = signal["entry"][0]
        return {"kind": "set_be", "price": signal["entry"][0]}
    if kind == "move_sl":
        if update.get("tp"):
            if not 1 <= update["tp"] <= len(priced):
                return None
            price = priced[update["tp"] - 1]
        else:
            price = update["price"]
        if progress.get("sl") == price:
            return None
        progress["sl"] = price
        return {"kind": "move_sl", "tp": update.get("tp"), "price": price}
    if kind in ("sl_hit", "be_hit", "cancel"):
        if progress.get("closed"):
            return None
        progress["closed"] = True
        stop = progress.get("sl") or signal["sl"]
        if kind == "sl_hit" and stop == signal["entry"][0]:
            kind = "be_hit"
        return {"kind": kind, "price": stop}
    if kind == "entry_triggered":
        if progress.get("triggered"):
            return None
        progress["triggered"] = True
        return {"kind": kind, "price": signal["entry"][0]}
    if kind == "close":
        if progress.get("closed"):
            return None
        progress["closed"] = True
        return {"kind": kind, "price": update.get("price")}
    if kind == "entry_updated":
        signal["entry"] = [update["price"]]
        return {"kind": kind, "price": update["price"]}
    return None


def render_update(update, signal) -> Tuple[str, list]:
    b = _Builder()
    kind = update["kind"]
    price = update.get("price")
    entry = entry_price(signal)
    if kind == "tp":
        pips = pips_between(signal, entry, _num(price))
        b.emoji("tick").add(" ").bold(f"TP{update['tp']} HIT · {price}").add("\n")
        b.emoji("present").add(" ").italic(f"{format_pips(pips)} pips secured")
    elif kind == "all":
        pips = pips_between(signal, entry, _num(price))
        b.emoji("boom").add(" ").bold(f"ALL TARGETS HIT · {price}").add(" ").emoji("boom").add("\n")
        b.emoji("present").add(" ").italic(f"{format_pips(pips)} pips · trade closed")
    elif kind == "set_be":
        b.emoji("scythe").add(" ").bold(f"SET BREAKEVEN · SL to {price}").add("\n")
        b.italic("Trade is now risk-free")
    elif kind == "move_sl":
        b.emoji("scythe").add(" ").bold(f"MOVE SL · {price}").add("\n")
        b.italic(f"Profit locked in at TP{update['tp']}" if update.get("tp") else "Stop loss updated")
    elif kind == "be_hit":
        b.emoji("scythe").add(" ").bold(f"CLOSED AT BREAKEVEN · {signal['entry'][0]}").add("\n")
        b.italic("0 pips · capital protected")
    elif kind == "sl_hit":
        pips = pips_between(signal, entry, _num(price))
        if pips > 0:
            b.emoji("tick").add(" ").bold(f"CLOSED IN PROFIT · {price}").add("\n")
            b.emoji("present").add(" ").italic(f"{format_pips(pips)} pips secured")
        else:
            b.emoji("cross").add(" ").bold(f"SL HIT · {price}").add("\n")
            b.italic(f"{format_pips(pips)} pips · next setup soon")
    elif kind == "entry_triggered":
        b.emoji("tick").add(" ").bold(f"ENTRY TRIGGERED · {price}").add("\n")
        b.italic("Order filled · trade is live")
    elif kind == "close":
        if price:
            pips = pips_between(signal, entry, _num(price))
            b.emoji("warning").add(" ").bold(f"CLOSE NOW · {price}").add("\n")
            b.emoji("present").add(" ").italic(f"{format_pips(pips)} pips · take the profit")
        else:
            b.emoji("warning").add(" ").bold("CLOSE NOW").add("\n")
            b.italic("Secure your profits")
    elif kind == "cancel":
        b.emoji("cross").add(" ").bold("ORDER CANCELLED").add("\n")
        b.italic("Pending order removed · setup no longer valid")
    elif kind == "entry_updated":
        b.emoji("star").add(" ").bold(f"ENTRY UPDATED · {price}").add("\n")
        b.italic("New entry for this setup")
    else:
        raise ValueError("Unknown update kind: " + str(kind))
    return b.result()

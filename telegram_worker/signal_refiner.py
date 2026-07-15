import html
import json
import os
import re
from typing import Optional, Dict, Any, List

import requests

from telegram_worker.parser import parse_signal

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-haiku-20240307").strip()
USE_CLAUDE = os.environ.get("USE_CLAUDE_SIGNAL_AI", "1").strip() == "1"

HYBRID_BLOCK_ENABLED = os.environ.get("BLOCK_HYBRID_SIGNALS", "1").strip() == "1"
HYBRID_BLOCK_KEYWORDS = [
    x.strip().lower()
    for x in os.environ.get(
        "HYBRID_BLOCK_KEYWORDS",
        "hybrid,hybrid signal,hybrid signals,hybrid setup,hybrid entry",
    ).split(",")
    if x.strip()
]

CUSTOM_EMOJIS = {
    "DIAMOND": os.environ.get("CUSTOM_EMOJI_DIAMOND", "5427168083074628963"),
    "RED_ALERT": os.environ.get("CUSTOM_EMOJI_RED_ALERT", "5411225014148014586"),
    "UPTREND_CHART": os.environ.get("CUSTOM_EMOJI_UPTREND_CHART", "5244837092042750681"),
    "DOWNTREND_CHART": os.environ.get("CUSTOM_EMOJI_DOWNTREND_CHART", "5246762912428603768"),
    "VERY_GOOD": os.environ.get("CUSTOM_EMOJI_VERY_GOOD", "5206607081334906820"),
    "RED_CROSS": os.environ.get("CUSTOM_EMOJI_RED_CROSS", "5210952531676504517"),
    "PIN_SIGNALS": os.environ.get("CUSTOM_EMOJI_PIN_SIGNALS", "5397782960512444700"),
    "EXCLAMATION_RED": os.environ.get("CUSTOM_EMOJI_EXCLAMATION_RED", "5274099962655816924"),
    "CAUTION_RED": os.environ.get("CUSTOM_EMOJI_CAUTION_RED", "5420323339723881652"),
}

EMOJI_FALLBACKS = {
    "DIAMOND": "💎",
    "RED_ALERT": "🔴",
    "UPTREND_CHART": "📈",
    "DOWNTREND_CHART": "📉",
    "VERY_GOOD": "✅",
    "RED_CROSS": "❌",
    "PIN_SIGNALS": "📌",
    "EXCLAMATION_RED": "❗️",
    "CAUTION_RED": "⚠️",
}


def ce(name: str) -> str:
    doc_id = str(CUSTOM_EMOJIS.get(name, "")).strip()
    emoji = EMOJI_FALLBACKS.get(name, "")
    if doc_id and doc_id.isdigit():
        return f'<tg-emoji emoji-id="{doc_id}">{emoji}</tg-emoji>'
    return emoji


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def clean_text(text: str) -> str:
    return (text or "").replace("\u200b", "").strip()


def is_hybrid_signal(text: str) -> bool:
    if not HYBRID_BLOCK_ENABLED:
        return False
    low = clean_text(text).lower()
    if not low:
        return False
    return any(keyword in low for keyword in HYBRID_BLOCK_KEYWORDS)


def price(x) -> str:
    x = float(x)
    ax = abs(x)

    # Forex-style prices: keep 5 decimals, e.g. EURUSD 1.08750
    if ax < 10:
        return f"{x:.5f}"

    # JPY/metal/smaller symbols: keep 3 decimals, e.g. GBPJPY 191.500
    if ax < 1000:
        return f"{x:.3f}"

    # Indices/gold/crypto large prices
    if abs(x - int(x)) < 0.00001:
        return str(int(x))
    return (f"{x:.2f}").rstrip("0").rstrip(".")


def display_symbol(symbol: str) -> str:
    s = (symbol or "").upper()
    if s in ("NAS100", "US100", "NASDAQ", "NASDAQ100"):
        return "NASDAQ 100"
    if s in ("US500", "SPX500", "SP500"):
        return "US500"
    if s in ("GER40", "DAX", "DAX40"):
        return "GER40"
    return s


def compact_range(high, low) -> str:
    hi = price(high)
    lo = price(low)
    if "." in hi or "." in lo:
        return f"{hi}-{lo}"
    if len(hi) == len(lo) and hi[:-2] == lo[:-2]:
        return f"{hi}-{lo[-2:]}"
    return f"{hi}-{lo}"


def source_line(source_name: str, message_id) -> str:
    source_name = esc(source_name or "ExposedFX")
    suffix = f" #{esc(message_id)}" if message_id else ""
    return f"{ce('DIAMOND')} Source: {source_name}{suffix}"


def all_tps(text: str, parsed: Dict[str, Any]) -> List[float]:
    t = clean_text(text).upper().replace(",", " ")
    values = []
    patterns = [
        r"\bTP\s*(?:#?\s*\d+)?\s*[:\-]?\s*([0-9]{3,7}(?:\.\d+)?)",
        r"\bTARGET\s*(?:#?\s*\d+)?\s*[:\-]?\s*([0-9]{3,7}(?:\.\d+)?)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, t):
            values.append(float(m.group(1)))
    for key in ("tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8", "tp9"):
        if parsed.get(key) is not None:
            values.append(float(parsed[key]))
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out[:8]



def compact_entry_range(low, high) -> str:
    """
    Display entry zone cleanly:
    4134.75 + 4135 -> 4134.75-35
    4161 + 4162.50 -> 4161-62.5
    4100 + 4100.25 -> 4100-00.25
    4057 + 4065 -> 4057-65
    Single entry -> 4065
    """
    lo = float(low)
    hi = float(high)

    if abs(hi - lo) < 0.00001:
        return price(hi)

    lo_s = price(min(lo, hi))
    hi_s = price(max(lo, hi))

    lo_int = lo_s.split(".", 1)[0]
    hi_int = hi_s.split(".", 1)[0]

    if len(lo_int) >= 3 and len(hi_int) >= 2 and lo_int[:-2] == hi_int[:-2]:
        prefix = hi_int[:-2]

        if hi_s.startswith(prefix):
            return f"{lo_s}-{hi_s[len(prefix):]}"

        return f"{lo_s}-{hi_int[-2:]}"

    return f"{lo_s}-{hi_s}"


def symbol_family(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU") or "GOLD" in s:
        return "GOLD"
    if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "USDT")):
        return "CRYPTO"
    if any(x in s for x in ("NAS", "US100", "US30", "SPX", "SP500", "GER", "DAX", "UK100")):
        return "INDEX"
    if len(s) == 6 and s.isalpha():
        return "FOREX"
    return "OTHER"


def auto_risk(symbol: str, direction: str, entry_low: float, entry_high: float, sl: float) -> str:
    """
    Practical trade risk:
    tighter stop = HIGH risk because normal noise can hit SL easier.
    wider stop = LOW risk because the setup has more breathing room.
    """
    lo = float(entry_low)
    hi = float(entry_high)
    stop = float(sl)
    mid = (lo + hi) / 2
    distance = max(abs(hi - stop), abs(lo - stop))
    pct = distance / mid if mid else 999
    fam = symbol_family(symbol)

    if fam == "GOLD":
        if distance <= 8:
            return "HIGH"
        if distance <= 18:
            return "MEDIUM"
        return "LOW"

    if fam == "CRYPTO":
        if pct <= 0.003:
            return "HIGH"
        if pct <= 0.008:
            return "MEDIUM"
        return "LOW"

    if fam == "INDEX":
        if pct <= 0.0025:
            return "HIGH"
        if pct <= 0.006:
            return "MEDIUM"
        return "LOW"

    if fam == "FOREX":
        if pct <= 0.0015:
            return "HIGH"
        if pct <= 0.005:
            return "MEDIUM"
        return "LOW"

    if pct <= 0.003:
        return "HIGH"
    if pct <= 0.007:
        return "MEDIUM"
    return "LOW"



def risk_icon(risk: str) -> str:
    if risk == "LOW":
        return ce("VERY_GOOD")
    if risk == "MEDIUM":
        return ce("EXCLAMATION_RED")
    return ce("CAUTION_RED")


def estimated_layer(direction: str, entry_low: float, entry_high: float, sl: float) -> float:
    lo = float(entry_low)
    hi = float(entry_high)
    stop = float(sl)
    if abs(hi - lo) > 0.00001:
        return lo if direction == "BUY" else hi
    entry = hi
    # Layer half-way between entry and SL, on the risk side.
    return (entry + stop) / 2


def order_type_from_text(text: str, direction: str) -> str:
    t = clean_text(text).upper()

    if direction == "SELL":
        if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS|SHORTING)\b[^\n]{0,50}\b(BELOW|UNDER)\b", t):
            return "SELL_STOP"
        if re.search(r"\bSELL\s+STOP\b", t):
            return "SELL_STOP"
        if re.search(r"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{0,20}\b(BELOW|UNDER)\b", t):
            return "SELL_STOP"
        if re.search(rf"\b(BELOW|UNDER)\s+[0-9]{{1,7}}(?:\.\d+)?\b[^\n]{{0,50}}\b(SELL|SELLS|SHORT|SHORTS)\b", t):
            return "SELL_STOP"

    if direction == "BUY":
        if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS|LONGING)\b[^\n]{0,50}\b(ABOVE|OVER)\b", t):
            return "BUY_STOP"
        if re.search(r"\bBUY\s+STOP\b", t):
            return "BUY_STOP"
        if re.search(r"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{0,20}\b(ABOVE|OVER)\b", t):
            return "BUY_STOP"
        if re.search(rf"\b(ABOVE|OVER)\s+[0-9]{{1,7}}(?:\.\d+)?\b[^\n]{{0,50}}\b(BUY|BUYS|LONG|LONGS)\b", t):
            return "BUY_STOP"

    if re.search(r"\b(BUY|SELL)\s+LIMIT\b", t):
        return "LIMIT"

    return "MARKET_OR_ZONE"


def local_parse(text: str) -> Optional[Dict[str, Any]]:
    if is_hybrid_signal(text):
        return None

    parsed = parse_signal(text)
    if not parsed:
        return None
    tps = all_tps(text, parsed)
    if not tps:
        return None
    direction = str(parsed["direction"]).upper()

    return {
        "is_signal": True,
        "symbol": parsed["symbol"],
        "direction": direction,
        "order_type": str(parsed.get("order_type") or order_type_from_text(text, direction)).upper(),
        "entry_low": float(parsed["entry_low"]),
        "entry_high": float(parsed["entry_high"]),
        "sl": float(parsed["sl"]),
        "tps": tps,
    }


def claude_parse(text: str) -> Optional[Dict[str, Any]]:
    if is_hybrid_signal(text):
        return None

    if not (USE_CLAUDE and ANTHROPIC_API_KEY):
        return None

    system = (
        "You extract actionable trading signals from messy Telegram messages. "
        "Return JSON only. If the text does not contain enough information yet, return {\"is_signal\":false}. "
        "Reject only clear non-trading messages, adverts, analysis-only posts, news, or results. "
        "Accept messy, compact, split, or casual signal wording such as BUY NOW, SELL NOW, long, short, entry, limit, zone, SL, stop, TP, target. "
        "A valid complete signal needs direction, instrument or symbol context, entry or entry zone, stop loss, and at least one take profit/target or TP open. "
        "If the text is multiple messages combined, infer the full signal from all lines. "
        "Normalize symbols but do not force all symbols to XAUUSD. GOLD/XAU/XAUUSD should become XAUUSD. BTC should become BTCUSD. "
        "For entry zones, set entry_low to the lower price and entry_high to the higher price. For a single entry, both should equal the same number. "
        "If TP is open, return tps as an empty array and tp_open as true. "
        "Return exactly these keys: is_signal, symbol, direction, order_type, entry_low, entry_high, sl, tps, tp_open, risk. "
        "direction must be BUY or SELL. order_type must be SELL_STOP, BUY_STOP, LIMIT, or MARKET_OR_ZONE. tps must be an array of numeric take profits in order. risk can be LOW, MEDIUM, HIGH, or empty."
    )

    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 500,
                "temperature": 0,
                "system": system,
                "messages": [{"role": "user", "content": clean_text(text)[:3500]}],
            },
            timeout=18,
        )
        if res.status_code >= 400:
            return None
        data = res.json()
        content = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        obj = json.loads(content)
        if not obj.get("is_signal"):
            return None
        direction = str(obj.get("direction", "")).upper()
        symbol = str(obj.get("symbol", "XAUUSD")).upper().replace("GOLD", "XAUUSD").replace("XAU/USD", "XAUUSD")
        entry_low = float(obj["entry_low"])
        entry_high = float(obj.get("entry_high", entry_low))
        sl = float(obj["sl"])
        tps = [float(x) for x in obj.get("tps", []) if x is not None][:8]
        tp_open = bool(obj.get("tp_open", False)) or "OPEN" in clean_text(text).upper()
        if direction not in ("BUY", "SELL") or (not tps and not tp_open):
            return None
        risk = str(obj.get("risk", "")).upper()
        return {
            "is_signal": True,
            "symbol": symbol,
            "direction": direction,
            "order_type": str(obj.get("order_type") or order_type_from_text(text, direction)).upper(),
            "entry_low": min(entry_low, entry_high),
            "entry_high": max(entry_low, entry_high),
            "sl": sl,
            "tps": tps,
            "tp_open": tp_open,
            "risk": risk if risk in ("LOW", "MEDIUM", "HIGH") else "",
        }
    except Exception:
        return None


def build_message(sig: Dict[str, Any]) -> str:
    direction = sig["direction"].upper()
    order_type = str(sig.get("order_type") or "MARKET_OR_ZONE").upper()
    symbol_raw = sig.get("symbol", "XAUUSD").upper()
    symbol = esc(display_symbol(symbol_raw))
    lo = float(sig["entry_low"])
    hi = float(sig["entry_high"])
    sl = float(sig["sl"])
    tps = [float(x) for x in sig.get("tps", [])][:4]
    risk = str(sig.get("risk") or "").upper()

    if risk not in ("LOW", "MEDIUM", "HIGH"):
        risk = auto_risk(symbol_raw, direction, lo, hi, sl)

    layer = float(sig.get("layer_point") or estimated_layer(direction, lo, hi, sl))

    extra_tips = []

    if order_type == "SELL_STOP":
        trigger = lo
        heading = f"{ce('RED_ALERT')}<b>SELL STOP {symbol} BREAKOUT ZONE</b>"
        entry_lines = [
            f"• Trigger Below : {esc(price(trigger))}",
            f"• Sell Stop Entry : {esc(price(trigger))}",
        ]
        extra_tips = [
            f"Only enter if price breaks below {esc(price(trigger))} {ce('EXCLAMATION_RED')}",
            f"Do not enter early before confirmation {ce('EXCLAMATION_RED')}",
        ]

    elif order_type == "BUY_STOP":
        trigger = hi
        heading = f"{ce('UPTREND_CHART')}<b>BUY STOP {symbol} BREAKOUT ZONE</b>"
        entry_lines = [
            f"• Trigger Above : {esc(price(trigger))}",
            f"• Buy Stop Entry : {esc(price(trigger))}",
        ]
        extra_tips = [
            f"Only enter if price breaks above {esc(price(trigger))} {ce('EXCLAMATION_RED')}",
            f"Do not enter early before confirmation {ce('EXCLAMATION_RED')}",
        ]

    elif direction == "BUY":
        heading = f"{ce('UPTREND_CHART')}<b>BUY {symbol} INTRADAY ZONE</b>"
        entry_lines = [f"• Buy Point : {esc(compact_entry_range(lo, hi))}"]

    else:
        heading = f"{ce('RED_ALERT')}<b>SELL {symbol} ZONE</b>"
        entry_lines = [f"• Sell Point : {esc(compact_entry_range(lo, hi))}"]

    lines = [heading, "", *entry_lines, f"• Stop Loss : {esc(price(sl))}", ""]

    shown_tps = tps[:4]

    for idx, tp in enumerate(shown_tps, 1):
        lines.append(f"{ce('PIN_SIGNALS')}TP{idx} - {esc(price(tp))}")

    open_idx = min(len(shown_tps) + 1, 5)
    lines.append(f"{ce('PIN_SIGNALS')}TP{open_idx} - Open")

    lines += [
        "",
        f"{risk_icon(risk)} RISK: {risk}",
        "",
        "TIPS:",
        *extra_tips,
        f"Breakeven after TP1 HIT {ce('EXCLAMATION_RED')}",
        f"Use correct Risk management {ce('EXCLAMATION_RED')}",
        f"Take spread into consideration when placing SL{ce('EXCLAMATION_RED')}",
        f"THIS IS NOT FINANCIAL ADVICE {ce('CAUTION_RED')}",
    ]

    return "\n".join(lines)


def refine_signal(text: str, source_name: str = "ExposedFX", message_id=None) -> Optional[Dict[str, Any]]:
    text = clean_text(text)
    if not text:
        return None

    if is_hybrid_signal(text):
        return None

    sig = claude_parse(text) or local_parse(text)
    if not sig:
        return None

    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }

import re
from typing import Optional, Dict, Any

from telegram_worker.signal_refiner import build_message, source_line


def looks_like_signal_candidate(text: str) -> bool:
    t = (text or "").upper()
    has_action = bool(re.search(r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT)\b", t))
    has_terms = bool(re.search(r"\b(XAUUSD|XAU|GOLD|ENTRY|ENTRIES|ZONE|LIMIT|SL|STOP|TP\d*|TARGET)\b", t))
    has_price = bool(re.search(r"\b\d{3,6}(?:\.\d+)?\b", t))
    return has_price and (has_action or has_terms)


def _nums(text: str):
    return [float(x) for x in re.findall(r"\b\d{3,6}(?:\.\d+)?\b", text or "")]


def force_format(text: str, source_name: str, message_id=None) -> Optional[Dict[str, Any]]:
    if not looks_like_signal_candidate(text):
        return None

    raw = (text or "").replace("–", "-").replace("—", "-")
    up = raw.upper()

    if re.search(r"\b(BUY|BUYS|LONG)\b", up):
        direction = "BUY"
    elif re.search(r"\b(SELL|SELLS|SHORT)\b", up):
        direction = "SELL"
    else:
        return None

    if not (re.search(r"\b(XAUUSD|XAU|GOLD)\b", up) or any(2500 <= n <= 6000 for n in _nums(up))):
        return None

    sl_match = re.search(r"\b(SL|STOP\s*LOSS|STOP)\b\s*[:\-]?\s*(\d{3,6}(?:\.\d+)?)", up)
    if not sl_match:
        return None
    sl = float(sl_match.group(2))

    tp_values = [float(x) for x in re.findall(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*)\b\s*[:\-]?\s*(\d{3,6}(?:\.\d+)?)", up)]
    if not tp_values:
        return None

    entry_low = entry_high = None
    patterns = [
        r"\b(?:ENTRY|ENTRIES|ZONE|LIMIT)\b\s*[:\-]?\s*(\d{3,6}(?:\.\d+)?)\s*(?:-|TO)?\s*(\d{3,6}(?:\.\d+)?)?",
        r"\b(?:BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD)?\b\D{0,30}(\d{3,6}(?:\.\d+)?)\s*(?:-|TO)\s*(\d{3,6}(?:\.\d+)?)",
        r"\b(?:BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD)?\b\D{0,30}(\d{3,6}(?:\.\d+)?)",
    ]
    for pat in patterns:
        m = re.search(pat, up)
        if not m:
            continue
        a = float(m.group(1))
        b = float(m.group(2)) if len(m.groups()) > 1 and m.group(2) else a
        if a == sl or a in tp_values:
            continue
        entry_low, entry_high = min(a, b), max(a, b)
        break

    if entry_low is None:
        return None

    mid = (entry_low + entry_high) / 2
    if direction == "BUY" and not (sl < mid and tp_values[0] > mid):
        return None
    if direction == "SELL" and not (sl > mid and tp_values[0] < mid):
        return None

    parsed = {
        "is_signal": True,
        "symbol": "XAUUSD",
        "direction": direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tps": tp_values[:8],
    }
    return {
        "is_signal": True,
        "message": build_message(parsed),
        "source": source_line(source_name, message_id),
        "parsed": parsed,
    }

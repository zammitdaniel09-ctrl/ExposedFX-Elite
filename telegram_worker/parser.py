# telegram_bridge/parser.py
# Conservative regex-based trading signal parser.
# It only returns actionable signals with direction + symbol + entry + SL + TP1.

import re
from typing import Optional, Dict, List

SYMBOL_ALIASES = {
    "GOLD": "XAUUSD",
    "XAU": "XAUUSD",
    "XAUUSD": "XAUUSD",
    "XAU/USD": "XAUUSD",
    "NASDAQ": "NAS100",
    "NAS": "NAS100",
    "NAS100": "NAS100",
    "US100": "NAS100",
    "BTC": "BTCUSD",
    "BTCUSD": "BTCUSD",
    "BTC/USD": "BTCUSD",
    "ETH": "ETHUSD",
    "ETHUSD": "ETHUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
}

IGNORE_WORDS = ["BELOW", "ABOVE", "BIAS", "WATCH", "IDEA", "ANALYSIS"]


def clean_text(text: str) -> str:
    text = text or ""
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace("@", " ")
    text = re.sub(r"[✅🔥⚡️⚡🎯💎🚨📍🟢🔴]", " ", text)

    # Handle compact Telegram typing like: SL 3990TP 4020 or TP1-4020TP2-4030
    text = re.sub(r"(?i)(\d)(TP\s*\d*\b)", r"\1 \2", text)
    text = re.sub(r"(?i)(\d)(SL\b|STOP\s*LOSS\b|STOP\b)", r"\1 \2", text)
    text = re.sub(r"(?i)\b(TP\s*\d*|SL|STOP\s*LOSS|STOP)(?=\d)", r"\1 ", text)
    text = re.sub(r"(?i)\b(TP)\s+(\d)\s+", r"\1\2 ", text)

    text = re.sub(r"\s+", " ", text)
    return text.strip()


def find_direction(text: str) -> Optional[str]:
    t = text.upper()
    if re.search(r"\b(BUY|BUYS|LONG)\b", t):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SHORT)\b", t):
        return "SELL"
    return None


def find_symbol(text: str) -> Optional[str]:
    t = text.upper().replace("/", "")
    for alias, sym in SYMBOL_ALIASES.items():
        a = alias.upper().replace("/", "")
        if re.search(rf"\b{re.escape(a)}\b", t):
            return sym

    nums = [float(x) for x in re.findall(r"\b\d{3,5}(?:\.\d+)?\b", t)]
    if any(2500 <= n <= 6000 for n in nums):
        return "XAUUSD"

    return None


def extract_numbers_after(label_regex: str, text: str) -> List[float]:
    t = text.upper()
    pattern = label_regex + r"\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)"
    return [float(m.group(1)) for m in re.finditer(pattern, t)]


def find_sl(text: str) -> Optional[float]:
    vals = extract_numbers_after(r"\b(SL|STOP\s*LOSS|STOP)\b", text)
    return vals[0] if vals else None


def find_tps(text: str) -> List[float]:
    t = text.upper()
    found = []

    patterns = [
        r"\bTP\s*#?\s*\d*\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
        r"\bTAKE\s*PROFIT\s*#?\s*\d*\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
        r"\bTARGET\s*#?\s*\d*\s*[:\-]?\s*([0-9]{1,6}(?:\.\d+)?)",
    ]
    for pat in patterns:
        for m in re.finditer(pat, t):
            found.append(float(m.group(1)))

    # Lines like: TP: 4390 4395 4400
    m = re.search(r"\bTP[S]?\s*[:\-]\s*((?:[0-9]{1,6}(?:\.\d+)?\s*){1,9})", t)
    if m:
        found.extend(float(x) for x in re.findall(r"[0-9]{1,6}(?:\.\d+)?", m.group(1)))

    out = []
    for x in found:
        if x not in out:
            out.append(x)
    return out[:5]


def find_entry_zone(text: str, direction: str, sl: Optional[float], tps: List[float]) -> Optional[tuple]:
    t = text.upper()

    m = re.search(
        r"\b(ENTRY|ENTRIES|ZONE)\s*[:\-]?\s*([0-9]{3,6}(?:\.\d+)?)\s*(?:-|TO)?\s*([0-9]{3,6}(?:\.\d+)?)?", t
    )
    if m:
        a = float(m.group(2))
        b = float(m.group(3)) if m.group(3) else a
        return (min(a, b), max(a, b))

    m = re.search(
        r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD|NAS100|NAS|BTCUSD|BTC|EURUSD|GBPUSD|USDJPY)?\b\D{0,25}([0-9]{3,6}(?:\.\d+)?)\s*(?:-|TO)\s*([0-9]{3,6}(?:\.\d+)?)",
        t,
    )
    if m:
        a = float(m.group(2))
        b = float(m.group(3))
        return (min(a, b), max(a, b))

    m = re.search(
        r"\b(BUY|BUYS|SELL|SELLS|LONG|SHORT)\b.*?\b(?:XAUUSD|XAU|GOLD|NAS100|NAS|BTCUSD|BTC|EURUSD|GBPUSD|USDJPY)?\b\D{0,25}([0-9]{3,6}(?:\.\d+)?)",
        t,
    )
    if m:
        p = float(m.group(2))
        if sl and abs(p - sl) < 0.00001:
            return None
        if any(abs(p - tp) < 0.00001 for tp in tps):
            return None
        return (p, p)

    return None


def validate(
    direction: str, entry_low: float, entry_high: float, sl: float, tps: List[float]
) -> Optional[str]:
    mid = (entry_low + entry_high) / 2
    tp1 = tps[0] if tps else None

    if tp1 is None:
        return "missing TP1"

    if direction == "BUY":
        if sl >= mid:
            return "BUY SL is not below entry"
        if tp1 <= mid:
            return "BUY TP1 is not above entry"

    if direction == "SELL":
        if sl <= mid:
            return "SELL SL is not above entry"
        if tp1 >= mid:
            return "SELL TP1 is not below entry"

    if abs(entry_high - entry_low) > 500:
        return "entry zone too wide"

    return None


def parse_signal(text: str) -> Optional[Dict]:
    raw = text or ""
    text = clean_text(raw)
    if not text:
        return None

    direction = find_direction(text)
    if not direction:
        return None

    symbol = find_symbol(text)
    if not symbol:
        return None

    sl = find_sl(text)
    if sl is None:
        return None

    tps = find_tps(text)
    if not tps:
        return None

    entry = find_entry_zone(text, direction, sl, tps)
    if not entry:
        return None

    entry_low, entry_high = entry
    reason = validate(direction, entry_low, entry_high, sl, tps)
    if reason:
        return None

    return {
        "symbol": symbol,
        "direction": direction,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": sl,
        "tp1": tps[0],
        "tp2": tps[1] if len(tps) > 1 else None,
        "tp3": tps[2] if len(tps) > 2 else None,
        "tp4": tps[3] if len(tps) > 3 else None,
        "tp5": tps[4] if len(tps) > 4 else None,
    }


if __name__ == "__main__":
    samples = [
        "BUY NOW GOLD 4380-4381 TP1: 4390 TP2: 4395 TP3: 4400 SL: 4370",
        "SELL XAUUSD 4535 - 4538 SL 4550 TP1 4520 TP2 4500",
        "XAUUSD BUY NOW 4000-4010 SL 3990TP 4020 TP2 4030 TP3 4040",
        "GOLD SELLS BELOW 4535 TO 4384",
    ]
    for s in samples:
        print("RAW:", s)
        print("CLEAN:", clean_text(s))
        print("PARSED:", parse_signal(s))

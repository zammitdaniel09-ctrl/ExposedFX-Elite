import json
import logging
import os
import re
import time
import unicodedata
from typing import Any, Dict, Optional
from pathlib import Path

import requests

from telegram_worker.signal_refiner import build_message, source_line

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001").strip()
ANTHROPIC_FAST_MODEL = os.environ.get("ANTHROPIC_FAST_MODEL", ANTHROPIC_MODEL or "claude-haiku-4-5-20251001").strip()
ANTHROPIC_STRONG_MODEL = os.environ.get("ANTHROPIC_STRONG_MODEL", "claude-sonnet-4-6").strip()
CLAUDE_DAILY_BUDGET_USD = float(os.environ.get("CLAUDE_DAILY_BUDGET_USD", "8").strip() or "8")
CLAUDE_USAGE_FILE = Path(os.environ.get("DATA_DIR") or "./data") / "claude_usage.json"
USE_CLAUDE = os.environ.get("USE_CLAUDE_SIGNAL_AI", "1").strip() == "1"
AUTO_TP_IF_MISSING = os.environ.get("AUTO_TP_IF_MISSING", "1").strip() == "1"
STRICT_ENTRY_SAFETY = os.environ.get("STRICT_ENTRY_SAFETY", "1").strip() == "1"
CLAUDE_DEBUG_LOGS = os.environ.get("CLAUDE_DEBUG_LOGS", "1").strip() == "1"
ENABLE_TEXT_NORMALIZER = os.environ.get("ENABLE_TEXT_NORMALIZER", "1").strip() == "1"
TEXT_NORMALIZER_DEBUG = os.environ.get("TEXT_NORMALIZER_DEBUG", "1").strip() == "1"
ENABLE_SIGNAL_SANITY_VALIDATION = os.environ.get("ENABLE_SIGNAL_SANITY_VALIDATION", "1").strip() == "1"
ENABLE_PRICE_TYPO_REPAIR = os.environ.get("ENABLE_PRICE_TYPO_REPAIR", "1").strip() == "1"
log = logging.getLogger("universal-signal-ai")

PRICE_RE = r"\d{1,7}(?:\.\d+)?"
FOREX_CURRENCIES = {"EUR", "USD", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF"}
IGNORE_SYMBOLS = {
    "ENTRY", "TARGET", "AROUND", "SIGNAL", "SHORTS", "LONGS", "SELLING",
    "BUYING", "UPDATE", "PROFIT", "LOSSES", "MANAGE", "RISKYY", "RISKED"
}


def mojibake_score(value: str) -> int:
    bad_markers = ("Ã", "Â", "â", "ð", "Ð", "Ø", "Ô", "ƒ", "É", "Ç", "œ", "¢", "�", "\u00ad")
    return sum(value.count(x) for x in bad_markers)


def trade_keyword_score(value: str) -> int:
    u = value.upper()
    keywords = (
        "BUY", "SELL", "GOLD", "XAU", "XAUUSD", "SL", "STOP", "STOPLOSS",
        "TP", "TARGET", "ENTRY", "ABOVE", "BELOW", "UNDER", "OVER",
        "PIPS", "HIT", "RISK",
    )
    return sum(1 for k in keywords if k in u)


def maybe_repair_mojibake(value: str) -> str:
    if not value:
        return value

    candidates = [value]

    for enc in ("latin1", "cp1252"):
        try:
            repaired = value.encode(enc, errors="ignore").decode("utf-8", errors="ignore")
            if repaired and repaired not in candidates:
                candidates.append(repaired)
        except Exception:
            pass

    def rank(candidate: str):
        normal = unicodedata.normalize("NFKC", candidate)
        return (trade_keyword_score(normal), -mojibake_score(normal), len(normal))

    return max(candidates, key=rank)


def normalize_provider_text(value: str) -> str:
    raw = value or ""

    raw = raw.replace("\\n", "\n").replace("\\r", "\n")
    raw = raw.replace("\u200b", " ").replace("\xa0", " ").replace("\u00ad", "")
    raw = raw.replace("→", " ").replace("➡", " ").replace("➜", " ")
    raw = raw.replace("–", "-").replace("—", "-").replace("_", "-")

    if not ENABLE_TEXT_NORMALIZER:
        return raw.strip()

    before = raw
    repaired = maybe_repair_mojibake(raw)
    normalized = unicodedata.normalize("NFKC", repaired)
    normalized = normalized.replace("＄", "$")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)

    if TEXT_NORMALIZER_DEBUG and normalized != before:
        try:
            log.info(
                f"[text normalized] before_score={trade_keyword_score(before)} "
                f"after_score={trade_keyword_score(normalized)} "
                f"before_bad={mojibake_score(before)} after_bad={mojibake_score(normalized)}"
            )
        except Exception:
            pass

    return normalized.strip()


def clean(text: str) -> str:
    return normalize_provider_text(text)


def actionable_signal_text(text: str) -> str:
    """
    If a message says an old trade hit SL and then gives a new trade,
    only parse the new trade part.

    Example:
    Activated and hit stoploss, go in to the next trade:
    SELL LIMIT BTCUSD @ 63000...
    """
    raw = clean(text)
    up = raw.upper()

    markers = [
        "GO IN TO THE NEXT TRADE",
        "GO INTO THE NEXT TRADE",
        "NEXT TRADE:",
        "NEW TRADE:",
        "NEXT SETUP:",
        "NEW SETUP:",
    ]

    has_stop_context = bool(re.search(
        r"\b(HIT\s+STOPLOSS|HIT\s+STOP\s*LOSS|STOPLOSS\s+HIT|STOP\s*LOSS\s+HIT|SL\s+HIT|STOP\s+PRESO|STOPPED\s+OUT)\b",
        up,
    ))

    if not has_stop_context:
        return raw

    best_pos = None
    best_marker = None

    for marker in markers:
        pos = up.find(marker)
        if pos >= 0 and (best_pos is None or pos < best_pos):
            best_pos = pos
            best_marker = marker

    if best_pos is None:
        return raw

    sliced = raw[best_pos + len(best_marker):].strip(" :\n\r\t-")
    if sliced:
        return sliced

    return raw


def is_invalidated_trade_notice(text: str) -> bool:
    """
    Pure management/invalidated message. Do not parse as a fresh signal.
    If it contains a next-trade section, actionable_signal_text() handles it.
    """
    raw = clean(text)
    up = raw.upper()

    has_stop_context = bool(re.search(
        r"\b(HIT\s+STOPLOSS|HIT\s+STOP\s*LOSS|STOPLOSS\s+HIT|STOP\s*LOSS\s+HIT|SL\s+HIT|STOP\s+PRESO|STOPPED\s+OUT)\b",
        up,
    ))

    has_next_trade = bool(re.search(
        r"\b(GO\s+IN\s+TO\s+THE\s+NEXT\s+TRADE|GO\s+INTO\s+THE\s+NEXT\s+TRADE|NEXT\s+TRADE|NEW\s+TRADE|NEXT\s+SETUP|NEW\s+SETUP)\b",
        up,
    ))

    # If it is a stop notice with no new trade, block it completely.
    return has_stop_context and not has_next_trade


def is_trade_management_update(text: str) -> bool:
    raw = clean(text)

    if is_invalidated_trade_notice(raw):
        return True

    t = raw.upper()
    low = raw.lower()

    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))
    has_sl = bool(re.search(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT|ABOVE|BELOW)?\s*[:@\-]?\s*\d", t))
    has_price = bool(re.search(rf"\b{PRICE_RE}\b", t))

    # A real setup can contain "high risk" or "pips" and must NOT be blocked.
    if has_direction and has_sl and has_price:
        return False

    management_patterns = [
        r"\bTP\s*\d*\s*HIT\b",
        r"\bBE\s*HIT\b",
        r"\bSL\s*TO\s*BE\b",
        r"\bSET\s*BE\b",
        r"\bBREAK\s*EVEN\b",
        r"\bBREAKEVEN\b",
        r"\bCLOSE\b",
        r"\bCLOSED\b",
        r"\bCANCEL\b",
        r"\bINVALID\b",
        r"\bSTILL\s+RUNNING\b",
        r"\bRUNNING\b",
        r"\bPARTIAL\b",
        r"\bSECURE\b",
        r"\bCOLLECT\b",
        r"\bWHOSE\s+IN\b",
        r"\bANYONE\s+IN\b",
        r"\bPIPS?\b",
        r"\bR\s*\d+\s+\d+\s*PIPS?\b",
    ]

    if any(re.search(pat, t) for pat in management_patterns):
        return True

    management_words = [
        "take further",
        "take a partial",
        "running trade",
        "remainder should run",
        "set stop loss",
        "move stop loss",
        "move sl",
        "stop loss to",
        "click close",
        "edit the lot size",
        "close half",
        "close 50",
        "secure profits",
        "book profits",
        "being cautious",
        "just looking after us",
    ]

    return any(x in low for x in management_words)


def has_strict_new_signal_requirements(text: str) -> bool:
    """
    Fresh setup gate.
    A complete signal needs direction + SL + usable entry/price.
    Split signals are handled by worker_signal_hub before this function.
    """
    raw = preclean_obvious_gold_sl_typos(actionable_signal_text(text))
    t = raw.upper()

    if is_trade_management_update(raw):
        return False

    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b", t))
    has_sl = bool(re.search(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT|ABOVE|BELOW)?\s*[:@\-]?\s*\d", t))

    if not (has_direction and has_sl):
        return False

    try:
        direction = direction_from_text(raw)
        return extract_breakout_entry(raw, direction) is not None or extract_entry(raw) is not None
    except Exception:
        pass

    compact_patterns = [
        rf"\b(?:BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b[^\n]{{0,80}}{PRICE_RE}",
        rf"\b(?:ENTRY|ENTRIES|ENTER|ENTERING)\b[^\n]{{0,80}}{PRICE_RE}",
        rf"^\s*{PRICE_RE}\s*(?:-|:|/)\s*{PRICE_RE}\s*$",
    ]

    return any(re.search(pat, t, re.M) for pat in compact_patterns)

def normalize_symbol(symbol: str, text: str = "") -> str:
    raw = (text or "").upper()
    t = raw.replace("/", "").replace("-", "").replace(" ", "")
    s = (symbol or "").upper().replace("/", "").replace("-", "").replace(" ", "")

    if "NASDAQ100" in t or "NASDAQ" in raw or "NAS100" in t or "US100" in t or re.search(r"\bNAS\b", raw):
        return "NAS100"
    if "US30" in t or "DOW" in raw:
        return "US30"
    if "US500" in t or "SPX500" in t or "SP500" in t or "S&P" in raw:
        return "US500"
    if "GER40" in t or "DAX" in raw:
        return "GER40"
    if "UK100" in t:
        return "UK100"

    if "BTC" in t or "BITCOIN" in raw or s in {"BTC", "BTCUSD", "BTCUSDT"}:
        return "BTCUSD"
    if "ETH" in t or "ETHEREUM" in raw or s in {"ETH", "ETHUSD", "ETHUSDT"}:
        return "ETHUSD"
    if "SOL" in t or "SOLANA" in raw or s in {"SOL", "SOLUSD", "SOLUSDT"}:
        return "SOLUSD"
    if "XRP" in t:
        return "XRPUSD"

    if "XAU" in t or "GOLD" in raw or s in {"XAU", "XAUUSD", "GOLD"}:
        return "XAUUSD"
    if "XAG" in t or "SILVER" in raw or s in {"XAG", "XAGUSD", "SILVER"}:
        return "XAGUSD"

    for m in re.finditer(r"\b([A-Z]{6})\b", raw.replace("/", "")):
        pair = m.group(1)
        if pair in IGNORE_SYMBOLS:
            continue
        if pair[:3] in FOREX_CURRENCIES and pair[3:] in FOREX_CURRENCIES:
            return pair

    if s and s not in IGNORE_SYMBOLS:
        return s
    return "XAUUSD"


def symbol_family(symbol: str) -> str:
    s = (symbol or "").upper().replace("/", "")
    if s.startswith("XAU") or s == "GOLD":
        return "GOLD"
    if s.startswith("XAG") or s == "SILVER":
        return "METAL"
    if any(x in s for x in ("BTC", "ETH", "SOL", "XRP", "USDT")):
        return "CRYPTO"
    if any(x in s for x in ("NAS", "US100", "US30", "US500", "SPX", "SP500", "GER", "DAX", "UK100")):
        return "INDEX"
    if len(s) == 6 and s[:3] in FOREX_CURRENCIES and s[3:] in FOREX_CURRENCIES:
        return "FOREX"
    return "OTHER"



def looks_like_signal(text: str) -> bool:
    raw = clean(text)
    if has_strict_new_signal_requirements(raw):
        return True

    # Partial pieces are allowed into the buffer by worker_signal_hub.
    t = raw.upper()
    has_direction = bool(re.search(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS|ENTER)\b", t))
    has_symbol = bool(re.search(r"\b(XAUUSD|XAGUSD|GOLD|SILVER|BTC|ETH|SOL|NAS100|NASDAQ|US100|US30|US500|GER40|DAX|[A-Z]{6})\b", t))
    has_sl_or_tp = bool(re.search(r"\b(SL|S/L|STOP|STOPLOSS|STOP\s*LOSS|TP|TARGET|TAKE\s*PROFIT)\b", t))
    has_price = bool(re.search(rf"\b{PRICE_RE}\b", t))
    bare_range = bool(re.search(rf"^\s*{PRICE_RE}\s*(?:-|:|/)\s*{PRICE_RE}\s*$", t, re.M))

    return (has_direction and (has_symbol or has_price)) or (has_sl_or_tp and has_price) or bare_range

def forex_pip_size(symbol: str) -> float:
    s = (symbol or "").upper().replace("/", "")
    return 0.01 if s.endswith("JPY") else 0.0001


def forex_risk_thresholds(symbol: str) -> tuple[float, float]:
    """Return tight/medium limits in pips for each FX pair style."""
    s = (symbol or "").upper().replace("/", "")
    if s.endswith("JPY"):
        if s in {"GBPJPY", "EURJPY"}:
            return 35, 80
        return 25, 60
    if s.startswith("GBP") or s.endswith("GBP"):
        return 20, 55
    if s in {"EURUSD", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD", "EURGBP", "AUDCAD", "AUDNZD"}:
        return 15, 40
    return 20, 50


def percentage_risk_thresholds(symbol: str) -> tuple[float, float]:
    """Return tight/medium limits as decimals, e.g. 0.003 = 0.30%."""
    s = (symbol or "").upper().replace("/", "")
    fam = symbol_family(s)

    if s.startswith("BTC"):
        return 0.0035, 0.0090
    if s.startswith("ETH"):
        return 0.0045, 0.0120
    if s.startswith("SOL"):
        return 0.0075, 0.0180
    if s.startswith("XRP"):
        return 0.0100, 0.0250

    if s in {"NAS100", "US100"}:
        return 0.0025, 0.0065
    if s == "US30":
        return 0.0020, 0.0055
    if s in {"US500", "SPX500", "SP500"}:
        return 0.0020, 0.0050
    if s in {"GER40", "DAX", "DAX40"}:
        return 0.0025, 0.0060

    if s.startswith("XAG"):
        return 0.0040, 0.0100

    if fam == "CRYPTO":
        return 0.0040, 0.0100
    if fam == "INDEX":
        return 0.0025, 0.0060
    if fam == "METAL":
        return 0.0040, 0.0100
    return 0.0030, 0.0070


def dynamic_risk(symbol: str, entry: float, sl: float) -> str:
    """
    Inverted practical signal risk:
    tighter stop = HIGH risk because normal noise can hit SL easier;
    wider stop = LOW risk because the setup has more breathing room.
    """
    symbol = (symbol or "").upper().replace("/", "")
    entry = float(entry)
    sl = float(sl)
    distance = abs(entry - sl)
    pct = distance / entry if entry else 999
    fam = symbol_family(symbol)

    if fam == "GOLD":
        if distance <= 6:
            return "HIGH"
        if distance <= 15:
            return "MEDIUM"
        return "LOW"

    if fam == "FOREX":
        pip_size = forex_pip_size(symbol)
        pips = distance / pip_size
        tight_pips, medium_pips = forex_risk_thresholds(symbol)
        if pips <= tight_pips:
            return "HIGH"
        if pips <= medium_pips:
            return "MEDIUM"
        return "LOW"

    tight_pct, medium_pct = percentage_risk_thresholds(symbol)
    if pct <= tight_pct:
        return "HIGH"
    if pct <= medium_pct:
        return "MEDIUM"
    return "LOW"



def xau_pip_value() -> float:
    # Gold rule: 10 pips = $1, so 1 pip = $0.10
    return 0.1


def pip_value_for_symbol(symbol: str) -> float:
    fam = symbol_family(symbol)
    s = (symbol or "").upper().replace("/", "")

    if fam == "GOLD":
        return xau_pip_value()
    if fam == "FOREX":
        return forex_pip_size(s)
    if fam == "INDEX":
        return 1.0
    if fam == "CRYPTO":
        return 1.0
    if fam == "METAL":
        return 0.01
    return 1.0


def expand_shorthand_price(first: float, second_text: str) -> float:
    """
    Expands shorthand ranges:
    4498-96 -> 4498-4496
    4492-89 -> 4492-4489
    4502-00 -> 4502-4500
    """
    raw = str(second_text).strip()

    if "." in raw:
        return float(raw)

    try:
        second = float(raw)
    except Exception:
        return float(first)

    if abs(first) >= 1000 and 0 <= second < 100:
        first_int = str(int(abs(first)))
        digits = raw.zfill(len(raw))
        prefix = first_int[:-len(digits)]
        candidate = float(prefix + digits)

        # Handles rollover cases like 4502-99 -> 4499
        if candidate > first and (candidate - first) > 50:
            candidate -= 100

        return candidate

    return second



def repair_obvious_gold_price_token(token: str, entry_ref: Optional[float] = None) -> Optional[float]:
    """
    Repairs extreme one-extra-digit XAU/GOLD typo:
    44003 -> 4403 when entry is around 4407.
    Returns None if not safely repairable.
    """
    raw = str(token).strip()

    if not re.fullmatch(r"\d{5,6}(?:\.0+)?", raw):
        return None

    digits = raw.split(".")[0]

    candidates = []
    for i in range(len(digits)):
        fixed = digits[:i] + digits[i+1:]
        if not fixed:
            continue
        try:
            val = float(fixed)
        except Exception:
            continue
        if 1000 <= val <= 9999:
            candidates.append(val)

    if not candidates:
        return None

    if entry_ref:
        near = [x for x in candidates if abs(x - float(entry_ref)) / float(entry_ref) <= 0.08]
        if near:
            return min(near, key=lambda x: abs(x - float(entry_ref)))
        return None

    return candidates[0]


def preclean_obvious_gold_sl_typos(text: str) -> str:
    """
    Before extraction, fix impossible SL tokens like Sl:44003 when entry is nearby.
    This prevents the deeper parser from hanging or generating impossible levels.
    """
    raw = clean(text)
    up = raw.upper()

    if "XAU" not in up and "GOLD" not in up:
        return raw

    # Find rough entry reference from BUY/SELL/LIMIT line.
    entry_ref = None
    entry_match = re.search(
        rf"\b(?:BUY|SELL|BUYS|SELLS|BUYING|SELLING|LONG|SHORT|LIMIT|ENTRY)\b[^\n]{{0,80}}?({PRICE_RE})",
        up,
    )

    if entry_match:
        try:
            entry_ref = float(entry_match.group(1))
        except Exception:
            entry_ref = None

    def repl(m):
        prefix = m.group(1)
        token = m.group(2)
        fixed = repair_obvious_gold_price_token(token, entry_ref)
        if fixed is None:
            return m.group(0)
        log.warning(f"[early sl typo repaired] {token} -> {fixed} text={raw[:80]!r}")
        return f"{prefix}{fixed:g}"

    repaired = re.sub(
        r"(\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT|ABOVE|BELOW)?\s*[:@\-]?\s*)(\d{5,6}(?:\.0+)?)\b",
        repl,
        raw,
        flags=re.I,
    )

    return repaired


def extract_sl_distance_units(text: str) -> Optional[float]:
    """
    Supports:
    SL 100 PIPS
    SL: 100-150 PIPS
    SL 25 POINTS
    """
    up = clean(text).upper()

    patterns = [
        r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT)?\s*[:@\-]?\s*(\d+(?:\.\d+)?)(?:\s*(?:-|/|TO)\s*(\d+(?:\.\d+)?))?\s*(PIP|PIPS|POINT|POINTS)\b",
        r"\b(\d+(?:\.\d+)?)(?:\s*(?:-|/|TO)\s*(\d+(?:\.\d+)?))?\s*(PIP|PIPS|POINT|POINTS)\b[^\n]{0,20}\b(?:SL|STOP)\b",
    ]

    for pat in patterns:
        m = re.search(pat, up)
        if not m:
            continue
        try:
            # Use first value in ranges by default: 100-150 pips -> 100 pips.
            return float(m.group(1))
        except Exception:
            pass

    return None



def explicit_bracket_sl_price(symbol: str, direction: str, entry_mid: float, text: str) -> Optional[float]:
    """
    Exact bracket SL wins:
    SL 10 PIPS (4160) -> 4160
    """
    raw = clean(text)

    patterns = [
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT)?\s*[:@\-]?\s*\d+(?:\.\d+)?\s*(?:PIP|PIPS|POINT|POINTS)\s*\(\s*({PRICE_RE})\s*\)",
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b[^\n]{{0,50}}\(\s*({PRICE_RE})\s*\)",
    ]

    for pat in patterns:
        m = re.search(pat, raw, flags=re.I)
        if not m:
            continue

        try:
            value = float(m.group(1))
        except Exception:
            continue

        fam = symbol_family(symbol)

        if fam == "GOLD" and value < 1000:
            continue

        if entry_mid and abs(value - float(entry_mid)) / float(entry_mid) > 0.25:
            continue

        if direction == "BUY" and value >= float(entry_mid):
            continue

        if direction == "SELL" and value <= float(entry_mid):
            continue

        return value

    return None


def convert_distance_sl_if_needed(symbol: str, direction: str, entry_mid: float, sl_value: float, text: str) -> float:
    """
    Convert distance-style SL into chart price.

    Gold:
    10 pips = $1.
    For zones, caller passes correct risk-side entry:
    BUY uses bottom of zone, SELL uses top of zone.
    """
    raw = clean(text)
    entry_mid = float(entry_mid)
    sl_value = float(sl_value)

    distance_units = extract_sl_distance_units(raw)

    if distance_units is None:
        return sl_value

    explicit = explicit_bracket_sl_price(symbol, direction, entry_mid, raw)
    if explicit is not None:
        log.info(f"[explicit bracket sl used] distance={distance_units} explicit_sl={explicit}")
        return float(explicit)

    distance = float(distance_units) * pip_value_for_symbol(symbol)

    if direction == "BUY":
        return entry_mid - distance

    return entry_mid + distance



def invalid_tp_for_direction(direction: str, entry: float, tp: float) -> bool:
    if direction == "BUY":
        return tp <= entry
    return tp >= entry


def entry_bounds(entry_low: float, entry_high: float) -> tuple[float, float]:
    return min(float(entry_low), float(entry_high)), max(float(entry_low), float(entry_high))


def validation_entry_ref(direction: str, entry_low: float, entry_high: float) -> float:
    lo, hi = entry_bounds(entry_low, entry_high)
    return lo if direction == "BUY" else hi


def tp_entry_ref(direction: str, entry_low: float, entry_high: float) -> float:
    lo, hi = entry_bounds(entry_low, entry_high)
    return hi if direction == "BUY" else lo


def sl_wrong_side(direction: str, entry_low: float, entry_high: float, sl: float) -> bool:
    lo, hi = entry_bounds(entry_low, entry_high)
    sl = float(sl)
    if direction == "BUY":
        return sl >= lo
    return sl <= hi


def tp_wrong_side(direction: str, entry_low: float, entry_high: float, tp: float) -> bool:
    lo, hi = entry_bounds(entry_low, entry_high)
    tp = float(tp)
    if direction == "BUY":
        return tp <= hi
    return tp >= lo


def repair_obvious_sl_typo(symbol: str, direction: str, entry_low: float, entry_high: float, sl: float, text: str) -> float:
    """
    Repairs obvious extra-digit SL typos, e.g. XAU entry 4407 SL 44003 -> 4403.
    Does not guess unclear wrong-side SLs like SELL 4486 SL 4300.
    """
    if not ENABLE_PRICE_TYPO_REPAIR:
        return float(sl)

    fam = symbol_family(symbol)
    sl = float(sl)
    lo, hi = entry_bounds(entry_low, entry_high)
    mid = (lo + hi) / 2

    if fam != "GOLD":
        return sl

    # Only repair extreme impossible gold prices.
    if sl < 10000:
        return sl

    early = repair_obvious_gold_price_token(str(int(abs(sl))), (float(entry_low) + float(entry_high)) / 2)
    if early is not None:
        log.warning(f"[sl typo repaired fast] {sl} -> {early} text={clean(text)[:80]!r}")
        return early

    raw_int = str(int(abs(sl)))
    candidates = []

    for i in range(len(raw_int)):
        fixed = raw_int[:i] + raw_int[i+1:]
        if not fixed:
            continue
        try:
            candidate = float(fixed)
        except Exception:
            continue

        if candidate < 1000:
            continue

        # Candidate must be near entry and on correct SL side.
        if mid and abs(candidate - mid) / mid > 0.08:
            continue

        if direction == "BUY" and candidate < lo:
            candidates.append(candidate)
        elif direction == "SELL" and candidate > hi:
            candidates.append(candidate)

    if not candidates:
        return sl

    best = min(candidates, key=lambda x: abs(x - mid))
    log.warning(f"[sl typo repaired] {sl} -> {best} text={clean(text)[:80]!r}")
    return best


def explicit_tp_wrong_side_exists(text: str, symbol: str, direction: str, entry_low: float, entry_high: float) -> bool:
    """
    If provider explicitly gives a wrong-side TP, reject instead of silently replacing it.
    Skips TP pips lines because those are converted separately.
    """
    raw = clean(text)

    for line in raw.splitlines():
        u = line.upper().strip()

        if not re.search(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT\s*#?\s*\d*)\b", u):
            continue

        if "OPEN" in u:
            continue

        if re.search(r"\b(PIP|PIPS|POINT|POINTS)\b", u):
            continue

        if re.search(r"\b\d{1,2}:\d{2}\b", u) and not re.search(r"\b\d{3,7}(?:\.\d+)?\b", u):
            continue

        body = re.sub(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT\s*#?\s*\d*)\b", "", u, flags=re.I)
        nums = re.findall(PRICE_RE, body)

        for n in nums:
            try:
                tp = float(n)
            except Exception:
                continue

            if not reasonable_tp_price(symbol, tp_entry_ref(direction, entry_low, entry_high), tp):
                continue

            if tp_wrong_side(direction, entry_low, entry_high, tp):
                return True

    return False


def validate_signal_sanity(symbol: str, direction: str, order_type: str, entry_low: float, entry_high: float, sl: float, tps: list[float], tp_open: bool, text: str) -> bool:
    if not ENABLE_SIGNAL_SANITY_VALIDATION:
        return True

    lo, hi = entry_bounds(entry_low, entry_high)
    mid = (lo + hi) / 2
    fam = symbol_family(symbol)

    if fam == "GOLD":
        if lo < 1000 or hi < 1000 or float(sl) < 1000:
            log.warning(f"[signal sanity rejected] tiny gold price entry={lo}-{hi} sl={sl} text={clean(text)[:100]!r}")
            return False

    if fam == "FOREX":
        if hi > 100 or float(sl) > 100:
            log.warning(f"[signal sanity rejected] impossible forex price entry={lo}-{hi} sl={sl}")
            return False

    if mid and abs(float(sl) - mid) / mid > 0.25:
        log.warning(f"[signal sanity rejected] SL too far entry={lo}-{hi} sl={sl} text={clean(text)[:100]!r}")
        return False

    if sl_wrong_side(direction, lo, hi, sl):
        log.warning(f"[signal sanity rejected] SL wrong side direction={direction} entry={lo}-{hi} sl={sl} text={clean(text)[:100]!r}")
        return False

    if explicit_tp_wrong_side_exists(text, symbol, direction, lo, hi):
        log.warning(f"[signal sanity rejected] explicit TP wrong side direction={direction} entry={lo}-{hi} text={clean(text)[:100]!r}")
        return False

    for tp in tps or []:
        if tp_wrong_side(direction, lo, hi, float(tp)):
            log.warning(f"[signal sanity rejected] TP wrong side direction={direction} entry={lo}-{hi} tp={tp}")
            return False

    return True


def risk_from_text(text: str, symbol: str, entry: float, sl: float) -> str:
    t = (text or "").upper()
    if "RISKY" in t or "HIGHER RISK" in t or "HIGH RISK" in t or "VERY HIGH" in t:
        return "HIGH"
    if "MEDIUM RISK" in t:
        return "MEDIUM"
    if "LOW RISK" in t:
        return "LOW"
    return dynamic_risk(symbol, entry, sl)


def estimate_layer(direction: str, entry_low: float, entry_high: float, sl: float) -> float:
    lo = float(entry_low)
    hi = float(entry_high)
    stop = float(sl)
    if abs(hi - lo) > 0.00001:
        return lo if direction == "BUY" else hi
    entry = hi
    return (entry + stop) / 2


def fallback_step(symbol: str, entry: float, sl: float) -> float:
    risk_distance = abs(float(entry) - float(sl))
    fam = symbol_family(symbol)
    if fam == "FOREX":
        return max(risk_distance * 0.5, entry * 0.0008)
    if fam == "INDEX":
        return max(risk_distance * 0.5, entry * 0.0015)
    if fam == "GOLD":
        return max(risk_distance * 0.5, 5)
    if fam == "METAL":
        return max(risk_distance * 0.5, entry * 0.004)
    if fam == "CRYPTO":
        return max(risk_distance * 0.5, entry * 0.002)
    return max(risk_distance * 0.5, entry * 0.002)


def estimate_tps(symbol: str, direction: str, entry: float, sl: float, tps: list[float], tp_open: bool = False) -> list[float]:
    """
    Provider TPs first.
    If provider gives no TP prices, create fake RR targets as real prices:
    TP1 = 1:1, TP2 = 2:1, TP3 = 3:1, TP4 = 4:1.
    """
    cleaned = []

    for x in tps or []:
        if x is None:
            continue

        try:
            v = float(x)
        except Exception:
            continue

        if v not in cleaned:
            cleaned.append(v)

    if cleaned:
        return cleaned[:8]

    if not tp_open and not AUTO_TP_IF_MISSING:
        return []

    entry = float(entry)
    sl = float(sl)
    sign = 1 if direction == "BUY" else -1
    risk_distance = abs(entry - sl)

    if risk_distance <= 0:
        risk_distance = fallback_step(symbol, entry, sl)

    return [entry + sign * risk_distance * rr for rr in (1, 2, 3, 4)]



def direction_from_text(text: str) -> Optional[str]:
    up = text.upper()
    if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS)\b", up):
        return "BUY"
    if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS)\b", up):
        return "SELL"
    return None


def has_entry_keyword(line: str) -> bool:
    return bool(re.search(r"\b(ENTRY|ENTRIES|ENTER|ENTERING|ENTERED\s+AT|ZONE|AREA|AROUND|NOW|LIMIT|ABOVE|BELOW|OVER|UNDER)\b", line.upper()))


def line_contains_protected_trade_level(line: str) -> bool:
    return bool(re.search(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS|STOP|TP|TARGET|TAKE\s*PROFIT)\b", line.upper()))


def remove_protected_trade_level_segments(line: str) -> str:
    """
    Remove SL/TP fragments so their prices are not accidentally treated as entries.
    Example:
    SELL GOLD SL 4450 TP 4430 -> SELL GOLD
    SELL GOLD 4480 SL 4500 -> SELL GOLD 4480
    """
    cleaned = line

    # Remove common SL/TP fragments with one or multiple numbers.
    cleaned = re.sub(
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT|ABOVE|BELOW)?\s*[:@\-]?\s*{PRICE_RE}(?:\s*(?:-|/|,|&|AND)\s*{PRICE_RE})*",
        " ",
        cleaned,
        flags=re.I,
    )

    cleaned = re.sub(
        rf"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT\s*#?\s*\d*)\b\s*[:@\-]?\s*{PRICE_RE}(?:\s*(?:-|/|,|&|AND)\s*{PRICE_RE})*",
        " ",
        cleaned,
        flags=re.I,
    )

    return cleaned


def extract_breakout_entry(text: str, direction: Optional[str] = None) -> Optional[tuple[float, float]]:
    """
    Directly extracts breakout/pending-stop entry prices:
    - SELL UNDER 4450
    - SELL GOLD BELOW 4450
    - BELOW 4450 SELL
    - BUY XAUUSD ABOVE 4510
    - BUYS OVER 4505
    - ABOVE 4510 BUY
    """
    raw = clean(text)
    t = raw.upper()
    direction = direction or direction_from_text(raw)

    if direction not in ("BUY", "SELL"):
        return None

    patterns = []

    if direction == "SELL":
        patterns = [
            rf"\b(?:SELL|SELLS|SELLING|SHORT|SHORTS|SHORTING)\b[^\n]{{0,50}}\b(?:BELOW|UNDER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})",
            rf"\b(?:BELOW|UNDER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})\b[^\n]{{0,50}}\b(?:SELL|SELLS|SELLING|SHORT|SHORTS|SHORTING)\b",
            rf"\bSELL\s+STOP\b[^\n]{{0,30}}({PRICE_RE})",
            rf"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{{0,20}}\b(?:BELOW|UNDER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})",
        ]

    if direction == "BUY":
        patterns = [
            rf"\b(?:BUY|BUYS|BUYING|LONG|LONGS|LONGING)\b[^\n]{{0,50}}\b(?:ABOVE|OVER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})",
            rf"\b(?:ABOVE|OVER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})\b[^\n]{{0,50}}\b(?:BUY|BUYS|BUYING|LONG|LONGS|LONGING)\b",
            rf"\bBUY\s+STOP\b[^\n]{{0,30}}({PRICE_RE})",
            rf"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{{0,20}}\b(?:ABOVE|OVER)\b\s*(?:AT|@|:|\-)?\s*({PRICE_RE})",
        ]

    for pat in patterns:
        m = re.search(pat, t)
        if not m:
            continue
        try:
            price = float(m.group(1))
        except Exception:
            continue

        # Avoid tiny fake gold/default entries.
        symbol_hint = normalize_symbol("", raw)
        if symbol_family(symbol_hint) == "GOLD" and price < 1000:
            continue

        return price, price

    return None


def extract_entry(text: str) -> Optional[tuple[float, float]]:
    """
    Robust entry extractor.

    Fixes:
    - XAUUSD BUY 4494-4493
    - Gold buy 4496
    - Xau/usd buy limit:4504 4502
    - BUY LIMIT XAUUSD then price on next line
    - 4498-96 -> 4498-4496
    - 4424 / 4429 after "gold sell now r 2"
    - ignores R1/R2/risk numbers as fake entries
    - does NOT use SL/TP prices as fake entries
    """
    raw = clean(text)
    up = raw.upper().replace("–", "-").replace("—", "-")
    lines = [line.strip() for line in up.splitlines() if line.strip()]

    direction_hint = direction_from_text(raw)
    breakout = extract_breakout_entry(raw, direction_hint)
    if breakout:
        return breakout

    direction_re = re.compile(r"\b(BUY|BUYS|BUYING|SELL|SELLS|SELLING|LONG|LONGS|SHORT|SHORTS)\b")
    bad_entry_line_re = re.compile(r"\b(SL|S/L|STOP\s*LOSS|STOPLOSS|TP|TARGET|TAKE\s*PROFIT)\b")

    def nums_from(line: str):
        return re.findall(PRICE_RE, line)

    def make_entry(nums):
        if not nums:
            return None

        a = float(nums[0])

        if len(nums) >= 2:
            b = expand_shorthand_price(a, nums[1])
        else:
            b = a

        return min(a, b), max(a, b)

    def is_probable_real_price(v: float) -> bool:
        # Gold/default chart prices should not be tiny R1/R2 style labels.
        # Forex can be < 10, but forex symbols are normally explicit 6-letter pairs.
        if "GOLD" in up or "XAU" in up or not re.search(r"\b[A-Z]{6}\b", up):
            return v >= 1000
        return v > 0

    def clean_entry_nums(line: str):
        nums = nums_from(line)

        filtered = []
        for n in nums:
            # Ignore R1 / R 1 / R2 / R 2 labels.
            if re.search(rf"\bR\s*{re.escape(n)}\b", line):
                continue

            # Ignore risk labels like risk 2.
            if re.search(rf"\bRISK\s*{re.escape(n)}\b", line):
                continue

            filtered.append(n)

        nums = filtered

        # If only one tiny number is found on a direction line, it is probably R2 / risk label.
        if len(nums) == 1:
            try:
                v = float(nums[0])
                if not is_probable_real_price(v):
                    return []
            except Exception:
                return []

        return nums

    # 1) Explicit Entry lines
    for line in lines:
        if not re.search(r"\b(ENTRY|ENTRIES|ENTER|ENTERING|ENTERED\s+AT)\b", line):
            continue

        nums = clean_entry_nums(line)
        result = make_entry(nums)
        if result:
            return result

    # 2) Prefer bare price ranges before/after direction, e.g. "4424 / 4429"
    for line in lines:
        if bad_entry_line_re.search(line):
            continue

        if re.search(rf"^\s*{PRICE_RE}\s*(?:-|:|/)\s*{PRICE_RE}\s*$", line):
            nums = nums_from(line)
            result = make_entry(nums)
            if result:
                return result

    # 3) Direction line with real price on same line
    for i, line in enumerate(lines):
        if not direction_re.search(line):
            continue

        if bad_entry_line_re.search(line) and not direction_re.search(line):
            continue

        working_line = line

        if STRICT_ENTRY_SAFETY and line_contains_protected_trade_level(line) and not has_entry_keyword(line):
            working_line = remove_protected_trade_level_segments(line)

        nums = clean_entry_nums(working_line)

        # Avoid NASDAQ 100 being treated as entry if it is the only number.
        if len(nums) == 1 and re.search(r"\b(NASDAQ\s*100|NAS100|US100)\b", working_line):
            nums = []

        if nums:
            if len(nums) >= 2 and re.search(r"\b(NASDAQ\s*100|NAS100|US100)\b", line):
                nums = nums[-2:]

            result = make_entry(nums)
            if result:
                return result

        # 4) Direction line, price on following line
        for nxt in lines[i + 1:i + 5]:
            if bad_entry_line_re.search(nxt):
                continue

            working_nxt = nxt
            if STRICT_ENTRY_SAFETY and line_contains_protected_trade_level(nxt) and not has_entry_keyword(nxt):
                working_nxt = remove_protected_trade_level_segments(nxt)

            nums = clean_entry_nums(working_nxt)
            result = make_entry(nums)
            if result:
                return result

    return None



def extract_sl(text: str) -> Optional[float]:
    raw = clean(text)
    up = raw.upper()
    vals = []

    patterns = [
        rf"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|STOP)\b\s*(?:TO|AT|ABOVE|BELOW)?\s*[:@\-]?\s*({PRICE_RE})",
        rf"\bSET\s+(?:YOUR\s+)?(?:SL|STOP\s*LOSS|STOPLOSS|STOP)\s*(?:TO|AT)?\s*[:@\-]?\s*({PRICE_RE})",
        rf"\b(?:STOP|STOPLOSS|STOP\s*LOSS)\s+(?:ABOVE|BELOW)\s*({PRICE_RE})",
    ]

    for pat in patterns:
        for x in re.findall(pat, up):
            try:
                vals.append(float(x))
            except Exception:
                pass

    return vals[-1] if vals else None



def extract_tps(text: str) -> tuple[list[float], bool]:
    vals = []
    tp_open = False
    tp_line_re = re.compile(r"\b(?:TP\s*#?\s*\d*|TARGET\s*#?\s*\d*|TAKE\s*PROFIT\s*#?\s*\d*)\b", re.I)

    for line in clean(text).splitlines():
        u = line.upper().strip()
        if not tp_line_re.search(u):
            continue
        if re.search(r"\bOPEN\b", u):
            tp_open = True
            continue
        nums = re.findall(PRICE_RE, u)
        if not nums:
            continue
        v = float(nums[-1])
        if v not in vals:
            vals.append(v)
    return vals, tp_open



def normalise_xau_shorthand_entry_with_sl(symbol: str, direction: str, entry: tuple[float, float], sl: float) -> tuple[float, float]:
    """
    Handles gold shorthand entries:
    Buy now 20-16 + SL 4512 -> 4516-4520
    4424 / 4429 + SL 4433 stays unchanged.
    """
    try:
        lo, hi = float(entry[0]), float(entry[1])
        sl = float(sl)
    except Exception:
        return entry

    if symbol_family(symbol) != "GOLD":
        return entry

    if hi >= 1000 or sl < 1000:
        return entry

    base = int(sl // 100) * 100
    values = [base + lo, base + hi]

    # If reconstructed entry is on the wrong side of SL, try the next/previous 100 block.
    if direction == "BUY" and max(values) <= sl:
        values = [v + 100 for v in values]
    elif direction == "SELL" and min(values) >= sl:
        values = [v - 100 for v in values]

    return min(values), max(values)


def reasonable_tp_price(symbol: str, entry_mid: float, tp: float) -> bool:
    try:
        entry_mid = float(entry_mid)
        tp = float(tp)
    except Exception:
        return False

    fam = symbol_family(symbol)

    # Reject accidental times/labels like TP: scalp dump 5:00 on gold.
    if fam == "GOLD" and tp < 1000:
        return False
    if fam == "FOREX" and tp > 100:
        return False
    if entry_mid and abs(tp - entry_mid) / entry_mid > 0.50:
        return False
    return True



def clean_tp_body_for_numbers(line: str) -> str:
    """
    Keeps provider TP prices:
    TP 4137 -> 4137
    TP #1: 4105 -> 4105
    TP1 4150 -> 4150
    TP 50 pips -> 50 pips
    """
    body = str(line or "").upper().strip()

    body = re.sub(r"\bTAKE\s*PROFIT\b", "TP", body, flags=re.I)
    body = re.sub(r"\bTARGET\b", "TP", body, flags=re.I)
    body = re.sub(r"\bTP\s*#?\s*(?:[1-9]|1[0-9]|20)\b\s*[:.@_\-]?\s*", " ", body, flags=re.I)
    body = re.sub(r"\bTP\b\s*#?\s*[:.@_\-]?\s*", " ", body, flags=re.I)

    return body.strip()


def has_explicit_numeric_tp(text: str) -> bool:
    for line in clean(text).splitlines():
        u = line.upper().strip()

        if not re.search(r"\b(?:TP|TARGET|TAKE\s*PROFIT)\b", u):
            continue

        body = clean_tp_body_for_numbers(u)

        if "OPEN" in body and not re.search(rf"\b{PRICE_RE}\b", body):
            continue

        if re.search(rf"\b{PRICE_RE}\b", body):
            return True

    return False


def extract_tps_contextual(text: str, symbol: str, direction: str, entry_mid: float) -> tuple[list[float], bool]:
    vals: list[float] = []
    tp_open = False
    sign = 1 if direction == "BUY" else -1

    for line in clean(text).splitlines():
        u = line.upper().strip()

        if not re.search(r"\b(?:TP|TARGET|TAKE\s*PROFIT)\b", u):
            continue

        body = clean_tp_body_for_numbers(u)

        if re.search(r"\bOPEN\b", body):
            tp_open = True

        if re.search(r"\b\d{1,2}:\d{2}\b", body) and not re.search(r"\b\d{3,7}(?:\.\d+)?\b", body):
            continue

        if "SAME AS ABOVE" in body:
            continue

        nums = re.findall(PRICE_RE, body)
        if not nums:
            continue

        if re.search(r"\b(PIP|PIPS|POINT|POINTS)\b", body):
            try:
                distance_units = float(nums[-1])
            except Exception:
                continue

            tp = float(entry_mid) + sign * distance_units * pip_value_for_symbol(symbol)

            if not invalid_tp_for_direction(direction, entry_mid, tp) and reasonable_tp_price(symbol, entry_mid, tp):
                if tp not in vals:
                    vals.append(tp)

            continue

        for n in nums:
            try:
                tp = float(n)
            except Exception:
                continue

            if invalid_tp_for_direction(direction, entry_mid, tp):
                continue

            if not reasonable_tp_price(symbol, entry_mid, tp):
                continue

            if tp not in vals:
                vals.append(tp)

    return vals, tp_open



def order_type_from_text(text: str, direction: str) -> str:
    """
    Detect pending order / breakout wording.
    Covers:
    - sells below / sell under / shorts below
    - sell gold below / sell xauusd under
    - buys above / buy over / longs above
    - buy xauusd above / buys gold over
    - buy/sell stop
    - break/breaks/breakout above/below
    - above 4450 buy / below 4450 sell
    """
    t = clean(text).upper()

    if direction == "SELL":
        if re.search(r"\b(SELL|SELLS|SELLING|SHORT|SHORTS|SHORTING)\b[^\n]{0,50}\b(BELOW|UNDER)\b", t):
            return "SELL_STOP"
        if re.search(r"\bSELL\s+STOP\b", t):
            return "SELL_STOP"
        if re.search(r"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{0,20}\b(BELOW|UNDER)\b", t):
            return "SELL_STOP"
        if re.search(rf"\b(BELOW|UNDER)\s+{PRICE_RE}\b[^\n]{{0,50}}\b(SELL|SELLS|SHORT|SHORTS)\b", t):
            return "SELL_STOP"

    if direction == "BUY":
        if re.search(r"\b(BUY|BUYS|BUYING|LONG|LONGS|LONGING)\b[^\n]{0,50}\b(ABOVE|OVER)\b", t):
            return "BUY_STOP"
        if re.search(r"\bBUY\s+STOP\b", t):
            return "BUY_STOP"
        if re.search(r"\b(?:BREAK|BREAKS|BROKE|BREAKOUT|IF\s+BREAKS?)\b[^\n]{0,20}\b(ABOVE|OVER)\b", t):
            return "BUY_STOP"
        if re.search(rf"\b(ABOVE|OVER)\s+{PRICE_RE}\b[^\n]{{0,50}}\b(BUY|BUYS|LONG|LONGS)\b", t):
            return "BUY_STOP"

    if re.search(r"\b(BUY|SELL)\s+LIMIT\b", t):
        return "LIMIT"

    return "MARKET_OR_ZONE"



def regex_extract(text: str) -> Optional[Dict[str, Any]]:
    raw = preclean_obvious_gold_sl_typos(actionable_signal_text(text))

    if not has_strict_new_signal_requirements(raw):
        return None
    if not looks_like_signal(raw):
        return None

    direction = direction_from_text(raw)
    if not direction:
        return None

    symbol = normalize_symbol("", raw)
    entry = extract_breakout_entry(raw, direction) or extract_entry(raw)
    sl = extract_sl(raw)

    if entry is None or sl is None:
        return None

    entry = normalise_xau_shorthand_entry_with_sl(symbol, direction, entry, sl)

    entry_low = float(entry[0])
    entry_high = float(entry[1])
    mid = (entry_low + entry_high) / 2

    order_type = order_type_from_text(raw, direction)

    sl_ref = validation_entry_ref(direction, entry_low, entry_high)
    sl = convert_distance_sl_if_needed(symbol, direction, sl_ref, sl, raw)
    sl = repair_obvious_sl_typo(symbol, direction, entry_low, entry_high, sl, raw)

    tp_ref = tp_entry_ref(direction, entry_low, entry_high)
    tps, tp_open = extract_tps_contextual(raw, symbol, direction, tp_ref)

    if not tps and not tp_open:
        tp_open = True

    tps = [tp for tp in tps if not invalid_tp_for_direction(direction, tp_ref, float(tp))]

    if not tps and not tp_open:
        tp_open = True

    if not validate_signal_sanity(symbol, direction, order_type, entry_low, entry_high, sl, tps, tp_open, raw):
        return None

    return {
        "is_signal": True,
        "symbol": symbol,
        "direction": direction,
        "order_type": order_type,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "sl": float(sl),
        "tps": estimate_tps(symbol, direction, validation_entry_ref(direction, entry_low, entry_high), sl, tps, tp_open),
        "tp_open": True,
        "risk": risk_from_text(raw, symbol, mid, sl),
        "layer_point": estimate_layer(direction, entry_low, entry_high, sl),
    }

def parse_jsonish(value: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(value)
    except Exception:
        pass
    m = re.search(r"\{.*\}", value, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None



CLAUDE_PRICE_PER_MTOK = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


def _today_key():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_claude_usage():
    try:
        if CLAUDE_USAGE_FILE.exists():
            return json.loads(CLAUDE_USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_claude_usage(data):
    try:
        CLAUDE_USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CLAUDE_USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(CLAUDE_USAGE_FILE)
    except Exception:
        pass


def _model_price(model):
    return CLAUDE_PRICE_PER_MTOK.get(model, CLAUDE_PRICE_PER_MTOK.get(model.replace("-20251001", ""), (3.0, 15.0)))


def _record_claude_usage(model, input_tokens, output_tokens):
    data = _load_claude_usage()
    day = _today_key()
    rec = data.get(day, {"usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0})

    in_price, out_price = _model_price(model)
    cost = (float(input_tokens or 0) / 1_000_000.0) * in_price + (float(output_tokens or 0) / 1_000_000.0) * out_price

    rec["usd"] = float(rec.get("usd", 0.0)) + cost
    rec["calls"] = int(rec.get("calls", 0)) + 1
    rec["input_tokens"] = int(rec.get("input_tokens", 0)) + int(input_tokens or 0)
    rec["output_tokens"] = int(rec.get("output_tokens", 0)) + int(output_tokens or 0)
    data[day] = rec

    _save_claude_usage(data)
    return rec["usd"]


def _claude_budget_remaining():
    data = _load_claude_usage()
    used = float((data.get(_today_key()) or {}).get("usd", 0.0))
    return CLAUDE_DAILY_BUDGET_USD - used


def claude_extract(text: str) -> Optional[Dict[str, Any]]:
    text = preclean_obvious_gold_sl_typos(actionable_signal_text(text))
    if not has_strict_new_signal_requirements(text):
        return None
    if not (USE_CLAUDE and ANTHROPIC_API_KEY):
        return None
    if _claude_budget_remaining() <= 0:
        return None

    system = (
        "Extract an actionable trading signal from messy Telegram text. Return JSON only. "
        "Accept any market: forex, metals, crypto, indices. "
        "A complete signal needs direction, entry, and stop loss. If no TP prices are provided, return tps=[] and tp_open=true so the system can create RR targets. Never invent provider TP prices. "
        "If stop loss or take profit is written as pip distance, keep the numeric pip distance and the system will convert it. "
        "For XAUUSD/GOLD, 10 pips equals 1 dollar, so 40 pips equals 4 dollars and 100 pips equals 10 dollars. "
        "Expand shorthand ranges such as 4498-96 as 4498 to 4496, not 4498 to 96. "
        "Do not use SL or TP prices as entry when entry is missing. Ignore R1/R2 labels as entries. "
        "Use latest/current stop loss if several are shown. "
        "For TP open or missing TP prices, return tps=[] and tp_open=true. Never hallucinate TP prices from the provider. "
        "Do not decide risk unless the text explicitly says low risk, medium risk, high risk, higher risk, risky, or very high. Otherwise leave risk empty so the system calculates risk dynamically by pair. "
        "Return: is_signal, symbol, direction, order_type, entry_low, entry_high, sl, tps, tp_open, risk. order_type must be SELL_STOP for sells below/sell stop/break below, BUY_STOP for buys above/buy stop/break above, LIMIT for buy limit/sell limit, otherwise MARKET_OR_ZONE."
    )

    models = []
    for model in [ANTHROPIC_FAST_MODEL, ANTHROPIC_STRONG_MODEL]:
        if model and model not in models:
            models.append(model)

    for model in models:
        if _claude_budget_remaining() <= 0:
            return None

        try:
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 600,
                    "temperature": 0,
                    "system": system,
                    "messages": [{"role": "user", "content": clean(text)[:4500]}],
                },
                timeout=20,
            )

            if res.status_code >= 400:
                if CLAUDE_DEBUG_LOGS:
                    log.warning(f"[claude extract skipped] model={model} status={res.status_code} body={res.text[:200]!r}")
                continue

            data = res.json()
            usage = data.get("usage") or {}
            _record_claude_usage(model, usage.get("input_tokens", 0), usage.get("output_tokens", 0))

            content = "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
            obj = parse_jsonish(content)
            if not obj or not obj.get("is_signal"):
                continue

            direction = str(obj.get("direction", "")).upper()
            if direction not in ("BUY", "SELL"):
                continue

            entry_low = float(obj["entry_low"])
            entry_high = float(obj.get("entry_high", entry_low))
            sl = float(obj["sl"])
            symbol = normalize_symbol(str(obj.get("symbol", "")), text)

            tps = []
            for item in obj.get("tps", []) or []:
                try:
                    tps.append(float(item))
                except Exception:
                    pass

            entry_low = min(entry_low, entry_high)
            entry_high = max(entry_low, entry_high)
            mid = (entry_low + entry_high) / 2
            tp_ref = tp_entry_ref(direction, entry_low, entry_high)

            regex_tps, regex_open = extract_tps_contextual(text, symbol, direction, tp_ref)
            if regex_tps:
                tps = regex_tps

            tp_open = bool(obj.get("tp_open", False)) or regex_open or "OPEN" in clean(text).upper()

            if not has_explicit_numeric_tp(text):
                tps = []
                tp_open = True

            if not tps and not tp_open:
                tp_open = True

            sl_ref = validation_entry_ref(direction, entry_low, entry_high)
            sl = convert_distance_sl_if_needed(symbol, direction, sl_ref, sl, text)
            sl = repair_obvious_sl_typo(symbol, direction, entry_low, entry_high, sl, text)
            tps = [tp for tp in tps if not invalid_tp_for_direction(direction, tp_ref, float(tp))]

            order_type = str(obj.get("order_type") or order_type_from_text(text, direction)).upper()

            if not validate_signal_sanity(symbol, direction, order_type, entry_low, entry_high, sl, tps, tp_open, text):
                continue

            explicit_risk = str(obj.get("risk", "")).upper()
            if explicit_risk in ("LOW", "MEDIUM", "HIGH") and any(x in clean(text).upper() for x in ("LOW RISK", "MEDIUM RISK", "HIGH RISK", "HIGHER RISK", "VERY HIGH", "RISKY")):
                risk = explicit_risk
            else:
                risk = risk_from_text(text, symbol, mid, sl)

            return {
                "is_signal": True,
                "symbol": symbol,
                "direction": direction,
                "order_type": order_type,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "sl": sl,
                "tps": estimate_tps(symbol, direction, validation_entry_ref(direction, entry_low, entry_high), sl, tps, tp_open),
                "tp_open": True,
                "risk": risk,
                "layer_point": estimate_layer(direction, entry_low, entry_high, sl),
            }

        except Exception as exc:
            if CLAUDE_DEBUG_LOGS:
                log.warning(f"[claude extract exception] model={model}: {type(exc).__name__}: {exc}")
            continue

    return None

def extract_and_format(text: str, source_name: str = "ExposedFX", message_id=None) -> Optional[Dict[str, Any]]:
    text = preclean_obvious_gold_sl_typos(actionable_signal_text(text))
    if is_invalidated_trade_notice(text):
        if CLAUDE_DEBUG_LOGS:
            log.info("[extract skipped] invalidated trade notice")
        return None
    if not has_strict_new_signal_requirements(text):
        if CLAUDE_DEBUG_LOGS:
            log.info("[extract skipped] strict requirements failed")
        return None
    sig = regex_extract(text) or claude_extract(text)
    if not sig:
        if CLAUDE_DEBUG_LOGS:
            log.info("[extract skipped] regex+claude returned none")
        return None
    return {
        "is_signal": True,
        "message": build_message(sig),
        "source": source_line(source_name, message_id),
        "parsed": sig,
    }

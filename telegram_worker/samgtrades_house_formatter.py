"""House formatter for SamGtrades whole-chat relay.

Converts the structured SamGtrades XAUUSD signal/update schema into the same
ImperiumFX house presentation used by the existing VIP topic-7 formatter.
Unknown/general-chat messages pass through unchanged.
"""

import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

from telegram_worker.imperium_vip_formatter import build_signal, build_update
from telegram_worker.imperium_vip_trade_update_hardening import (
    classify_trade_update,
)


PRICE = r"\d{1,7}(?:\.\d+)?"
REQUIRED_ALIASES = ("Boom", "GreenTick", "RedCross", "Warning")


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("\u200b", " ").replace("\xa0", " ").replace("\ufe0f", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def registry_ready(registry) -> bool:
    if registry is None:
        return False
    try:
        return all(registry.get(alias) for alias in REQUIRED_ALIASES)
    except Exception:
        return False


def parse_samgtrades_signal(text: str) -> Optional[Dict[str, Any]]:
    raw = _normalise(text)
    upper = raw.upper()

    if not raw or "SIGNAL ALERT" not in upper or "XAUUSD" not in upper:
        return None

    direction_kind = re.search(
        r"\b(BUY|SELL)\s+(LIMIT|STOP|MARKET|NOW)\b",
        raw,
        re.IGNORECASE,
    )
    if not direction_kind:
        return None

    direction = direction_kind.group(1).upper()
    raw_kind = direction_kind.group(2).upper()
    kind = "NOW" if raw_kind in {"MARKET", "NOW"} else raw_kind

    entry_match = re.search(
        rf"^\s*(?:💰\s*)?ENTRY\s*:\s*({PRICE})\b",
        raw,
        re.IGNORECASE | re.MULTILINE,
    )
    sl_match = re.search(
        rf"^\s*(?:🛑\s*)?SL\s*:\s*({PRICE})\b",
        raw,
        re.IGNORECASE | re.MULTILINE,
    )

    tps = []
    for match in re.finditer(
        rf"^\s*(?:✅\s*)?TP\s*\d+\s*:\s*({PRICE})\b",
        raw,
        re.IGNORECASE | re.MULTILINE,
    ):
        value = match.group(1)
        if value not in tps:
            tps.append(value)

    if not entry_match or not sl_match or not tps:
        return None

    entry = entry_match.group(1)
    return {
        "direction": direction,
        "kind": kind,
        "entry_a": entry,
        "entry_b": entry,
        "tps": tps,
        "sl": sl_match.group(1),
    }


def _pip_value(raw: str) -> Optional[str]:
    match = re.search(
        r"\(\s*([+-]?\d+(?:\.\d+)?)\s*PIPS?\s*\)",
        raw,
        re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r"([+-]?\d+(?:\.\d+)?)\s*PIPS?\b",
            raw,
            re.IGNORECASE,
        )
    if not match:
        return None
    return match.group(1).lstrip("+").lstrip("-")


def classify_samgtrades_update(text: str) -> Optional[str]:
    raw = _normalise(text)
    if not raw:
        return None

    upper = raw.upper()

    # Do not let signal alerts fall into generic update classification.
    if parse_samgtrades_signal(raw):
        return None

    if "XAUUSD" in upper and re.search(r"\bCANCEL+ED\b|\bCANCELED\b", upper):
        return "{RedCross} TRADE CANCELLED"

    if "XAUUSD" in upper and re.search(r"\bZONE\s+ACTIVE\b", upper):
        return "{GreenTick} ZONE TRIGGERED/ACTIVATED"

    tp = re.search(r"\bTP\s*(\d+)\s+HIT\b", upper)
    if tp:
        pips = _pip_value(raw)
        if pips:
            return f"{{GreenTick}} TP{tp.group(1)} HIT — +{pips} PIPS"
        return f"{{GreenTick}} TP{tp.group(1)} HIT"

    if re.search(r"\bSL\s+HIT\b", upper):
        pips = _pip_value(raw)
        if pips:
            return f"{{RedCross}} SL HIT — -{pips} PIPS"
        return "{RedCross} SL HIT"

    # Keep the exact same update vocabulary/classifier as VIP topic 7 for any
    # additional provider wording we already know how to classify safely.
    return classify_trade_update(raw)


def format_samgtrades_text(
    text: str,
    original_entities,
    registry,
) -> Tuple[str, Any, Optional[str]]:
    raw = (text or "").strip()
    if not raw or not registry_ready(registry):
        return text, original_entities, None

    upper = raw.upper()
    if "NOT FINANCIAL ADVICE" in upper and "XAUUSD" in upper:
        return text, original_entities, None

    parsed = parse_samgtrades_signal(raw)
    if parsed:
        out_text, out_entities = build_signal(registry, parsed)
        return out_text, out_entities, "signal"

    update_template = classify_samgtrades_update(raw)
    if update_template:
        out_text, out_entities = build_update(registry, update_template)
        return out_text, out_entities, "update"

    return text, original_entities, None


def run_self_test():
    signal = """🎯 SIGNAL ALERT
SELL LIMIT — XAUUSD

📊 Timeframe: 3

💰 Entry: 4183.955
🛑 SL: 4187.810 (38.6 pips)

✅ TP1: 4180.100 (38.6 pips, 1R)
✅ TP2: 4174.317 (96.4 pips, 2.5R)
✅ TP3: 4159.320 (246.4 pips, 6.39R)

Trade responsibly! 📈

🔑 Ref: 1790863020000S"""

    parsed = parse_samgtrades_signal(signal)
    expected = {
        "direction": "SELL",
        "kind": "LIMIT",
        "entry_a": "4183.955",
        "entry_b": "4183.955",
        "tps": ["4180.100", "4174.317", "4159.320"],
        "sl": "4187.810",
    }
    if parsed != expected:
        raise RuntimeError(
            f"SamGtrades signal self-test failed parsed={parsed!r}"
        )

    cases = {
        """🚫 CANCELLED — XAUUSD SELL

Level 4183.955 is no longer valid — a new range has formed.

🔑 Ref: 1790863020000S""": "{RedCross} TRADE CANCELLED",
        """✅ ZONE ACTIVE — XAUUSD SELL

Filled at 4160.080. Tracking has started.

🔑 Ref: 1790863560000S""": "{GreenTick} ZONE TRIGGERED/ACTIVATED",
        """✅ TP1 HIT — XAUUSD

Take Profit reached at 4153.540 (+65.4 pips)

Consider moving SL to break even 🔒""": "{GreenTick} TP1 HIT — +65.4 PIPS",
        """🛑 SL HIT — XAUUSD

Stop Loss reached at 4151.650 (-27.6 pips)

Trade closed — on to the next 🔁""": "{RedCross} SL HIT — -27.6 PIPS",
    }

    for source, expected_template in cases.items():
        got = classify_samgtrades_update(source)
        if got != expected_template:
            raise RuntimeError(
                f"SamGtrades update self-test failed source={source!r} "
                f"expected={expected_template!r} got={got!r}"
            )

    return 1, len(cases)

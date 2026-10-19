"""House formatter for SamGtrades whole-chat relay.

Converts structured SamGtrades XAUUSD signals/updates into the ImperiumFX
house presentation used by the AI-forwarded VIP while adding R/R multiples.

RR policy:
- ENTRY is 0.0 RR.
- SL is -1.0 RR.
- TP RR values are taken from the provider's source signal when present
  (for example 1R / 2.5R / 6.39R), otherwise calculated from entry/SL/TP.
- TP-hit updates recover the exact TP RR from the replied-to source signal.
- SL hit is always -1.0 RR.
- Zone activation and a pending-order cancellation are 0.0 RR.
Unknown/general-chat messages still pass through unchanged.
"""

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from telethon.tl.types import MessageEntityBold

from telegram_worker.imperium_vip_formatter import build_update
from telegram_worker.imperium_vip_trade_update_hardening import (
    classify_trade_update,
)


PRICE = r"\d{1,7}(?:\.\d+)?"
REQUIRED_ALIASES = ("Boom", "GreenTick", "RedCross", "Warning")


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


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


def _decimal(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _format_rr_value(value: Any, signed: bool = True) -> Optional[str]:
    number = _decimal(value)
    if number is None:
        return None

    # Keep useful precision for large/odd provider RR values (6.39R, 10.11R)
    # while displaying standard ratios cleanly (1.0, 2.5, 5.0).
    quantized = number.quantize(Decimal("0.01"))
    text = f"{quantized:.2f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"

    if signed and number > 0:
        return f"+{text} RR"
    return f"{text} RR"


def _computed_rr(entry: str, sl: str, target: str) -> Optional[Decimal]:
    e = _decimal(entry)
    s = _decimal(sl)
    t = _decimal(target)
    if e is None or s is None or t is None:
        return None

    risk = abs(e - s)
    if risk == 0:
        return None
    return abs(t - e) / risk


def _extract_ref(raw: str) -> Optional[str]:
    match = re.search(r"\bREF\s*:\s*([A-Z0-9_-]+)", raw, re.IGNORECASE)
    return match.group(1) if match else None


def _extract_risk_pips(raw: str) -> Optional[Decimal]:
    match = re.search(
        rf"^\s*(?:🛑\s*)?SL\s*:\s*{PRICE}\s*\(\s*([+-]?\d+(?:\.\d+)?)\s*PIPS?",
        raw,
        re.IGNORECASE | re.MULTILINE,
    )
    return _decimal(match.group(1)) if match else None


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

    if not entry_match or not sl_match:
        return None

    entry = entry_match.group(1)
    sl = sl_match.group(1)

    tps: List[str] = []
    rrs: List[Optional[Decimal]] = []

    for line in raw.splitlines():
        match = re.match(
            rf"^\s*(?:✅\s*)?TP\s*\d+\s*:\s*({PRICE})\b(.*)$",
            line,
            re.IGNORECASE,
        )
        if not match:
            continue

        price = match.group(1)
        tail = match.group(2) or ""

        # Source format: TPx: price (pips, 2.5R)
        rr_match = re.search(
            r"(?:^|[,\s(])([+-]?\d+(?:\.\d+)?)\s*R\b",
            tail,
            re.IGNORECASE,
        )
        rr = _decimal(rr_match.group(1)) if rr_match else None
        if rr is None:
            rr = _computed_rr(entry, sl, price)

        if price not in tps:
            tps.append(price)
            rrs.append(rr)

    if not tps:
        return None

    return {
        "direction": direction,
        "kind": kind,
        "entry_a": entry,
        "entry_b": entry,
        "tps": tps,
        "sl": sl,
        "rrs": rrs,
        "risk_pips": _extract_risk_pips(raw),
        "ref": _extract_ref(raw),
    }


def _build_signal_with_rr(registry, parsed):
    direction = parsed["direction"]
    kind = parsed["kind"]
    heading = {
        "NOW": f"XAUUSD {direction} NOW",
        "ZONE": f"XAUUSD {direction} ZONE",
        "LIMIT": f"XAUUSD {direction} LIMIT",
        "STOP": f"XAUUSD {direction} STOP",
    }[kind]

    entry = parsed["entry_a"]
    lines = [
        f"{{Boom}} {heading}",
        "",
        f"ENTRY = {entry} (0.0 RR)",
        "",
    ]

    rrs = list(parsed.get("rrs") or [])
    for idx, price in enumerate(parsed["tps"], 1):
        rr = rrs[idx - 1] if idx - 1 < len(rrs) else None
        rr_text = _format_rr_value(rr, signed=True) or "RR N/A"
        lines.append(
            f"TP{idx}{{GreenTick}} = {price} ({rr_text})"
        )

    lines += [
        "",
        f"SL{{RedCross}} = {parsed['sl']} (-1.0 RR)",
        "",
        "{Warning} NOT FINANCIAL ADVICE {Warning}",
    ]

    text, entities = registry.render("\n".join(lines))

    pos = text.find(heading)
    if pos >= 0:
        entities.append(
            MessageEntityBold(
                offset=_utf16_len(text[:pos]),
                length=_utf16_len(heading),
            )
        )
    entities.sort(key=lambda entity: int(getattr(entity, "offset", 0)))
    return text, entities


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


def _parent_rr_for_tp(
    parent_signal: Optional[Dict[str, Any]],
    tp_number: int,
) -> Optional[Decimal]:
    if not parent_signal:
        # SamGtrades currently defines TP1=1R and TP2=2.5R. TP3 can vary,
        # therefore only these two have safe fallbacks without the parent.
        if tp_number == 1:
            return Decimal("1")
        if tp_number == 2:
            return Decimal("2.5")
        return None

    rrs = list(parent_signal.get("rrs") or [])
    index = tp_number - 1
    if 0 <= index < len(rrs):
        return rrs[index]
    return None


def _append_rr_to_base_update(
    template: str,
    raw: str,
    parent_signal: Optional[Dict[str, Any]],
) -> str:
    upper = template.upper()

    # If a generic update already contains an RR marker, leave it untouched.
    if re.search(r"\bRR\b", upper):
        return template

    if "BREAKEVEN" in upper or "ENTRY STILL VALID" in upper:
        return f"{template} (0.0 RR)"

    pips = _pip_value(raw)
    risk_pips = (
        parent_signal.get("risk_pips")
        if parent_signal
        else None
    )
    if pips is not None and risk_pips:
        pips_num = _decimal(pips)
        risk_num = _decimal(risk_pips)
        if pips_num is not None and risk_num not in (None, Decimal("0")):
            rr = abs(pips_num) / abs(risk_num)
            rr_text = _format_rr_value(rr, signed=True)
            if rr_text:
                return f"{template} ({rr_text})"

    return template


def classify_samgtrades_update(
    text: str,
    parent_text: Optional[str] = None,
) -> Optional[str]:
    raw = _normalise(text)
    if not raw:
        return None

    upper = raw.upper()

    # Do not let signal alerts fall into update classification.
    if parse_samgtrades_signal(raw):
        return None

    parent_signal = (
        parse_samgtrades_signal(parent_text or "")
        if parent_text
        else None
    )

    if "XAUUSD" in upper and re.search(r"\bCANCEL+ED\b|\bCANCELED\b", upper):
        return "{RedCross} TRADE CANCELLED — 0.0 RR"

    if "XAUUSD" in upper and re.search(r"\bZONE\s+ACTIVE\b", upper):
        return "{GreenTick} ZONE TRIGGERED/ACTIVATED — 0.0 RR"

    tp = re.search(r"\bTP\s*(\d+)\s+HIT\b", upper)
    if tp:
        tp_number = int(tp.group(1))
        pips = _pip_value(raw)
        rr = _parent_rr_for_tp(parent_signal, tp_number)
        rr_text = _format_rr_value(rr, signed=True)

        parts = [f"{{GreenTick}} TP{tp_number} HIT"]
        if pips:
            parts.append(f"+{pips} PIPS")
        if rr_text:
            parts.append(f"({rr_text})")
        elif tp_number >= 3:
            parts.append("(RR N/A)")
        return " — ".join(parts[:2]) + (
            f" {parts[2]}" if len(parts) >= 3 else ""
        )

    if re.search(r"\bSL\s+HIT\b", upper):
        pips = _pip_value(raw)
        if pips:
            return f"{{RedCross}} SL HIT — -{pips} PIPS (-1.0 RR)"
        return "{RedCross} SL HIT — -1.0 RR"

    base = classify_trade_update(raw)
    if base:
        return _append_rr_to_base_update(
            base,
            raw,
            parent_signal,
        )

    return None


def format_samgtrades_text(
    text: str,
    original_entities,
    registry,
    parent_text: Optional[str] = None,
) -> Tuple[str, Any, Optional[str]]:
    raw = (text or "").strip()
    if not raw or not registry_ready(registry):
        return text, original_entities, None

    upper = raw.upper()
    if "NOT FINANCIAL ADVICE" in upper and "XAUUSD" in upper:
        return text, original_entities, None

    parsed = parse_samgtrades_signal(raw)
    if parsed:
        out_text, out_entities = _build_signal_with_rr(registry, parsed)
        return out_text, out_entities, "signal"

    update_template = classify_samgtrades_update(
        raw,
        parent_text=parent_text,
    )
    if update_template:
        out_text, out_entities = build_update(
            registry,
            update_template,
        )
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
    if not parsed:
        raise RuntimeError("SamGtrades signal self-test parse failed")

    if parsed["tps"] != ["4180.100", "4174.317", "4159.320"]:
        raise RuntimeError(
            f"SamGtrades TP parse failed: {parsed['tps']!r}"
        )

    got_rr = [
        _format_rr_value(value, signed=True)
        for value in parsed["rrs"]
    ]
    expected_rr = ["+1.0 RR", "+2.5 RR", "+6.39 RR"]
    if got_rr != expected_rr:
        raise RuntimeError(
            f"SamGtrades RR parse failed expected={expected_rr!r} got={got_rr!r}"
        )

    cases = {
        """🚫 CANCELLED — XAUUSD SELL

Level 4183.955 is no longer valid — a new range has formed.

🔑 Ref: 1790863020000S""": "{RedCross} TRADE CANCELLED — 0.0 RR",
        """✅ ZONE ACTIVE — XAUUSD SELL

Filled at 4183.955. Tracking has started.

🔑 Ref: 1790863020000S""": "{GreenTick} ZONE TRIGGERED/ACTIVATED — 0.0 RR",
        """✅ TP1 HIT — XAUUSD

Take Profit reached at 4180.100 (+38.6 pips)

Consider moving SL to break even 🔒

🔑 Ref: 1790863020000S""": "{GreenTick} TP1 HIT — +38.6 PIPS (+1.0 RR)",
        """✅ TP2 HIT — XAUUSD

Take Profit reached at 4174.317 (+96.4 pips)

Runner continues toward TP3 🏃

🔑 Ref: 1790863020000S""": "{GreenTick} TP2 HIT — +96.4 PIPS (+2.5 RR)",
        """✅ TP3 HIT — XAUUSD

Take Profit reached at 4159.320 (+246.4 pips)

🔑 Ref: 1790863020000S""": "{GreenTick} TP3 HIT — +246.4 PIPS (+6.39 RR)",
        """🛑 SL HIT — XAUUSD

Stop Loss reached at 4187.810 (-38.6 pips)

Trade closed — on to the next 🔁

🔑 Ref: 1790863020000S""": "{RedCross} SL HIT — -38.6 PIPS (-1.0 RR)",
    }

    for source, expected_template in cases.items():
        got = classify_samgtrades_update(
            source,
            parent_text=signal,
        )
        if got != expected_template:
            raise RuntimeError(
                f"SamGtrades update self-test failed source={source!r} "
                f"expected={expected_template!r} got={got!r}"
            )

    return 1, len(cases)

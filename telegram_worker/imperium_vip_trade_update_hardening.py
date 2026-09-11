import re
import unicodedata
from typing import Optional, Tuple

from telegram_worker.imperium_vip_trade_updates import (
    classify_trade_update as _base_classify,
    run_update_self_test as _base_self_test,
)


PIP_RE = re.compile(r"([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b", re.IGNORECASE)
PRICE = r"\d{1,7}(?:\.\d+)?"


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("\u200b", " ").replace("\xa0", " ").replace("\ufe0f", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _positive_pips(text: str) -> Optional[str]:
    match = PIP_RE.search(text or "")
    if not match:
        return None
    value = re.sub(r"\s+", "", match.group(1))
    return value.lstrip("+").lstrip("-")


def _target_label(number: Optional[str]) -> str:
    return f"TP{number}" if number else "TP"


def _missed_target(raw: str, compact: str) -> Optional[str]:
    """Explicit non-hit target wording must never become a positive pip result."""
    patterns = (
        r"\bMISSED\s+(?:THE\s+)?(?:TP|T/P|TARGET)\s*#?\s*(\d+)?\s*(?:BY\s+)?(\d+(?:\.\d+)?)\s*PIPS?\b",
        r"\b(?:TP|T/P|TARGET)\s*#?\s*(\d+)?\s+(?:WAS\s+)?MISSED\s+(?:BY\s+)?(\d+(?:\.\d+)?)\s*PIPS?\b",
        r"\b(?:TP|T/P|TARGET)\s*#?\s*(\d+)?\s+(?:MISSED|NOT\s+HIT|DIDN'T\s+HIT|DIDNT\s+HIT)\b.{0,25}\b(?:BY|BY\s+ABOUT|BY\s+AROUND)?\s*(\d+(?:\.\d+)?)\s*PIPS?\b",
        r"\b(?:SHORT|SHY)\s+OF\s+(?:THE\s+)?(?:TP|T/P|TARGET)\s*#?\s*(\d+)?\s+(?:BY\s+)?(\d+(?:\.\d+)?)\s*PIPS?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, compact, re.IGNORECASE)
        if match:
            number = match.group(1) or None
            pips = match.group(2)
            return f"{{Warning}} {_target_label(number)} MISSED BY {pips} PIPS"

    match = re.search(
        r"\b(?:MISSED\s+(?:THE\s+)?(?:TP|T/P|TARGET)|(?:TP|T/P|TARGET)\s*#?\s*(\d+)?\s+(?:WAS\s+)?MISSED)\b",
        compact,
        re.IGNORECASE,
    )
    if match:
        number = match.group(1) if match.lastindex else None
        return f"{{Warning}} {_target_label(number)} MISSED"

    return None


def _multi_target_hit(raw: str, compact: str) -> Optional[str]:
    """Handle TP2,3 / TP2 & 3 / TP2/TP3 / TP2 AND TP3 without dropping targets."""
    match = re.search(
        r"\b(?:TP|T/P|TARGET)\s*#?\s*\d+"
        r"(?:\s*(?:,|&|/|\+|\bAND\b)\s*(?:(?:TP|T/P|TARGET)\s*#?\s*)?\d+)+",
        compact,
        re.IGNORECASE,
    )
    if not match:
        return None

    numbers = []
    for value in re.findall(r"\d+", match.group(0)):
        if value not in numbers:
            numbers.append(value)

    if len(numbers) < 2:
        return None

    explicit_hit = bool(
        re.search(
            r"\b(?:HIT|HITTED|TOUCHED|TAGGED|TRIGGERED|DONE|BANKED|SMASHED|CLEARED|SCALPED)\b",
            compact,
            re.IGNORECASE,
        )
        or PIP_RE.search(raw)
    )
    if not explicit_hit:
        return None

    label = " & ".join(f"TP{number}" for number in numbers)
    pips = _positive_pips(raw)
    if pips:
        return f"{{GreenTick}} {label} HIT — +{pips} PIPS"
    return f"{{GreenTick}} {label} HIT"


def classify_trade_update(text: str) -> Optional[str]:
    """Bulletproof front-end for the normal Imperium update classifier."""
    raw = _normalise(text)
    if not raw:
        return None
    compact = re.sub(r"\s+", " ", raw.upper()).strip()

    # Negative target context must win before broad bare-pips handling.
    missed = _missed_target(raw, compact)
    if missed:
        return missed

    if re.search(
        r"\b(?:DIDN'T|DIDNT|DID\s+NOT|NOT|NEVER)\b.{0,35}\b(?:HIT|REACH|TOUCH)\b.{0,25}\b(?:TP|T/P|TARGET)\b",
        compact,
        re.IGNORECASE,
    ):
        return None

    # Distance/miss commentary is not a realised pip result.
    if PIP_RE.search(raw) and re.search(
        r"\b(?:MISSED|MISS|AWAY|SHORT|SHY|OFF\s+BY|NEEDED|NEED|FROM\s+(?:TP|T/P|TARGET))\b",
        compact,
        re.IGNORECASE,
    ) and not re.search(
        r"(?:^|\s)\+\s*\d|\b(?:PROFIT|SECURED|BANKED|MADE|GAINED|RAN|RUNNING|UP)\b",
        compact,
        re.IGNORECASE,
    ):
        return None

    multi = _multi_target_hit(raw, compact)
    if multi:
        return multi

    # Unnumbered explicit TP hit.
    if (
        re.search(r"\b(?:TP|T/P|TARGET)\b.{0,40}\b(?:HIT|SMASHED|DONE|TAGGED|TOUCHED)\b", compact, re.IGNORECASE)
        or re.search(r"\b(?:HIT|SMASHED|DONE|TAGGED|TOUCHED)\b.{0,40}\b(?:TP|T/P|TARGET)\b", compact, re.IGNORECASE)
    ) and not re.search(r"\b(?:TP|T/P|TARGET)\s*#?\s*\d+", compact, re.IGNORECASE):
        pips = _positive_pips(raw)
        return f"{{GreenTick}} TP HIT — +{pips} PIPS" if pips else "{GreenTick} TP HIT"

    if re.search(
        r"\b(?:CLOSE|EXIT)\s+(?:THE\s+)?(?:TRADE\s+|POSITION\s+)?(?:AT\s+)?ENTRY\s*(?:NOW|HERE|ASAP)?\b",
        compact,
        re.IGNORECASE,
    ):
        return "{Warning} CLOSE AT ENTRY NOW"

    match = re.search(
        rf"\b(?:CHANGE|ADJUST|UPDATE)\s+(?:THE\s+)?(?:SL|S/L|STOP(?:\s*LOSS)?)\s+(?:TO|AT|@)\s*({PRICE})\b",
        compact,
        re.IGNORECASE,
    )
    if match:
        return f"{{Warning}} MOVE SL = {match.group(1)}"

    return _base_classify(raw)


def run_update_self_test() -> Tuple[int, int]:
    base_count, base_safe = _base_self_test()

    cases = {
        "TP1💥130 PIPs": "{GreenTick} TP1 HIT — +130 PIPS",
        "+ 220 PIPs": "{GreenTick} +220 PIPS",
        "TP2💥260 PIPs": "{GreenTick} TP2 HIT — +260 PIPS",
        "Tp hit anyways if u stayed in": "{GreenTick} TP HIT",
        "Missed tp by 10 pips ffs": "{Warning} TP MISSED BY 10 PIPS",
        "Go Breakeven": "{GreenTick} MOVE SL TO BREAKEVEN",
        "TP2,3💥290 PIPs scalped 👋": "{GreenTick} TP2 & TP3 HIT — +290 PIPS",
        "TP2 & 3 hit 290 pips": "{GreenTick} TP2 & TP3 HIT — +290 PIPS",
        "TP2/TP3 smashed": "{GreenTick} TP2 & TP3 HIT",
        "TP1💥smashed": "{GreenTick} TP1 HIT",
        "Change sl to 4335": "{Warning} MOVE SL = 4335",
        "Close at entry now": "{Warning} CLOSE AT ENTRY NOW",
    }

    for source, expected in cases.items():
        got = classify_trade_update(source)
        if got != expected:
            raise RuntimeError(
                f"bulletproof trade-update self-test failed source={source!r} expected={expected!r} got={got!r}"
            )

    safe = {
        "missed the move by 10 pips": None,
        "didn't hit tp because spread was wide": None,
        "tp is 10 pips away": None,
    }
    for source, expected in safe.items():
        got = classify_trade_update(source)
        if got != expected:
            raise RuntimeError(
                f"bulletproof fail-safe classified ambiguous text source={source!r} got={got!r}"
            )

    return base_count + len(cases), base_safe + len(safe)

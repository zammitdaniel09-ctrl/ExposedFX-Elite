import re
import unicodedata
from typing import Optional, Tuple

PIP_RE = re.compile(r"([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b", re.IGNORECASE)
PERCENT_RE = re.compile(r"\b(\d{1,3})\s*%")
PRICE = r"\d{1,7}(?:\.\d+)?"


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("\u200b", " ").replace("\xa0", " ").replace("\ufe0f", "")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _has(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def _pip_number(raw: str) -> Optional[str]:
    m = PIP_RE.search(raw)
    if not m:
        return None
    return re.sub(r"\s+", "", m.group(1))


def _positive_pips(raw: str) -> Optional[str]:
    value = _pip_number(raw)
    if not value:
        return None
    return value.lstrip("+").lstrip("-")


def _append(lines, value: Optional[str]):
    if value and value not in lines:
        lines.append(value)


def _extract_sl_move_price(raw: str) -> Optional[str]:
    patterns = (
        rf"\b(?:MOVE|PUT|SET|BRING|TRAIL)\s+(?:THE\s+)?(?:SL|S/L|STOP(?:\s*LOSS)?)\s+(?:TO|AT|@)?\s*({PRICE})\b",
        rf"\b(?:SL|S/L|STOP(?:\s*LOSS)?)\s+(?:TO|AT|@)\s*({PRICE})\b",
    )
    for pattern in patterns:
        m = re.search(pattern, raw, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def classify_trade_update(text: str) -> Optional[str]:
    """Deterministically clean Imperium trade updates without inventing facts."""
    raw = _normalise(text)
    if not raw or len(raw) > 1200:
        return None

    compact = re.sub(r"\s+", " ", raw.upper()).strip()
    short = len(compact) <= 220
    pips = _positive_pips(raw)

    # Terminal outcomes always win.
    if _has(
        r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b.{0,35}\b(?:HIT|HITTED|TOUCHED|TRIGGERED|TAGGED|SMASHED)\b|"
        r"\b(?:HIT|TOUCHED|TRIGGERED|TAGGED|SMASHED)\b.{0,35}\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b|"
        r"\bSTOPPED\s+OUT\b|\bKNOCKED\s+(?:US\s+)?OUT\b",
        compact,
    ):
        return f"{{RedCross}} SL HIT — -{pips} PIPS" if pips else "{RedCross} SL HIT"

    if _has(
        r"\b(?:BE|B/E|BREAK\s*-?\s*EVEN|BREAKEVEN)\b.{0,30}\b(?:HIT|HITTED|TOUCHED|TAGGED|TRIGGERED|CLOSED|STOPPED)\b|"
        r"\b(?:HIT|TOUCHED|TAGGED|TRIGGERED|CLOSED|STOPPED)\b.{0,30}\b(?:BE|B/E|BREAK\s*-?\s*EVEN|BREAKEVEN)\b|"
        r"\b(?:CLOSED|STOPPED)\s+(?:OUT\s+)?(?:AT\s+)?(?:BE|B/E|BREAK\s*-?\s*EVEN|BREAKEVEN|ENTRY)\b|"
        r"\bSTOP\s+(?:AT\s+)?ENTRY\s+(?:HIT|TOUCHED|TAGGED|TRIGGERED)\b",
        compact,
    ):
        lines = ["{GreenTick} BREAKEVEN HIT — TRADE CLOSED RISK-FREE"]
        if pips and _has(r"(?:^|\s)\+\s*\d|\b(?:PROFIT|SECURED|BANKED|MADE|UP|PLUS)\b", compact):
            lines.append(f"{{GreenTick}} +{pips} PIPS SECURED")
        return "\n\n".join(lines)

    # Do not transform long commentary merely because it mentions TP/BE.
    if len(compact) > 180 and not _has(
        r"^(?:TP\s*#?\s*\d+|T/P\s*#?\s*\d+|ALL\s+TP|FULL\s+TP|FINAL\s+TP|"
        r"\+\s*\d|\d+(?:\.\d+)?\s*PIPS?|RAN\s+\d|RUNNING\s+\d|UP\s+\d|"
        r"SL\b|S/L\b|STOPPED\s+OUT|BE\b|B/E\b|BREAKEVEN\b|GO\s+BE|GO\s+BREAKEVEN|"
        r"MOVE\s+SL|SECURE\s+PARTIAL|TAKE\s+PROFIT|CLOSE\b|EXIT\b|ENTRY\b|LAYER\b|"
        r"DELETE\b|CANCEL\b|DO\s+NOT\s+ENTER|MISSED\s+ENTRY|RE-?ENTER\b|TRAIL\s+SL|LOCK\s+IN\s+PROFIT)",
        compact,
    ):
        return None

    lines = []

    # TP / target results.
    all_targets = _has(
        r"\b(?:ALL\s+(?:TP'?S|TPS|TARGETS?)\s+(?:HIT|DONE|SMASHED|BANKED)|"
        r"FULL\s+TP|FINAL\s+TP\s+(?:HIT|DONE)|TP\s*OPEN\s+(?:HIT|DONE))\b",
        compact,
    )
    if all_targets:
        _append(lines, f"{{GreenTick}} ALL TARGETS HIT — +{pips} PIPS" if pips else "{GreenTick} ALL TARGETS HIT")

    tp_match = re.search(r"\b(?:TP|T/P|TARGET)\s*#?\s*(\d+)\b", compact, re.IGNORECASE)
    if tp_match and not all_targets:
        tp_num = tp_match.group(1)
        if _has(r"\b(?:HIT|HITTED|TOUCHED|TAGGED|TRIGGERED|DONE|BANKED|SMASHED|CLEARED)\b", compact) or bool(PIP_RE.search(raw)):
            _append(lines, f"{{GreenTick}} TP{tp_num} HIT — +{pips} PIPS" if pips else f"{{GreenTick}} TP{tp_num} HIT")

    # Non-SL negative pip update.
    loss_match = re.search(r"(?:^|\b)(?:-|MINUS\s+|LOST\s+|LOSS\s+OF\s+)(\d+(?:\.\d+)?)\s*PIPS?\b", compact, re.IGNORECASE)
    if loss_match and not lines:
        _append(lines, f"{{RedCross}} -{loss_match.group(1)} PIPS")

    pip_context_blocked = _has(
        r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS|LOSS|LOST|DROPPED|DOWN|SPREAD|EXAMPLE|RISK|PIP\s*SL)\b",
        compact,
    )
    if pips and not lines and not pip_context_blocked:
        explicit_positive = _has(r"(?:^|\s)\+\s*\d|\b(?:RAN|RUNNING|UP|PROFIT|SECURED|BANKED|MADE|GAINED|LOCKED)\b", compact)
        bare_short = short and bool(re.fullmatch(r"[+\s\d.A-Z✔✅💥🔥!]+", compact))
        if explicit_positive or bare_short:
            if _has(r"\b(?:RAN|RUNNING|UP)\b", compact):
                _append(lines, f"{{GreenTick}} +{pips} PIPS RUNNING")
            elif _has(r"\b(?:SECURED|BANKED|LOCKED|CLOSED|TAKEN)\b", compact):
                _append(lines, f"{{GreenTick}} +{pips} PIPS SECURED")
            else:
                _append(lines, f"{{GreenTick}} +{pips} PIPS")

    # Partial closes / profit-taking. Percentage handling is exclusive from full-close handling.
    pct_match = PERCENT_RE.search(compact)
    close_pct = bool(pct_match) and _has(r"\b(?:CLOSE|SECURE|TAKE|BANK|BOOK)\b.{0,30}%", compact)
    if close_pct:
        pct = max(1, min(100, int(pct_match.group(1))))
        _append(lines, "{Warning} CLOSE TRADE NOW" if pct >= 100 else f"{{Warning}} CLOSE {pct}% OF POSITION")
    elif _has(r"\b(?:SECURE|TAKE|CLOSE|BANK|BOOK)\b.{0,35}\b(?:PARTIALS?|SOME\s+PROFIT|HALF)\b", compact):
        _append(lines, "{GreenTick} SECURE PARTIAL PROFITS")
    elif short and _has(r"\b(?:TAKE|SECURE|BANK|BOOK)\s+(?:THE\s+)?PROFITS?\b", compact):
        _append(lines, "{GreenTick} SECURE PROFITS")

    # Stop management.
    sl_price = _extract_sl_move_price(raw)
    if sl_price:
        _append(lines, f"{{Warning}} MOVE SL = {sl_price}")

    move_be = _has(
        r"\b(?:GO|MOVE|PUT|SET|BRING|SHIFT|MAKE)\b.{0,24}\b(?:BE|B/E|BREAK\s*-?\s*EVEN|BREAKEVEN|RISK\s*FREE|ENTRY)\b|"
        r"\b(?:BE|B/E|BREAK\s*-?\s*EVEN|BREAKEVEN)\s+(?:NOW|PLEASE|PLS|GUYS|ASAP)\b|"
        r"\b(?:GO|MAKE)\s+RISK\s*FREE\b|\bMEANT\s+(?:BE|B/E|BREAKEVEN|BREAK\s*-?\s*EVEN)\b",
        compact,
    )
    if move_be:
        _append(lines, "{GreenTick} MOVE SL TO BREAKEVEN")

    if _has(r"\b(?:TRADE|POSITION|RUNNER|WE|WE'RE|WE ARE|NOW)\b.{0,30}\bRISK\s*FREE\b|\bFREE\s+TRADE\b", compact) and not move_be:
        _append(lines, "{GreenTick} TRADE IS RISK-FREE")
    if _has(r"\b(?:LOCK|SECURE|MOVE\s+SL\s+INTO|SL\s+IN)\b.{0,25}\bPROFIT\b", compact):
        _append(lines, "{GreenTick} LOCK IN PROFIT")
    if short and _has(r"\b(?:TRAIL|TRAILING)\s+(?:THE\s+)?(?:SL|S/L|STOP(?:\s*LOSS)?)\b", compact) and not sl_price:
        _append(lines, "{Warning} TRAIL STOP LOSS")

    # Entry/order state.
    if _has(r"\b(?:ENTRY|SIGNAL|SETUP|TRADE)\b.{0,28}\b(?:STILL\s+)?VALID\b|\bVALID\s+(?:ENTRY|SETUP|SIGNAL)\b", compact):
        _append(lines, "{GreenTick} ENTRY STILL VALID")
    if _has(
        r"\b(?:ENTRY|ORDER|LIMIT|STOP)\b.{0,22}\b(?:HIT|TRIGGERED|ACTIVATED|FILLED|EXECUTED)\b|"
        r"\b(?:WE'?RE|WE\s+ARE|I'?M|I\s+AM)\s+IN\b|\bTRADE\s+(?:IS\s+)?LIVE\b",
        compact,
    ):
        _append(lines, "{GreenTick} ENTRY ACTIVATED")

    if _has(r"\b(?:ENTRY|SETUP|SIGNAL|ORDER|TRADE)\b.{0,24}\b(?:INVALID|INVALIDATED|CANCELLED|CANCELED)\b", compact):
        _append(lines, "{RedCross} PENDING ORDER CANCELLED" if _has(r"\b(?:ORDER|LIMIT|STOP)\b", compact) else "{RedCross} TRADE CANCELLED")
    if _has(r"\b(?:CANCEL|DELETE|REMOVE)\b.{0,24}\b(?:TRADE|SETUP|SIGNAL)\b", compact):
        _append(lines, "{RedCross} TRADE CANCELLED")

    delete_enter = (
        _has(r"\b(?:DELETE|CANCEL|REMOVE)\b.{0,30}\b(?:PENDING|ORDER|LIMIT|STOP)\b", compact)
        and _has(r"\b(?:ENTER|ENTRY|GET\s+IN)\b.{0,24}\b(?:NOW|MARKET)\b", compact)
    ) or _has(r"\bDELETE\s+N\s+ENTER\s+NOW\b", compact)
    if delete_enter:
        _append(lines, "{Warning} DELETE PENDING ORDER")
        _append(lines, "{Warning} ENTER MARKET NOW")
    elif _has(r"\b(?:CANCEL|DELETE|REMOVE)\b.{0,24}\b(?:PENDING\s+ORDER|ORDER|LIMIT|STOP\s+ORDER)\b", compact):
        _append(lines, "{RedCross} PENDING ORDER CANCELLED")

    if _has(r"\b(?:DO\s+NOT|DON'T|DONT)\s+(?:ENTER|TAKE|EXECUTE)\b|\bNO\s+ENTRY\b", compact):
        _append(lines, "{RedCross} DO NOT ENTER")
    if _has(r"\b(?:MISSED\s+(?:THE\s+)?ENTRY|ENTRY\s+(?:WAS\s+)?MISSED|MISSED\s+IT|DO\s+NOT\s+CHASE|DON'T\s+CHASE|DONT\s+CHASE)\b", compact):
        _append(lines, "{Warning} ENTRY MISSED — DO NOT CHASE")
    if _has(r"\b(?:WAIT|HOLD\s+OFF|STAY\s+OUT)\b.{0,20}\b(?:ENTRY|TRADE|SIGNAL|FOR\s+NOW|NOW)\b", compact):
        _append(lines, "{Warning} WAIT — DO NOT ENTER YET")

    # Position management.
    if _has(r"^(?:LAYER|LAYER\s+NOW|ADD\s+(?:ANOTHER\s+)?ENTRY|ADD\s+(?:ANOTHER\s+)?POSITION|SCALE\s+IN|ADD\s+ON)(?:\b|$)", compact):
        _append(lines, "{Warning} LAYER ENTRY NOW")
    if _has(r"\b(?:RE-?ENTER|REENTRY|RE-ENTRY)\b(?:.{0,20}\b(?:NOW|MARKET)\b)?", compact):
        _append(lines, "{Warning} RE-ENTER TRADE NOW")

    # Full close must be explicit. Generic "CLOSE 70%" must never match here.
    full_close = _has(
        r"\b(?:CLOSE|EXIT)\s+(?:THE\s+)?(?:FULL|ALL|ENTIRE|WHOLE)\b|"
        r"\b(?:CLOSE|EXIT)\s+(?:THE\s+)?(?:TRADE|POSITION|EVERYTHING)\b|"
        r"\b(?:CLOSE|EXIT)\s+(?:NOW|HERE|FULLY)\b|"
        r"\b(?:YOU\s+CAN\s+)?CLOSE\s+FULL\s+TRADE\b|\bGET\s+OUT\s+NOW\b|\bMANUALLY\s+CLOSE\b",
        compact,
    )
    has_partial_scope = bool(pct_match) or _has(r"\b(?:PARTIAL|HALF)\b", compact)
    if full_close and not has_partial_scope:
        _append(lines, "{Warning} CLOSE TRADE NOW")

    if _has(r"\b(?:I'?M|I\s+AM|WE'?RE|WE\s+ARE)\s+STILL\s+IN\b|\b(?:TRADE|POSITION)\s+(?:IS\s+)?STILL\s+(?:ACTIVE|OPEN|RUNNING)\b", compact):
        _append(lines, "{GreenTick} TRADE STILL ACTIVE")
    if _has(r"\b(?:LET|LEAVE)\s+(?:IT|RUNNER|REMAINDER|REST)\s*(?:RUN|RUNNING)?\b|\bHOLD\s+(?:THE\s+)?(?:TRADE|RUNNER|REMAINDER)\b", compact):
        _append(lines, "{GreenTick} LET REMAINDER RUN")
    if _has(r"\b(?:REDUCE|CUT|LOWER)\s+(?:THE\s+)?RISK\b", compact):
        _append(lines, "{Warning} REDUCE RISK")
    if _has(r"\b(?:REMOVE|DELETE)\s+(?:THE\s+)?(?:SL|S/L|STOP\s*LOSS)\b", compact):
        _append(lines, "{Warning} REMOVE STOP LOSS")

    # Keep explicit pip result when paired with management instructions.
    if pips and lines and not any("PIPS" in line for line in lines) and not pip_context_blocked:
        if _has(r"(?:^|\s)\+\s*\d|\b(?:PROFIT|SECURED|BANKED|MADE|GAINED|RAN|RUNNING|UP)\b", compact):
            _append(lines, f"{{GreenTick}} +{pips} PIPS")

    return "\n\n".join(lines) if lines else None


def run_update_self_test() -> Tuple[int, int]:
    cases = {
        "BE HIT trade still did well": "{GreenTick} BREAKEVEN HIT — TRADE CLOSED RISK-FREE",
        "breakeven got tagged": "{GreenTick} BREAKEVEN HIT — TRADE CLOSED RISK-FREE",
        "closed at BE": "{GreenTick} BREAKEVEN HIT — TRADE CLOSED RISK-FREE",
        "stop at entry hit": "{GreenTick} BREAKEVEN HIT — TRADE CLOSED RISK-FREE",
        "SL hit": "{RedCross} SL HIT",
        "Stopped out": "{RedCross} SL HIT",
        "SL hit -50 pips": "{RedCross} SL HIT — -50 PIPS",
        "+60 pips": "{GreenTick} +60 PIPS",
        "230 PIPS ✔️💥": "{GreenTick} +230 PIPS",
        "Ran 170 PIPs and flew": "{GreenTick} +170 PIPS RUNNING",
        "TP1💥80 PIPs": "{GreenTick} TP1 HIT — +80 PIPS",
        "TP 2 hit": "{GreenTick} TP2 HIT",
        "ALL TPS HIT": "{GreenTick} ALL TARGETS HIT",
        "TP2 HIT +250 PIPS!! CLOSE 70%": "{GreenTick} TP2 HIT — +250 PIPS\n\n{Warning} CLOSE 70% OF POSITION",
        "close 50%": "{Warning} CLOSE 50% OF POSITION",
        "+300 PIPS YOU CAN CLOSE FULL TRADE HERE": "{GreenTick} +300 PIPS\n\n{Warning} CLOSE TRADE NOW",
        "TP1 hit +100 pips secure partials and go BE": "{GreenTick} TP1 HIT — +100 PIPS\n\n{GreenTick} SECURE PARTIAL PROFITS\n\n{GreenTick} MOVE SL TO BREAKEVEN",
        "Secure partials and go breakeven": "{GreenTick} SECURE PARTIAL PROFITS\n\n{GreenTick} MOVE SL TO BREAKEVEN",
        "Go breakeven pls": "{GreenTick} MOVE SL TO BREAKEVEN",
        "Meant Breakeven": "{GreenTick} MOVE SL TO BREAKEVEN",
        "go risk free": "{GreenTick} MOVE SL TO BREAKEVEN",
        "trade is risk free now": "{GreenTick} TRADE IS RISK-FREE",
        "move sl to 4360": "{Warning} MOVE SL = 4360",
        "trail sl": "{Warning} TRAIL STOP LOSS",
        "lock in profit": "{GreenTick} LOCK IN PROFIT",
        "take profit": "{GreenTick} SECURE PROFITS",
        "Entry still valid": "{GreenTick} ENTRY STILL VALID",
        "entry triggered": "{GreenTick} ENTRY ACTIVATED",
        "we're in": "{GreenTick} ENTRY ACTIVATED",
        "Delete n enter now": "{Warning} DELETE PENDING ORDER\n\n{Warning} ENTER MARKET NOW",
        "delete pending order": "{RedCross} PENDING ORDER CANCELLED",
        "do not enter": "{RedCross} DO NOT ENTER",
        "entry was missed dont chase": "{Warning} ENTRY MISSED — DO NOT CHASE",
        "layer now": "{Warning} LAYER ENTRY NOW",
        "re-enter now": "{Warning} RE-ENTER TRADE NOW",
        "close now": "{Warning} CLOSE TRADE NOW",
        "I’m still in this btw": "{GreenTick} TRADE STILL ACTIVE",
        "let runner run": "{GreenTick} LET REMAINDER RUN",
        "reduce risk": "{Warning} REDUCE RISK",
        "cancel trade": "{RedCross} TRADE CANCELLED",
    }

    for source, expected in cases.items():
        got = classify_trade_update(source)
        if got != expected:
            raise RuntimeError(f"trade-update self-test failed source={source!r} expected={expected!r} got={got!r}")

    safe = (
        "wtf",
        "Everyone recovered we happy",
        "are we happy yet can I go off charts now",
        "20 pip sl",
        "markets are rough today",
        "keep tp few pips higher example like now tp is 4360 keep tp at 4360.5",
        "ONLY FOR TESTING IGNORE.",
        "Communication is key I wanna make sure we all making money in here",
        "Once tp1 hits the tp2 entry go Breakeven and let it run if it comes back to breakeven minor you made money from tp1 if it goes tp2 ur swimming in money and this is why splitting entries helps over a full session",
    )
    for source in safe:
        got = classify_trade_update(source)
        if got is not None:
            raise RuntimeError(f"trade-update fail-safe classified general chat source={source!r} got={got!r}")

    return len(cases), len(safe)

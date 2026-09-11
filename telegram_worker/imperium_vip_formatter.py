import logging
import os
import re
import unicodedata
from typing import Optional, Tuple, List, Dict, Any

from telethon import events
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji


log = logging.getLogger("imperium-vip-formatter")

PRICE = r"\d{1,7}(?:\.\d+)?"
DIRECTION_RE = re.compile(r"\b(BUY|BUYS|BUYING|LONG|LONGS|SELL|SELLS|SELLING|SHORT|SHORTS)\b", re.IGNORECASE)
XAU_RE = re.compile(r"\b(XAUUSD|XAU/USD|XAU|GOLD)\b", re.IGNORECASE)
RANGE_RE = re.compile(rf"({PRICE})\s*(?:-|–|—|TO)\s*({PRICE})", re.IGNORECASE)
SL_LINE_RE = re.compile(
    rf"^[ \t]*(?:SL|S/L|STOP[ \t]*LOSS|STOPLOSS)[ \t]*(?:IS|AT|TO)?[ \t]*[:=\-]?[ \t]*({PRICE})\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


def _normalise(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("’", "'").replace("‘", "'")
    value = value.replace("–", "-").replace("—", "-")
    value = value.replace("\u200b", " ").replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _normalise_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = re.sub(r"[^A-Za-z0-9]+", " ", value).strip().upper()
    return re.sub(r"\s+", " ", value)


def _chat_id_from_env() -> Optional[int]:
    raw = os.environ.get("IMPERIUM_VIP_CHAT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def resolve_imperium_vip_chat(client, logger=None) -> Tuple[Optional[int], str]:
    logger = logger or log
    configured = _chat_id_from_env()
    if configured is not None:
        try:
            entity = await client.get_entity(configured)
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(configured)
        except Exception:
            title = str(configured)
        return configured, str(title)

    exact = []
    fallback = []
    async for dialog in client.iter_dialogs():
        title = str(getattr(dialog, "name", None) or getattr(getattr(dialog, "entity", None), "title", None) or "")
        norm = _normalise_title(title)
        if norm == "IMPERIUM FX VIP":
            exact.append((int(dialog.id), title))
        elif "IMPERIUM" in norm and "VIP" in norm and "FX" in norm:
            fallback.append((int(dialog.id), title))

    matches = exact or fallback
    if not matches:
        logger.warning("[IMPERIUM VIP FORMATTER] target chat not found by title")
        return None, ""
    chosen = matches[0]
    if len(matches) > 1:
        logger.warning("[IMPERIUM VIP FORMATTER] multiple title matches=%s choosing=%s", matches, chosen)
    return chosen


def _first_trade_line(raw: str) -> str:
    for line in raw.splitlines():
        if DIRECTION_RE.search(line) and XAU_RE.search(line):
            return line.strip()
    return ""


def _direction(raw: str) -> Optional[str]:
    m = DIRECTION_RE.search(raw)
    if not m:
        return None
    token = m.group(1).upper()
    return "BUY" if token in {"BUY", "BUYS", "BUYING", "LONG", "LONGS"} else "SELL"


def _extract_tps(raw: str) -> List[str]:
    """Extract TP values safely.

    Numbered and unnumbered TP lines are parsed separately so `TP 4400`
    can never backtrack into index=440 and price=0.
    """
    out: List[str] = []
    prefix = r"(?:TP|T/P|TAKE\s*PROFIT|TARGET)"
    patterns = [
        re.compile(rf"^\s*{prefix}\s*#?\s*\d+\s*[:=\-]\s*({PRICE}|OPEN)\b", re.IGNORECASE),
        re.compile(rf"^\s*{prefix}\s*#?\s*\d+\s+({PRICE}|OPEN)\b", re.IGNORECASE),
        re.compile(rf"^\s*{prefix}\s*[:=\-]?\s*({PRICE}|OPEN)\b", re.IGNORECASE),
    ]
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = None
        for pat in patterns:
            m = pat.match(line)
            if m:
                break
        if not m:
            continue
        value = "OPEN" if m.group(1).upper() == "OPEN" else m.group(1)
        if value not in out:
            out.append(value)
    return out


def _extract_sl(raw: str) -> Optional[str]:
    m = SL_LINE_RE.search(raw)
    return m.group(1) if m else None


def _extract_entry_from_trade_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    if not line:
        return None, None
    range_match = RANGE_RE.search(line)
    if range_match:
        return range_match.group(1), range_match.group(2)
    cleaned = XAU_RE.sub(" ", line)
    cleaned = DIRECTION_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\b(NOW|MARKET|LIMIT|STOP|ENTRY|ENTRIES|ENTER|AT|ZONE|PRICE)\b|@", " ", cleaned, flags=re.IGNORECASE)
    nums = re.findall(PRICE, cleaned)
    if nums:
        return nums[0], nums[0]
    return None, None


def parse_xauusd_signal(text: str) -> Optional[Dict[str, Any]]:
    raw = _normalise(text)
    if not raw or not XAU_RE.search(raw):
        return None
    upper = raw.upper()
    if "NOT FINANCIAL ADVICE" in upper and ("TP1" in upper or "ENTRY =" in upper):
        return None

    direction = _direction(raw)
    tps = _extract_tps(raw)
    sl = _extract_sl(raw)
    trade_line = _first_trade_line(raw)
    if not direction or not tps or not sl or not trade_line:
        return None

    entry_a, entry_b = _extract_entry_from_trade_line(trade_line)
    line_up = trade_line.upper()
    if re.search(r"\b(?:BUY|SELL)\s+LIMIT\b", line_up):
        kind = "LIMIT"
    elif re.search(r"\b(?:BUY|SELL)\s+STOP\b", line_up):
        kind = "STOP"
    elif entry_a and entry_b and entry_a != entry_b:
        kind = "ZONE"
    elif re.search(r"\bNOW\b|\bMARKET\b|\bENTER\s+NOW\b", line_up):
        kind = "NOW"
    elif entry_a:
        kind = "ZONE"
    else:
        return None

    if kind in {"LIMIT", "STOP", "ZONE"} and not entry_a:
        return None
    return {"direction": direction, "kind": kind, "entry_a": entry_a, "entry_b": entry_b, "tps": tps, "sl": sl}


def _fmt_entry(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return None
    if not b or a == b:
        return a
    return f"{a} - {b}"


def _require_aliases(registry, aliases: List[str]) -> bool:
    return all(registry.get(alias) for alias in aliases)


def _add_bold(text: str, entities: list, phrase: str):
    pos = text.find(phrase)
    if pos >= 0:
        entities.append(MessageEntityBold(offset=_utf16_len(text[:pos]), length=_utf16_len(phrase)))


def build_signal(registry, parsed):
    direction = parsed["direction"]
    kind = parsed["kind"]
    heading = {
        "NOW": f"XAUUSD {direction} NOW",
        "ZONE": f"XAUUSD {direction} ZONE",
        "LIMIT": f"XAUUSD {direction} LIMIT",
        "STOP": f"XAUUSD {direction} STOP",
    }[kind]

    lines = [f"{{Boom}} {heading}", ""]
    entry = _fmt_entry(parsed.get("entry_a"), parsed.get("entry_b"))
    if entry:
        lines += [f"ENTRY = {entry}", ""]
    for idx, value in enumerate(parsed["tps"], 1):
        lines.append(f"TP{idx}{{GreenTick}} = {value}")
    lines += ["", f"SL{{RedCross}} = {parsed['sl']}", "", "{Warning} NOT FINANCIAL ADVICE {Warning}"]

    text, entities = registry.render("\n".join(lines))
    _add_bold(text, entities, heading)
    entities.sort(key=lambda e: int(getattr(e, "offset", 0)))
    return text, entities


def _pip_number(raw: str) -> Optional[str]:
    for pat in (
        r"([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b",
        r"\b(?:DROPPED|LOST|DOWN)\s+([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b",
    ):
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return None


def classify_update(text: str) -> Optional[str]:
    raw = _normalise(text)
    if not raw or len(raw) > 700:
        return None
    u = raw.upper()
    compact = re.sub(r"\s+", " ", u).strip()
    if parse_xauusd_signal(raw):
        return None

    sl_hit = bool(re.search(
        r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b.{0,30}\b(?:HIT|HITTED|TOUCHED|TRIGGERED|TAGGED)\b|"
        r"\b(?:HIT|TOUCHED|TRIGGERED|TAGGED)\b.{0,30}\b(?:SL|STOP\s*LOSS|STOPLOSS)\b|"
        r"\bSTOPPED\s+OUT\b|\bKNOCKED\s+(?:US\s+)?OUT\b",
        compact, re.IGNORECASE,
    ))
    if sl_hit:
        pips = _pip_number(raw)
        if pips:
            return f"{{RedCross}} SL HIT — -{pips.lstrip('+').lstrip('-')} PIPS"
        return "{RedCross} SL HIT"

    m_tp = re.search(r"\b(?:TP|T/P|TARGET)\s*#?\s*(\d+)\b", u)
    if m_tp:
        tp_num = m_tp.group(1)
        has_result = bool(re.search(r"\b(HIT|HITTED|TOUCHED|TAGGED|DONE|BANKED|SMASHED)\b", u)) or bool(re.search(r"\d+(?:\.\d+)?\s*PIPS?\b", u))
        if has_result:
            pips = _pip_number(raw)
            if pips:
                return f"{{GreenTick}} TP{tp_num} HIT — +{pips.lstrip('+').lstrip('-')} PIPS"
            return f"{{GreenTick}} TP{tp_num} HIT"

    if not re.search(r"\b(?:LOSS|LOST|DROPPED|SL|STOP\s*LOSS|SPREAD|KEEP\s+TP|EXAMPLE)\b", u):
        m = re.search(r"^\s*\+\s*(\d+(?:\.\d+)?)\s*PIPS?\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS"
        m = re.search(r"\b(?:RAN|RUNNING|UP)\s*\+?\s*(\d+(?:\.\d+)?)\s*PIPS?\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS RUNNING"
        m = re.search(r"^\s*(\d+(?:\.\d+)?)\s*PIPS?\s+(?:RUNNING|SECURED|BANKED|PROFIT|IN\s+PROFIT)\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS"

    if re.search(r"\b(?:SECURE|TAKE|CLOSE|BANK)\b.{0,30}\bPARTIAL", u) and re.search(r"\b(?:BREAK\s*-?\s*EVEN|BREAKEVEN|BE)\b", u):
        return "{GreenTick} SECURE PARTIAL PROFITS\n\nMOVE SL TO BREAKEVEN"
    if re.search(r"\b(?:BREAK\s*-?\s*EVEN|BREAKEVEN)\b", u) or re.search(r"\b(?:MOVE|PUT|SET)\s+(?:SL|STOP(?:\s*LOSS)?)\s+(?:TO\s+)?(?:BE|ENTRY)\b", u):
        return "{GreenTick} MOVE SL TO BREAKEVEN"
    if re.search(r"\b(?:SECURE|TAKE|CLOSE|BANK)\b.{0,30}\bPARTIAL", u):
        return "{GreenTick} SECURE PARTIAL PROFITS"
    if re.search(r"\b(?:ENTRY|SIGNAL|SETUP|TRADE)\b.{0,22}\b(?:STILL\s+)?VALID\b", u):
        return "{GreenTick} ENTRY STILL VALID"

    if ((re.search(r"\b(?:DELETE|CANCEL|REMOVE)\b.{0,28}\b(?:PENDING|ORDER|LIMIT|STOP)\b", u)
         and re.search(r"\b(?:ENTER|ENTRY|GET\s+IN)\b.{0,22}\b(?:NOW|MARKET)\b", u))
        or re.search(r"\bDELETE\s+N\s+ENTER\s+NOW\b", u)):
        return "{Warning} DELETE PENDING ORDER\n\nENTER MARKET NOW"

    short = len(compact) <= 120
    if short and re.search(r"^(?:LAYER|LAYER\s+NOW|ADD\s+(?:ANOTHER\s+)?ENTRY|ADD\s+(?:ANOTHER\s+)?POSITION|SCALE\s+IN|RE-?ENTER|ADD\s+ON)(?:\b|$)", compact):
        return "{Warning} LAYER ENTRY NOW"
    if re.search(r"\b(?:IF\s+YOURS?\s+(?:HAS(?:N'T| NOT)?|HASN'T)\s+CLOSED\s+)?CLOSE\s+(?:THE\s+)?(?:TRADE|POSITION)?\s*NOW\b", u) or re.search(r"\bCLOSE\s+NOW\b", u):
        return "{Warning} CLOSE TRADE NOW"
    if short and re.search(r"\b(?:I(?:'M| AM)\s+)?STILL\s+(?:IN\s+(?:THIS|IT)|HOLDING)|\bTRADE\s+STILL\s+ACTIVE\b", u):
        return "{GreenTick} TRADE STILL ACTIVE"
    if re.search(r"\b(?:CANCEL|DELETE|REMOVE)\b.{0,22}\b(?:PENDING\s+ORDER|ORDER|TRADE|SETUP|SIGNAL)\b", u) or re.search(r"\b(?:TRADE|SETUP|SIGNAL|ORDER)\s+(?:IS\s+)?(?:CANCELLED|CANCELED|INVALID|INVALIDATED)\b", u):
        if re.search(r"\b(?:PENDING|LIMIT|STOP\s+ORDER)\b", u):
            return "{RedCross} PENDING ORDER CANCELLED"
        return "{RedCross} TRADE CANCELLED"
    return None


def build_update(registry, template: str):
    return registry.render(template)


def _run_variant_self_test():
    signals = [
        ("Buy xauusd now\n\nTp 4400\nTp 4410\nTp 4450\n\nSl 4379", "NOW", "BUY", ["4400", "4410", "4450"]),
        ("SELL XAUUSD MARKET\nTP1: 4330\nTP2: 4315\nTP3: 4300\nSL: 4355", "NOW", "SELL", ["4330", "4315", "4300"]),
        ("Buy XAUUSD 4372-4369\nTP 4380\nTP 4390\nTP 4400\nSL 4356", "ZONE", "BUY", ["4380", "4390", "4400"]),
        ("Buy limit xauusd 4306\nTake Profit 4320\nTarget 4350\nTP 4400\nStop Loss 4290", "LIMIT", "BUY", ["4320", "4350", "4400"]),
        ("Sell stop GOLD 4360\nTP1 = 4350\nTP2 = 4330\nSL = 4372", "STOP", "SELL", ["4350", "4330"]),
        ("sell xauusd now 4367-4373\nT/P 4362\nT/P 4355\nT/P 4345\nS/L 4378", "ZONE", "SELL", ["4362", "4355", "4345"]),
    ]
    for text, kind, direction, expected_tps in signals:
        parsed = parse_xauusd_signal(text)
        if not parsed or parsed["kind"] != kind or parsed["direction"] != direction or parsed["tps"] != expected_tps:
            raise RuntimeError(f"signal self-test failed kind={kind} direction={direction} expected_tps={expected_tps} text={text!r} parsed={parsed!r}")

    updates = {
        "+ 130 PIPs": "{GreenTick} +130 PIPS",
        "TP1💥110 PIPs": "{GreenTick} TP1 HIT — +110 PIPS",
        "TP 2 hit": "{GreenTick} TP2 HIT",
        "Ran 250 PIPs": "{GreenTick} +250 PIPS RUNNING",
        "SL hit": "{RedCross} SL HIT",
        "Stopped out": "{RedCross} SL HIT",
        "Apologies family knocked us out and dropped 100 pips lol": "{RedCross} SL HIT — -100 PIPS",
        "Go breakeven pls": "{GreenTick} MOVE SL TO BREAKEVEN",
        "Secure partials and go breakeven.": "{GreenTick} SECURE PARTIAL PROFITS\n\nMOVE SL TO BREAKEVEN",
        "Entry still valid .": "{GreenTick} ENTRY STILL VALID",
        "layer now": "{Warning} LAYER ENTRY NOW",
        "Delete n enter now": "{Warning} DELETE PENDING ORDER\n\nENTER MARKET NOW",
        "If yours hasn’t closed close now": "{Warning} CLOSE TRADE NOW",
        "I’m still in this btw": "{GreenTick} TRADE STILL ACTIVE",
        "cancel trade": "{RedCross} TRADE CANCELLED",
        "delete pending order": "{RedCross} PENDING ORDER CANCELLED",
    }
    for text, expected in updates.items():
        got = classify_update(text)
        if got != expected:
            raise RuntimeError(f"update self-test failed text={text!r} expected={expected!r} got={got!r}")

    safe = (
        "wtf",
        "Everyone recovered we happy",
        "are we happy yet can I go off charts now",
        "20 pip sl",
        "keep tp few pips higher example like now tp is 4360 keep tp at 4360.5",
    )
    for text in safe:
        if classify_update(text) is not None:
            raise RuntimeError(f"fail-safe self-test failed general chat was classified: {text!r}")
    return len(signals), len(updates), len(safe)


async def install_imperium_vip_formatter(client, registry, logger=None):
    logger = logger or log
    if os.environ.get("IMPERIUM_VIP_FORMATTER_ENABLED", "1").strip() != "1":
        logger.info("[IMPERIUM VIP FORMATTER] disabled")
        return None

    signal_count, update_count, safe_count = _run_variant_self_test()
    logger.warning("[IMPERIUM VIP FORMATTER SELFTEST OK] signal_variants=%s update_variants=%s fail_safe_general_chat=%s", signal_count, update_count, safe_count)

    required = ["Boom", "GreenTick", "RedCross", "Warning"]
    if not _require_aliases(registry, required):
        missing = [x for x in required if not registry.get(x)]
        logger.warning("[IMPERIUM VIP FORMATTER] waiting; missing emoji aliases=%s", missing)
        return None

    chat_id, title = await resolve_imperium_vip_chat(client, logger=logger)
    if chat_id is None:
        return None

    processing = set()

    async def _process(message, source: str):
        msg_id = int(getattr(message, "id", 0) or 0)
        key = (chat_id, msg_id)
        if not msg_id or key in processing:
            return
        text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
        if not text:
            return

        up = _normalise(text).upper()
        if "NOT FINANCIAL ADVICE" in up and "XAUUSD" in up:
            return

        parsed = parse_xauusd_signal(text)
        update_template = None if parsed else classify_update(text)
        if not parsed and not update_template:
            return

        processing.add(key)
        try:
            if parsed:
                out_text, out_entities = build_signal(registry, parsed)
                await client.edit_message(chat_id, msg_id, out_text, formatting_entities=out_entities, parse_mode=None)
                logger.warning("[IMPERIUM VIP SIGNAL FORMATTED] source=%s msg=%s kind=%s direction=%s entry=%s/%s tps=%s sl=%s", source, msg_id, parsed["kind"], parsed["direction"], parsed.get("entry_a"), parsed.get("entry_b"), len(parsed["tps"]), parsed["sl"])
            else:
                out_text, out_entities = build_update(registry, update_template)
                if out_text.strip() == text.strip():
                    return
                await client.edit_message(chat_id, msg_id, out_text, formatting_entities=out_entities, parse_mode=None)
                logger.warning("[IMPERIUM VIP UPDATE FORMATTED] source=%s msg=%s before=%r after=%r", source, msg_id, _normalise(text)[:120], out_text[:120])
        except Exception as exc:
            logger.warning("[IMPERIUM VIP FORMAT FAILED] msg=%s %s: %s", msg_id, type(exc).__name__, exc)
        finally:
            processing.discard(key)

    async def _new_handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) == int(chat_id):
                await _process(event.message, "new")
        except Exception as exc:
            logger.exception("[IMPERIUM VIP NEW HANDLER FAILED] %s: %s", type(exc).__name__, exc)

    async def _edit_handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) == int(chat_id):
                await _process(event.message, "edit")
        except Exception as exc:
            logger.exception("[IMPERIUM VIP EDIT HANDLER FAILED] %s: %s", type(exc).__name__, exc)

    client.add_event_handler(_new_handler, events.NewMessage())
    client.add_event_handler(_edit_handler, events.MessageEdited())
    logger.warning("[IMPERIUM VIP FORMATTER ACTIVE] chat_id=%s title=%r formats=NOW,ZONE,LIMIT,STOP,TP,SL,BE,PARTIALS,LAYER,CLOSE,CANCEL,ACTIVE aliases=%s", chat_id, title, ",".join(required))
    return {"chat_id": chat_id, "title": title, "new_handler": _new_handler, "edit_handler": _edit_handler}

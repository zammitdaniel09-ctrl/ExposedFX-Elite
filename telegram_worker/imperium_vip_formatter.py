import logging
import os
import re
import unicodedata
from typing import Optional, Tuple, List, Dict, Any

from telethon import events
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji


log = logging.getLogger("imperium-vip-formatter")

PRICE = r"\d{1,7}(?:\.\d+)?"

DIRECTION_RE = re.compile(r"\b(BUY|BUYS|LONG|SELL|SELLS|SHORT)\b", re.IGNORECASE)
XAU_RE = re.compile(r"\b(XAUUSD|XAU/USD|XAU|GOLD)\b", re.IGNORECASE)
RANGE_RE = re.compile(rf"({PRICE})\s*(?:-|–|—|TO)\s*({PRICE})", re.IGNORECASE)

TP_LINE_RE = re.compile(
    rf"^[ \t]*(?:TP|T/P|TAKE[ \t]*PROFIT|TARGET)[ \t]*(?:#?[0-9]+)?[ \t]*[:=\-]?[ \t]*({PRICE}|OPEN)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
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
        if not norm:
            continue
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
    return "BUY" if token in {"BUY", "BUYS", "LONG"} else "SELL"


def _extract_tps(raw: str) -> List[str]:
    out = []
    for m in TP_LINE_RE.finditer(raw):
        value = m.group(1).upper() if m.group(1).upper() == "OPEN" else m.group(1)
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
    cleaned = re.sub(r"\b(NOW|MARKET|LIMIT|STOP|ENTRY|ENTER|AT|@|ZONE)\b", " ", cleaned, flags=re.IGNORECASE)
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
    if not direction:
        return None

    tps = _extract_tps(raw)
    sl = _extract_sl(raw)
    if not tps or not sl:
        return None

    trade_line = _first_trade_line(raw)
    if not trade_line:
        return None

    entry_a, entry_b = _extract_entry_from_trade_line(trade_line)
    line_up = trade_line.upper()

    if re.search(r"\bBUY\s+LIMIT\b|\bSELL\s+LIMIT\b", line_up):
        kind = "LIMIT"
    elif re.search(r"\bBUY\s+STOP\b|\bSELL\s+STOP\b", line_up):
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

    return {
        "direction": direction,
        "kind": kind,
        "entry_a": entry_a,
        "entry_b": entry_b,
        "tps": tps,
        "sl": sl,
    }


def _fmt_entry(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if not a:
        return None
    if not b or a == b:
        return a
    return f"{a} - {b}"


def _require_aliases(registry, aliases: List[str]) -> bool:
    return all(registry.get(alias) for alias in aliases)


def _add_bold(text: str, entities: list, phrase: str):
    char_pos = text.find(phrase)
    if char_pos >= 0:
        entities.append(MessageEntityBold(offset=_utf16_len(text[:char_pos]), length=_utf16_len(phrase)))


def build_signal(registry, parsed):
    direction = parsed["direction"]
    kind = parsed["kind"]

    if kind == "NOW":
        heading = f"XAUUSD {direction} NOW"
    elif kind == "ZONE":
        heading = f"XAUUSD {direction} ZONE"
    elif kind == "LIMIT":
        heading = f"XAUUSD {direction} LIMIT"
    else:
        heading = f"XAUUSD {direction} STOP"

    lines = [f"{{Boom}} {heading}", ""]

    entry = _fmt_entry(parsed.get("entry_a"), parsed.get("entry_b"))
    if entry:
        lines += [f"ENTRY = {entry}", ""]

    for idx, value in enumerate(parsed["tps"], 1):
        lines.append(f"TP{idx}{{GreenTick}} = {value}")

    lines += [
        "",
        f"SL{{RedCross}} = {parsed['sl']}",
        "",
        "{Warning} NOT FINANCIAL ADVICE {Warning}",
    ]

    text, entities = registry.render("\n".join(lines))
    _add_bold(text, entities, heading)
    entities.sort(key=lambda e: int(getattr(e, "offset", 0)))
    return text, entities


def _pip_number(raw: str) -> Optional[str]:
    patterns = [
        r"([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b",
        r"\b(?:DROPPED|LOST|DOWN)\s+([+-]?\s*\d+(?:\.\d+)?)\s*PIPS?\b",
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            return re.sub(r"\s+", "", m.group(1))
    return None


def classify_update(text: str) -> Optional[str]:
    raw = _normalise(text)
    if not raw or len(raw) > 500:
        return None

    u = raw.upper()
    compact = re.sub(r"\s+", " ", u).strip()

    # Do not touch newly-issued signals here.
    if parse_xauusd_signal(raw):
        return None

    # Stop loss / stopped-out variants. "knocked us out" is included because
    # Imperium uses this wording for an SL event.
    sl_hit = bool(re.search(
        r"\b(?:SL|S/L|STOP\s*LOSS|STOPLOSS)\b.{0,24}\b(?:HIT|HITTED|TOUCHED|TRIGGERED)\b|"
        r"\b(?:HIT|TOUCHED|TRIGGERED)\b.{0,24}\b(?:SL|STOP\s*LOSS|STOPLOSS)\b|"
        r"\bSTOPPED\s+OUT\b|\bKNOCKED\s+(?:US\s+)?OUT\b",
        compact,
        re.IGNORECASE,
    ))
    if sl_hit:
        pips = _pip_number(raw)
        if pips:
            value = pips.lstrip("+").lstrip("-")
            return f"{{RedCross}} SL HIT — -{value} PIPS"
        return "{RedCross} SL HIT"

    # TP results, including Imperium shorthand such as TP1💥110 PIPs.
    tp_num = None
    m_tp = re.search(r"\bTP\s*#?\s*(\d+)\b", u)
    if m_tp:
        tp_num = m_tp.group(1)
        has_tp_result = bool(re.search(r"\b(HIT|HITTED|TOUCHED|DONE|BANKED)\b", u)) or bool(re.search(r"\d+(?:\.\d+)?\s*PIPS?\b", u))
        if has_tp_result:
            pips = _pip_number(raw)
            if pips:
                value = pips.lstrip("+").lstrip("-")
                return f"{{GreenTick}} TP{tp_num} HIT — +{value} PIPS"
            return f"{{GreenTick}} TP{tp_num} HIT"

    # Explicit profit/result updates. Loss/spread discussion is blocked.
    if not re.search(r"\b(?:LOSS|LOST|DROPPED|SL|STOP\s*LOSS|SPREAD|KEEP\s+TP|EXAMPLE)\b", u):
        m = re.search(r"^\s*\+\s*(\d+(?:\.\d+)?)\s*PIPS?\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS"

        m = re.search(r"\bRAN\s+(\d+(?:\.\d+)?)\s*PIPS?\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS RUNNING"

        m = re.search(r"^\s*(\d+(?:\.\d+)?)\s*PIPS?\s+(?:RUNNING|SECURED|BANKED|PROFIT)\b", raw, re.IGNORECASE)
        if m:
            return f"{{GreenTick}} +{m.group(1)} PIPS"

    # Combined management command first.
    if re.search(r"\b(?:SECURE|TAKE|CLOSE)\b.{0,24}\bPARTIAL", u) and re.search(r"\b(?:BREAK\s*-?\s*EVEN|BREAKEVEN|BE)\b", u):
        return "{GreenTick} SECURE PARTIAL PROFITS\n\nMOVE SL TO BREAKEVEN"

    if re.search(r"\b(?:BREAK\s*-?\s*EVEN|BREAKEVEN)\b", u) or re.search(r"\b(?:MOVE|PUT|SET)\s+(?:SL|STOP(?:\s*LOSS)?)\s+(?:TO\s+)?(?:BE|ENTRY)\b", u):
        return "{GreenTick} MOVE SL TO BREAKEVEN"

    if re.search(r"\b(?:SECURE|TAKE|CLOSE)\b.{0,24}\bPARTIAL", u):
        return "{GreenTick} SECURE PARTIAL PROFITS"

    if re.search(r"\b(?:ENTRY|SIGNAL|SETUP)\b.{0,18}\b(?:STILL\s+)?VALID\b", u):
        return "{GreenTick} ENTRY STILL VALID"

    # Delete pending and enter market now must win over generic cancellation.
    if (
        re.search(r"\b(?:DELETE|CANCEL|REMOVE)\b.{0,24}\b(?:PENDING|ORDER|LIMIT|STOP)\b", u)
        and re.search(r"\b(?:ENTER|ENTRY|GET\s+IN)\b.{0,18}\b(?:NOW|MARKET)\b", u)
    ) or re.search(r"\bDELETE\s+N\s+ENTER\s+NOW\b", u):
        return "{Warning} DELETE PENDING ORDER\n\nENTER MARKET NOW"

    # Imperium often sends one-word/very short management updates.
    short = len(compact) <= 100
    if short and re.search(r"^(?:LAYER|LAYER\s+NOW|ADD\s+(?:ANOTHER\s+)?ENTRY|ADD\s+POSITION|SCALE\s+IN|RE-?ENTER)(?:\b|$)", compact):
        return "{Warning} LAYER ENTRY NOW"

    if re.search(r"\b(?:IF\s+YOURS?\s+(?:HAS(?:N'T| NOT)?|HASN'T)\s+CLOSED\s+)?CLOSE\s+(?:THE\s+)?(?:TRADE|POSITION)?\s*NOW\b", u) or re.search(r"\bCLOSE\s+NOW\b", u):
        return "{Warning} CLOSE TRADE NOW"

    if short and re.search(r"\b(?:I(?:'M| AM)\s+)?STILL\s+(?:IN\s+(?:THIS|IT)|HOLDING)|\bTRADE\s+STILL\s+ACTIVE\b", u):
        return "{GreenTick} TRADE STILL ACTIVE"

    if re.search(r"\b(?:CANCEL|DELETE|REMOVE)\b.{0,18}\b(?:PENDING\s+ORDER|ORDER|TRADE|SETUP|SIGNAL)\b", u) or re.search(r"\b(?:TRADE|SETUP|SIGNAL)\s+(?:IS\s+)?(?:CANCELLED|CANCELED|INVALID|INVALIDATED)\b", u):
        if re.search(r"\b(?:PENDING|LIMIT|STOP\s+ORDER)\b", u):
            return "{RedCross} PENDING ORDER CANCELLED"
        return "{RedCross} TRADE CANCELLED"

    return None


def _has_custom_emoji(message, document_id: int, at_start: bool = False) -> bool:
    for entity in (getattr(message, "entities", None) or []):
        if not isinstance(entity, MessageEntityCustomEmoji):
            continue
        if int(getattr(entity, "document_id", 0) or 0) != int(document_id):
            continue
        if at_start and int(getattr(entity, "offset", -1)) != 0:
            continue
        return True
    return False


def build_update(registry, template: str):
    return registry.render(template)


async def install_imperium_vip_formatter(client, registry, logger=None):
    logger = logger or log

    if os.environ.get("IMPERIUM_VIP_FORMATTER_ENABLED", "1").strip() != "1":
        logger.info("[IMPERIUM VIP FORMATTER] disabled")
        return None

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

        parsed = parse_xauusd_signal(text)
        update_template = None if parsed else classify_update(text)
        if not parsed and not update_template:
            return

        processing.add(key)
        try:
            if parsed:
                out_text, out_entities = build_signal(registry, parsed)
                await client.edit_message(chat_id, msg_id, out_text, formatting_entities=out_entities, parse_mode=None)
                logger.warning(
                    "[IMPERIUM VIP SIGNAL FORMATTED] source=%s msg=%s kind=%s direction=%s entry=%s/%s tps=%s sl=%s",
                    source,
                    msg_id,
                    parsed["kind"],
                    parsed["direction"],
                    parsed.get("entry_a"),
                    parsed.get("entry_b"),
                    len(parsed["tps"]),
                    parsed["sl"],
                )
            else:
                out_text, out_entities = build_update(registry, update_template)
                await client.edit_message(chat_id, msg_id, out_text, formatting_entities=out_entities, parse_mode=None)
                logger.warning(
                    "[IMPERIUM VIP UPDATE FORMATTED] source=%s msg=%s before=%r after=%r",
                    source,
                    msg_id,
                    _normalise(text)[:120],
                    out_text[:120],
                )
        except Exception as exc:
            logger.warning(
                "[IMPERIUM VIP FORMAT FAILED] msg=%s %s: %s",
                msg_id,
                type(exc).__name__,
                exc,
            )
        finally:
            processing.discard(key)

    async def _new_handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) != int(chat_id):
                return
            await _process(event.message, "new")
        except Exception as exc:
            logger.exception("[IMPERIUM VIP NEW HANDLER FAILED] %s: %s", type(exc).__name__, exc)

    async def _edit_handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) != int(chat_id):
                return
            await _process(event.message, "edit")
        except Exception as exc:
            logger.exception("[IMPERIUM VIP EDIT HANDLER FAILED] %s: %s", type(exc).__name__, exc)

    client.add_event_handler(_new_handler, events.NewMessage())
    client.add_event_handler(_edit_handler, events.MessageEdited())

    logger.warning(
        "[IMPERIUM VIP FORMATTER ACTIVE] chat_id=%s title=%r formats=NOW,ZONE,LIMIT,STOP,TP,SL,BE,PARTIALS,LAYER,CLOSE,CANCEL,ACTIVE aliases=%s",
        chat_id,
        title,
        ",".join(required),
    )

    return {
        "chat_id": chat_id,
        "title": title,
        "new_handler": _new_handler,
        "edit_handler": _edit_handler,
    }

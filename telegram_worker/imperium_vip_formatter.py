import logging
import os
import re
import unicodedata
from typing import Optional, Tuple, List

from telethon import events
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji


log = logging.getLogger("imperium-vip-formatter")

PRICE = r"\d{1,7}(?:\.\d+)?"

# Only the exact style requested for the Imperium VIP is reformatted here.
# Range/limit signals are intentionally left untouched until a separate format
# is explicitly defined for them.
NOW_SIGNAL_RE = re.compile(
    r"\b(BUY|SELL)\s+(?:XAUUSD|XAU/USD|XAU)\s+NOW\b",
    re.IGNORECASE,
)
TP_LINE_RE = re.compile(
    rf"^[ \t]*TP[ \t]*(?:#?[0-9]+)?[ \t]*[:=\-]?[ \t]*({PRICE})[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
SL_LINE_RE = re.compile(
    rf"^[ \t]*(?:SL|S/L|STOP[ \t]*LOSS|STOPLOSS)[ \t]*[:=\-]?[ \t]*({PRICE})(?:[ \t]*\([^\n]*\))?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Short, explicit trade-result/update forms. This deliberately avoids matching
# general discussion such as "keep TP a few pips higher" or "20 pip SL".
PIP_UPDATE_PATTERNS = [
    re.compile(r"^\s*\+\s*\d+(?:\.\d+)?\s*PIPS?\b.*$", re.IGNORECASE),
    re.compile(r"^\s*TP\s*\d+.*?\b\d+(?:\.\d+)?\s*PIPS?\b.*$", re.IGNORECASE),
    re.compile(r"^\s*RAN\s+\d+(?:\.\d+)?\s*PIPS?\b.*$", re.IGNORECASE),
    re.compile(r"^\s*\d+(?:\.\d+)?\s*PIPS?\s+(?:RUNNING|SECURED|BANKED|PROFIT)\b.*$", re.IGNORECASE),
]
PIP_UPDATE_BLOCK_RE = re.compile(
    r"\b(?:SL|STOP\s*LOSS|DROPPED|LOSS|LOST|SPREAD|EXAMPLE|KEEP\s+TP)\b",
    re.IGNORECASE,
)


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


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

    # Exact normalised title wins. If duplicate matching chats exist, use the
    # first Telegram dialog and log every candidate so an env override can be
    # supplied later without changing code.
    chosen = matches[0]
    if len(matches) > 1:
        logger.warning(
            "[IMPERIUM VIP FORMATTER] multiple title matches=%s choosing=%s",
            matches,
            chosen,
        )

    return chosen


def parse_xauusd_now_signal(text: str):
    raw = (text or "").replace("\u200b", "").strip()
    if not raw:
        return None

    m = NOW_SIGNAL_RE.search(raw)
    if not m:
        return None

    # Already in our target house style: never re-edit it.
    upper = raw.upper()
    if "NOT FINANCIAL ADVICE" in upper and "ENTRIES:" in upper:
        return None

    direction = m.group(1).upper()
    tps = [x.group(1) for x in TP_LINE_RE.finditer(raw)]
    sl_match = SL_LINE_RE.search(raw)

    if not tps or not sl_match:
        return None

    return {
        "direction": direction,
        "tps": tps,
        "sl": sl_match.group(1),
    }


def looks_like_pip_update(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or len(raw) > 120:
        return False
    if "\n" in raw and len(raw.splitlines()) > 2:
        return False
    if NOW_SIGNAL_RE.search(raw):
        return False
    if PIP_UPDATE_BLOCK_RE.search(raw):
        return False
    return any(pat.match(raw) for pat in PIP_UPDATE_PATTERNS)


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


def _require_aliases(registry, aliases: List[str]) -> bool:
    return all(registry.get(alias) for alias in aliases)


def build_now_signal(registry, parsed):
    direction = parsed["direction"]
    tp_lines = [f"TP{idx}{{GreenTick}} = {value}" for idx, value in enumerate(parsed["tps"], 1)]

    template = (
        f"{{Boom}} XAUUSD {direction} NOW\n\n"
        "Entries:\n"
        + "\n".join(tp_lines)
        + f"\n\nSL{{RedCross}} = {parsed['sl']}\n\n"
        "{Warning}NOT FINANCIAL ADVICE{Warning}"
    )

    text, entities = registry.render(template)

    heading = f"XAUUSD {direction} NOW"
    char_pos = text.find(heading)
    if char_pos >= 0:
        entities.append(
            MessageEntityBold(
                offset=_utf16_len(text[:char_pos]),
                length=_utf16_len(heading),
            )
        )

    entities.sort(key=lambda e: int(getattr(e, "offset", 0)))
    return text, entities


def build_pip_update(registry, original_text: str):
    template = "{GreenTick} " + (original_text or "").strip()
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

    green = registry.get("GreenTick") or {}
    green_doc_id = int(green.get("document_id") or 0) if green.get("type") == "custom" else 0

    processing = set()

    async def _process(message, source: str):
        msg_id = int(getattr(message, "id", 0) or 0)
        key = (chat_id, msg_id)
        if not msg_id or key in processing:
            return

        text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
        if not text:
            return

        parsed = parse_xauusd_now_signal(text)
        if parsed:
            processing.add(key)
            try:
                out_text, out_entities = build_now_signal(registry, parsed)
                await client.edit_message(
                    chat_id,
                    msg_id,
                    out_text,
                    formatting_entities=out_entities,
                    parse_mode=None,
                )
                logger.warning(
                    "[IMPERIUM VIP SIGNAL FORMATTED] source=%s msg=%s direction=%s tps=%s sl=%s",
                    source,
                    msg_id,
                    parsed["direction"],
                    len(parsed["tps"]),
                    parsed["sl"],
                )
            except Exception as exc:
                logger.warning(
                    "[IMPERIUM VIP SIGNAL FORMAT FAILED] msg=%s %s: %s",
                    msg_id,
                    type(exc).__name__,
                    exc,
                )
            finally:
                processing.discard(key)
            return

        if looks_like_pip_update(text):
            if green_doc_id and _has_custom_emoji(message, green_doc_id, at_start=True):
                return

            processing.add(key)
            try:
                out_text, out_entities = build_pip_update(registry, text)
                await client.edit_message(
                    chat_id,
                    msg_id,
                    out_text,
                    formatting_entities=out_entities,
                    parse_mode=None,
                )
                logger.warning(
                    "[IMPERIUM VIP PIP UPDATE FORMATTED] source=%s msg=%s text=%r",
                    source,
                    msg_id,
                    text[:100],
                )
            except Exception as exc:
                logger.warning(
                    "[IMPERIUM VIP PIP UPDATE FORMAT FAILED] msg=%s %s: %s",
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
            logger.exception(
                "[IMPERIUM VIP NEW HANDLER FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    async def _edit_handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) != int(chat_id):
                return
            await _process(event.message, "edit")
        except Exception as exc:
            logger.exception(
                "[IMPERIUM VIP EDIT HANDLER FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    client.add_event_handler(_new_handler, events.NewMessage())
    client.add_event_handler(_edit_handler, events.MessageEdited())

    logger.warning(
        "[IMPERIUM VIP FORMATTER ACTIVE] chat_id=%s title=%r formats=XAUUSD_NOW,PIP_UPDATES aliases=%s",
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

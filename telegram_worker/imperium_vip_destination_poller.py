import asyncio
import hashlib
import logging
import os
import re

from telethon.tl.types import MessageEntityCustomEmoji

from telegram_worker.imperium_vip_formatter import (
    build_signal,
    build_update,
    parse_xauusd_signal,
)
from telegram_worker.imperium_vip_trade_updates import (
    classify_trade_update,
    run_update_self_test,
)

log = logging.getLogger("imperium-vip-destination-poller")

SOURCE_CHAT = int(os.environ.get("IMPERIUM_AI_SOURCE_CHAT", "-1004367822325"))
SOURCE_TOPIC = int(os.environ.get("IMPERIUM_AI_SOURCE_TOPIC", "508"))
DEST_CHAT = int(os.environ.get("IMPERIUM_AI_DEST_CHAT", "-1003726286301"))
DEST_TOPIC = int(os.environ.get("IMPERIUM_AI_DEST_TOPIC", "7"))
POLL_SECONDS = max(0.75, float(os.environ.get("IMPERIUM_AI_DEST_POLL_SECONDS", "1.0")))
POLL_LIMIT = max(10, int(os.environ.get("IMPERIUM_AI_DEST_POLL_LIMIT", "30")))


HOUSE_UPDATE_RE = re.compile(
    r"^(?:"
    r"TP\d+ HIT|ALL TARGETS HIT|SL HIT|BREAKEVEN HIT|"
    r"MOVE SL TO BREAKEVEN|SECURE PARTIAL PROFITS|"
    r"ENTRY STILL VALID|ENTRY ACTIVATED|LAYER ENTRY NOW|RE-ENTER TRADE NOW|"
    r"CLOSE TRADE NOW|TRADE STILL ACTIVE|LET REMAINDER RUN|"
    r"TRADE IS RISK-FREE|LOCK IN PROFIT|REDUCE RISK|"
    r"TRADE CANCELLED|PENDING ORDER CANCELLED|DO NOT ENTER|"
    r"ENTRY MISSED|WAIT — DO NOT ENTER YET|DELETE PENDING ORDER|"
    r"ENTER MARKET NOW|MOVE SL =|REMOVE STOP LOSS|[+-]?\d+(?:\.\d+)? PIPS"
    r")",
    re.IGNORECASE,
)


def _text(message):
    return (
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    )


def _topic_id(message):
    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass

    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_top_id", "top_msg_id", "reply_to_msg_id"):
            value = getattr(reply, attr, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass

    direct = getattr(message, "reply_to_msg_id", None)
    if direct and getattr(message, "is_topic_message", False):
        try:
            return int(direct)
        except Exception:
            pass

    return None


def _signature(message):
    text = _text(message)
    edit_date = getattr(message, "edit_date", None)
    payload = f"{getattr(message, 'id', 0)}|{edit_date}|{text}"
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


def _our_custom_ids(registry):
    out = set()
    for alias in ("Boom", "GreenTick", "RedCross", "Warning"):
        entry = registry.get(alias) or {}
        if entry.get("type") == "custom" and entry.get("document_id"):
            try:
                out.add(int(entry["document_id"]))
            except Exception:
                pass
    return out


def _already_house_formatted(message, registry):
    """Only skip messages that clearly match our own output.

    A source provider can coincidentally use one of the same premium emoji
    documents. Custom emoji presence alone is therefore not sufficient to skip
    formatting; the text must also look like an Imperium house-format output.
    """
    text = _text(message).strip()
    upper = text.upper()

    if "NOT FINANCIAL ADVICE" in upper and (
        "XAUUSD BUY" in upper or "XAUUSD SELL" in upper
    ):
        return True

    stripped = re.sub(r"^[^A-Z0-9+\-]+", "", upper).strip()
    if not HOUSE_UPDATE_RE.search(stripped):
        return False

    custom_ids = _our_custom_ids(registry)
    if not custom_ids:
        return False

    for entity in (getattr(message, "entities", None) or []):
        if isinstance(entity, MessageEntityCustomEmoji):
            try:
                if int(getattr(entity, "document_id", 0) or 0) in custom_ids:
                    return True
            except Exception:
                pass
    return False


async def _topic_messages(client):
    try:
        messages = await client.get_messages(
            DEST_CHAT,
            limit=POLL_LIMIT,
            reply_to=DEST_TOPIC,
        )
        return list(messages or [])
    except Exception as exc:
        log.warning(
            "[IMPERIUM VIP DEST POLL GETREPLIES FAILED] %s: %s; using chat fallback",
            type(exc).__name__,
            exc,
        )

    messages = await client.get_messages(DEST_CHAT, limit=max(POLL_LIMIT * 3, 60))
    return [m for m in (messages or []) if _topic_id(m) == DEST_TOPIC]


async def _format_destination_message(client, registry, message, logger=None):
    logger = logger or log
    if _already_house_formatted(message, registry):
        return False

    raw = _text(message).strip()
    if not raw:
        return False

    parsed = parse_xauusd_signal(raw)
    update_template = None if parsed else classify_trade_update(raw)
    if not parsed and not update_template:
        return False

    msg_id = int(getattr(message, "id", 0) or 0)
    if not msg_id:
        return False

    if parsed:
        out_text, out_entities = build_signal(registry, parsed)
        await client.edit_message(
            DEST_CHAT,
            msg_id,
            out_text,
            formatting_entities=out_entities,
            parse_mode=None,
        )
        logger.warning(
            "[IMPERIUM VIP ROUTE SIGNAL FORMATTED] source=%s_%s dest=%s_%s msg=%s kind=%s direction=%s tps=%s sl=%s",
            SOURCE_CHAT,
            SOURCE_TOPIC,
            DEST_CHAT,
            DEST_TOPIC,
            msg_id,
            parsed.get("kind"),
            parsed.get("direction"),
            len(parsed.get("tps") or []),
            parsed.get("sl"),
        )
        return True

    out_text, out_entities = build_update(registry, update_template)
    if out_text.strip() == raw.strip():
        return False

    await client.edit_message(
        DEST_CHAT,
        msg_id,
        out_text,
        formatting_entities=out_entities,
        parse_mode=None,
    )
    logger.warning(
        "[IMPERIUM VIP ROUTE UPDATE FORMATTED] source=%s_%s dest=%s_%s msg=%s before=%r after=%r",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
        msg_id,
        raw[:140],
        out_text[:180],
    )
    return True


async def install_imperium_vip_destination_poller(client, registry, logger=None):
    logger = logger or log

    if os.environ.get("IMPERIUM_VIP_DEST_POLLER_ENABLED", "1").strip() != "1":
        logger.info("[IMPERIUM VIP DEST POLLER] disabled")
        return None

    required = ["Boom", "GreenTick", "RedCross", "Warning"]
    missing = [alias for alias in required if not registry.get(alias)]
    if missing:
        raise RuntimeError(f"VIP destination poller missing emoji aliases: {missing}")

    update_count, safe_count = run_update_self_test()
    logger.warning(
        "[IMPERIUM VIP UPDATE ENGINE SELFTEST OK] update_variants=%s fail_safe_general_chat=%s",
        update_count,
        safe_count,
    )

    baseline = await _topic_messages(client)
    seen = {int(m.id): _signature(m) for m in baseline if getattr(m, "id", None)}
    baseline_id = max(seen.keys(), default=0)

    logger.warning(
        "[IMPERIUM VIP DEST POLLER READY] source=%s_%s dest=%s_%s baseline=%s interval=%.2fs limit=%s NO_HISTORY=True SINGLE_WRITER=True",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
        baseline_id,
        POLL_SECONDS,
        POLL_LIMIT,
    )

    async def loop():
        while True:
            try:
                messages = await _topic_messages(client)
                current_ids = set()
                for message in sorted(
                    messages,
                    key=lambda m: int(getattr(m, "id", 0) or 0),
                ):
                    msg_id = int(getattr(message, "id", 0) or 0)
                    if not msg_id:
                        continue

                    current_ids.add(msg_id)
                    sig = _signature(message)
                    previous = seen.get(msg_id)
                    if previous == sig:
                        continue

                    # Existing history is snapshotted on install. Only new or
                    # subsequently edited messages are eligible.
                    seen[msg_id] = sig
                    if msg_id <= baseline_id and previous is None:
                        continue

                    try:
                        changed = await _format_destination_message(
                            client,
                            registry,
                            message,
                            logger=logger,
                        )
                        if changed:
                            # Refresh signature after our own edit so the next
                            # poll cannot immediately process it again.
                            refreshed = await client.get_messages(
                                DEST_CHAT,
                                ids=msg_id,
                            )
                            if refreshed:
                                seen[msg_id] = _signature(refreshed)
                    except Exception as exc:
                        logger.exception(
                            "[IMPERIUM VIP DEST FORMAT FAILED] msg=%s %s: %s",
                            msg_id,
                            type(exc).__name__,
                            exc,
                        )

                # Keep memory bounded to the recent topic window.
                if len(seen) > 500:
                    floor = sorted(seen)[-300]
                    for old_id in list(seen):
                        if old_id < floor and old_id not in current_ids:
                            seen.pop(old_id, None)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[IMPERIUM VIP DEST POLLER RETRY] %s: %s",
                    type(exc).__name__,
                    exc,
                )

            await asyncio.sleep(POLL_SECONDS)

    task = asyncio.create_task(loop())
    return {
        "task": task,
        "source_chat": SOURCE_CHAT,
        "source_topic": SOURCE_TOPIC,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "baseline_id": baseline_id,
        "single_writer": True,
    }

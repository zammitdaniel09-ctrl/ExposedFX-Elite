import copy
import logging
from typing import Any, Dict, Optional, Tuple

from telegram_worker.imperium_vip_formatter import (
    build_signal,
    build_update,
    parse_xauusd_signal,
)
from telegram_worker.imperium_vip_trade_updates import (
    classify_trade_update,
    run_update_self_test,
)


log = logging.getLogger("imperium-vip-inline-hook")

SOURCE_CHAT = -1004367822325
SOURCE_TOPIC = 508
DEST_CHAT = -1003726286301
DEST_TOPIC = 7
REQUIRED_ALIASES = ("Boom", "GreenTick", "RedCross", "Warning")


def _int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def is_imperium_vip_route(route: Dict[str, Any]) -> bool:
    """Return True only for the exact 508 -> VIP topic 7 route."""
    try:
        return (
            _int(route.get("source_chat")) == SOURCE_CHAT
            and _int(route.get("source_topic")) == SOURCE_TOPIC
            and _int(route.get("dest_chat")) == DEST_CHAT
            and _int(route.get("dest_topic")) == DEST_TOPIC
        )
    except Exception:
        return False


def _registry_ready(registry) -> bool:
    if registry is None:
        return False
    try:
        return all(registry.get(alias) for alias in REQUIRED_ALIASES)
    except Exception:
        return False


def _already_house_formatted(text: str) -> bool:
    upper = (text or "").upper()
    if "NOT FINANCIAL ADVICE" in upper and "XAUUSD" in upper:
        return True
    if "BREAKEVEN HIT — TRADE CLOSED RISK-FREE" in upper:
        return True
    return False


def format_imperium_vip_text(
    text: str,
    original_entities,
    registry,
) -> Tuple[str, Any, Optional[str]]:
    """Format a signal/update before it is sent to VIP topic 7.

    Returns (text, entities, kind). kind is None when the source should pass
    through unchanged. No prices, pips, percentages, direction or trade state
    are invented: the existing strict signal parser/update classifier owns all
    interpretation.
    """
    raw = (text or "").strip()
    if not raw or not _registry_ready(registry) or _already_house_formatted(raw):
        return text, original_entities, None

    parsed = parse_xauusd_signal(raw)
    if parsed:
        out_text, out_entities = build_signal(registry, parsed)
        return out_text, out_entities, "signal"

    update_template = classify_trade_update(raw)
    if update_template:
        out_text, out_entities = build_update(registry, update_template)
        return out_text, out_entities, "update"

    return text, original_entities, None


def _clone_message_with_format(message, registry):
    text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
    entities = getattr(message, "entities", None)
    out_text, out_entities, kind = format_imperium_vip_text(
        text,
        entities,
        registry,
    )
    if not kind:
        return message, None

    cloned = copy.copy(message)
    cloned.message = out_text
    cloned.entities = out_entities
    return cloned, kind


def _run_inline_self_test():
    update_count, safe_count = run_update_self_test()

    probe = parse_xauusd_signal(
        "Buy xauusd now\n\nTp 4324\nTp 4337\nTp 4350\nTp open\n\nSl 4308"
    )
    if not probe:
        raise RuntimeError("inline signal self-test failed: parser returned None")
    if probe.get("direction") != "BUY" or probe.get("kind") != "NOW":
        raise RuntimeError(f"inline signal self-test failed: {probe!r}")
    if probe.get("tps") != ["4324", "4337", "4350", "OPEN"]:
        raise RuntimeError(f"inline TP self-test failed: {probe!r}")
    if probe.get("sl") != "4308":
        raise RuntimeError(f"inline SL self-test failed: {probe!r}")

    return 1, update_count, safe_count


def install_imperium_vip_inline_hook(main_module, registry, logger=None):
    """Patch the active worker's copy/edit path for the exact VIP route.

    Formatting happens synchronously before the route's message is sent. This
    removes the timing dependency on same-client destination events and polling.
    """
    logger = logger or log

    if main_module is None:
        raise RuntimeError("main worker module unavailable")
    if not _registry_ready(registry):
        raise RuntimeError("emoji registry is not ready")

    existing = getattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", None)
    if existing:
        return existing

    signal_count, update_count, safe_count = _run_inline_self_test()
    logger.warning(
        "[IMPERIUM VIP INLINE SELFTEST OK] signal_variants=%s update_variants=%s fail_safe_general_chat=%s",
        signal_count,
        update_count,
        safe_count,
    )

    original_copy_one = getattr(main_module, "copy_one", None)
    original_copy_album = getattr(main_module, "copy_album", None)
    original_edit = getattr(main_module, "edit_existing_destination_in_place", None)

    if not callable(original_copy_one):
        raise RuntimeError("worker copy_one is unavailable")
    if not callable(original_edit):
        raise RuntimeError("worker edit_existing_destination_in_place is unavailable")

    async def copy_one_wrapper(message, route, edited=False, ensure_reply=True):
        if not is_imperium_vip_route(route):
            return await original_copy_one(
                message,
                route,
                edited=edited,
                ensure_reply=ensure_reply,
            )

        # Preserve the worker's username-mention safety semantics. Never clean
        # the text first and accidentally hide a mention from the original guard.
        mention_guard = getattr(main_module, "has_username_mention", None)
        if callable(mention_guard):
            try:
                if mention_guard(message):
                    return await original_copy_one(
                        message,
                        route,
                        edited=edited,
                        ensure_reply=ensure_reply,
                    )
            except Exception:
                pass

        formatted_message, kind = _clone_message_with_format(message, registry)
        if kind:
            logger.warning(
                "[IMPERIUM VIP INLINE PREPARED] kind=%s source_msg=%s route=%s",
                kind,
                getattr(message, "id", None),
                route.get("name"),
            )

        sent = await original_copy_one(
            formatted_message,
            route,
            edited=edited,
            ensure_reply=ensure_reply,
        )

        if kind and sent:
            sent_id = getattr(sent, "id", None)
            logger.warning(
                "[IMPERIUM VIP INLINE FORWARDED] kind=%s source_msg=%s dest_msg=%s source=%s_%s dest=%s_%s",
                kind,
                getattr(message, "id", None),
                sent_id,
                SOURCE_CHAT,
                SOURCE_TOPIC,
                DEST_CHAT,
                DEST_TOPIC,
            )

        return sent

    async def edit_wrapper(message, route, cascade_depth=0, visited=None):
        if not is_imperium_vip_route(route):
            return await original_edit(
                message,
                route,
                cascade_depth=cascade_depth,
                visited=visited,
            )

        formatted_message, kind = _clone_message_with_format(message, registry)
        if kind:
            logger.warning(
                "[IMPERIUM VIP INLINE EDIT PREPARED] kind=%s source_msg=%s",
                kind,
                getattr(message, "id", None),
            )

        return await original_edit(
            formatted_message,
            route,
            cascade_depth=cascade_depth,
            visited=visited,
        )

    async def copy_album_wrapper(messages, route):
        if not is_imperium_vip_route(route) or not callable(original_copy_album):
            if callable(original_copy_album):
                return await original_copy_album(messages, route)
            raise RuntimeError("worker copy_album is unavailable")

        cloned_messages = list(messages or [])
        formatted_kind = None
        for index, message in enumerate(cloned_messages):
            text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
            if not text:
                continue
            cloned, kind = _clone_message_with_format(message, registry)
            if kind:
                cloned_messages[index] = cloned
                formatted_kind = kind
                logger.warning(
                    "[IMPERIUM VIP INLINE ALBUM PREPARED] kind=%s source_msg=%s",
                    kind,
                    getattr(message, "id", None),
                )
            break

        sent = await original_copy_album(cloned_messages, route)
        if formatted_kind and sent:
            logger.warning(
                "[IMPERIUM VIP INLINE ALBUM FORWARDED] kind=%s source=%s_%s dest=%s_%s",
                formatted_kind,
                SOURCE_CHAT,
                SOURCE_TOPIC,
                DEST_CHAT,
                DEST_TOPIC,
            )
        return sent

    main_module.copy_one = copy_one_wrapper
    main_module.edit_existing_destination_in_place = edit_wrapper
    if callable(original_copy_album):
        main_module.copy_album = copy_album_wrapper

    state = {
        "source_chat": SOURCE_CHAT,
        "source_topic": SOURCE_TOPIC,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "copy_one": True,
        "copy_album": callable(original_copy_album),
        "edits": True,
        "primary": "inline_forward_path",
    }
    setattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", state)

    logger.warning(
        "[IMPERIUM VIP INLINE HOOK ACTIVE] source=%s_%s dest=%s_%s signals=True updates=True edits=True albums=%s PRIMARY=True",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
        bool(callable(original_copy_album)),
    )

    return state

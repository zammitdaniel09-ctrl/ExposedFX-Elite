import copy
import logging
from typing import Any, Dict, Optional, Tuple

from telegram_worker.imperium_vip_formatter import (
    build_signal,
    build_update,
    parse_xauusd_signal,
)
from telegram_worker.imperium_vip_trade_update_hardening import (
    classify_trade_update,
    run_update_self_test,
)
from telegram_worker.reply_hardening import (
    ensure_reply_mapping,
    install_reply_hardening,
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
    through unchanged. Prices, pips, percentages, direction and trade state are
    never calculated or invented.
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

    signal_cases = (
        (
            "Buy xauusd now\n\nTp 4324\nTp 4337\nTp 4350\nTp open\n\nSl 4308",
            "BUY",
            "NOW",
            ["4324", "4337", "4350", "OPEN"],
            "4308",
        ),
        (
            "Sell xauusd now 4327-4332\n\nTp 4320\nTp 4310\nTp 4280\n\nSl 4335",
            "SELL",
            "ZONE",
            ["4320", "4310", "4280"],
            "4335",
        ),
        (
            "Buy xauusd now 4305-4299\n\nTp 4315\nTp 4330\nTp 4370\nTp open\n\nSl 4290",
            "BUY",
            "ZONE",
            ["4315", "4330", "4370", "OPEN"],
            "4290",
        ),
    )

    for source, direction, kind, tps, sl in signal_cases:
        probe = parse_xauusd_signal(source)
        if not probe:
            raise RuntimeError(f"inline signal self-test failed: parser returned None for {source!r}")
        if probe.get("direction") != direction or probe.get("kind") != kind:
            raise RuntimeError(f"inline signal self-test failed: {probe!r}")
        if probe.get("tps") != tps or probe.get("sl") != sl:
            raise RuntimeError(f"inline signal values self-test failed: {probe!r}")

    return len(signal_cases), update_count, safe_count


def install_imperium_vip_inline_hook(main_module, registry, logger=None):
    """Patch the active worker's exact 508 -> VIP7 send path.

    Formatting and reply recovery happen before the destination send. This
    avoids same-client destination-event races and preserves source reply chains
    even when another forwarding account created the replied-to parent.
    """
    logger = logger or log

    if main_module is None:
        raise RuntimeError("main worker module unavailable")
    if not _registry_ready(registry):
        raise RuntimeError("emoji registry is not ready")

    existing = getattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", None)
    if existing:
        return existing

    # Patch the generic reply-ID/map functions first. This fixes direct-vs-header
    # Telethon reply metadata and list-valued message_map entries globally.
    install_reply_hardening(main_module, logger=logger)

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

        if ensure_reply:
            await ensure_reply_mapping(
                main_module,
                message,
                route,
                registry=registry,
                logger=logger,
            )

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
            logger.warning(
                "[IMPERIUM VIP INLINE FORWARDED] kind=%s source_msg=%s dest_msg=%s source=%s_%s dest=%s_%s",
                kind,
                getattr(message, "id", None),
                getattr(sent, "id", None),
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
        if cloned_messages:
            await ensure_reply_mapping(
                main_module,
                cloned_messages[0],
                route,
                registry=registry,
                logger=logger,
            )

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
        "replies": True,
        "cross_account_reply_recovery": True,
        "primary": "inline_forward_path",
    }
    setattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", state)

    logger.warning(
        "[IMPERIUM VIP INLINE HOOK ACTIVE] source=%s_%s dest=%s_%s signals=True updates=True replies=True cross_account_replies=True edits=True albums=%s PRIMARY=True",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
        bool(callable(original_copy_album)),
    )

    return state

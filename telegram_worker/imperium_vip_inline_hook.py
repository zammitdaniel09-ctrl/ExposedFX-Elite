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
    reply_source_ids,
)


log = logging.getLogger("imperium-vip-inline-hook")

SOURCE_CHAT = -1004367822325
SOURCE_TOPIC = 508
DEST_CHAT = -1003726286301
DEST_TOPIC = 7
REQUIRED_ALIASES = ("Boom", "GreenTick", "RedCross", "Warning")
MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS = 24 * 60 * 60


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
    out_text, out_entities, kind = format_imperium_vip_text(text, entities, registry)
    if not kind:
        return message, None

    cloned = copy.copy(message)
    cloned.message = out_text
    cloned.entities = out_entities
    return cloned, kind


def _message_age_seconds(parent, child) -> Optional[float]:
    try:
        return max(0.0, float(child.date.timestamp()) - float(parent.date.timestamp()))
    except Exception:
        return None


def _real_reply_parent_id(message) -> Optional[int]:
    for value in reply_source_ids(message):
        try:
            value = int(value)
        except Exception:
            continue
        if value and value != SOURCE_TOPIC:
            return value
    return None


def _run_inline_self_test():
    update_count, safe_count = run_update_self_test()

    signal_cases = (
        (
            "Buy xauusd now\n\nTp 4324\nTp 4337\nTp 4350\nTp open\n\nSl 4308",
            "BUY", "NOW", ["4324", "4337", "4350", "OPEN"], "4308",
        ),
        (
            "Sell xauusd now 4327-4332\n\nTp 4320\nTp 4310\nTp 4280\n\nSl 4335",
            "SELL", "ZONE", ["4320", "4310", "4280"], "4335",
        ),
        (
            "Buy xauusd now 4305-4299\n\nTp 4315\nTp 4330\nTp 4370\nTp open\n\nSl 4290",
            "BUY", "ZONE", ["4315", "4330", "4370", "OPEN"], "4290",
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
    """Own the exact 508 -> VIP7 send path: format + reply preservation before send."""
    logger = logger or log

    if main_module is None:
        raise RuntimeError("main worker module unavailable")
    if not _registry_ready(registry):
        raise RuntimeError("emoji registry is not ready")

    existing = getattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", None)
    if existing:
        return existing

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

    async def recover_missing_signal_parent(message, route):
        """Recover one explicitly replied-to signal, never bulk history.

        This only runs after normal/local and cross-account mapping recovery both
        failed. It is restricted to an XAU signal parent no older than 24 hours,
        so a live reply cannot lose its thread merely because the parent was
        missed during a deploy/account handoff.
        """
        parent_id = _real_reply_parent_id(message)
        if not parent_id:
            return None

        client = getattr(main_module, "client", None)
        if client is None:
            return None

        try:
            parent = await client.get_messages(SOURCE_CHAT, ids=parent_id)
        except Exception as exc:
            logger.warning(
                "[IMPERIUM VIP REPLY PARENT FETCH FAILED] parent=%s %s: %s",
                parent_id,
                type(exc).__name__,
                exc,
            )
            return None

        if not parent:
            return None

        parent_text = getattr(parent, "message", None) or getattr(parent, "raw_text", None) or ""
        parsed_parent = parse_xauusd_signal(parent_text)
        if not parsed_parent:
            # Do not import arbitrary old chat just to manufacture a reply chain.
            return None

        age = _message_age_seconds(parent, message)
        if age is None or age > MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS:
            logger.warning(
                "[IMPERIUM VIP REPLY PARENT TOO OLD] parent=%s age=%s max=%s",
                parent_id,
                age,
                MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS,
            )
            return None

        formatted_parent, kind = _clone_message_with_format(parent, registry)
        if kind != "signal":
            return None

        sent_parent = await original_copy_one(
            formatted_parent,
            route,
            edited=False,
            ensure_reply=False,
        )
        if not sent_parent:
            return None

        logger.warning(
            "[IMPERIUM VIP REPLY PARENT RECOVERED BY COPY] source_parent=%s dest_parent=%s age=%.1fs signal_only=True",
            parent_id,
            getattr(sent_parent, "id", None),
            age,
        )
        return getattr(sent_parent, "id", None)

    async def copy_one_wrapper(message, route, edited=False, ensure_reply=True):
        if not is_imperium_vip_route(route):
            return await original_copy_one(message, route, edited=edited, ensure_reply=ensure_reply)

        mention_guard = getattr(main_module, "has_username_mention", None)
        if callable(mention_guard):
            try:
                if mention_guard(message):
                    return await original_copy_one(message, route, edited=edited, ensure_reply=ensure_reply)
            except Exception:
                pass

        if ensure_reply and _real_reply_parent_id(message):
            resolved = await ensure_reply_mapping(
                main_module,
                message,
                route,
                registry=registry,
                logger=logger,
            )
            if not resolved:
                await recover_missing_signal_parent(message, route)

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
            return await original_edit(message, route, cascade_depth=cascade_depth, visited=visited)

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
        if cloned_messages and _real_reply_parent_id(cloned_messages[0]):
            resolved = await ensure_reply_mapping(
                main_module,
                cloned_messages[0],
                route,
                registry=registry,
                logger=logger,
            )
            if not resolved:
                await recover_missing_signal_parent(cloned_messages[0], route)

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
        "missing_signal_parent_recovery": True,
        "missing_signal_parent_max_age_seconds": MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS,
        "primary": "inline_forward_path",
    }
    setattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", state)

    logger.warning(
        "[IMPERIUM VIP INLINE HOOK ACTIVE] source=%s_%s dest=%s_%s signals=True updates=True replies=True cross_account_replies=True missing_signal_parent_recovery=True edits=True albums=%s PRIMARY=True",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
        bool(callable(original_copy_album)),
    )

    return state

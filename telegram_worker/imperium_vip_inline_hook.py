import copy
import logging
from typing import Any, Dict, Optional, Tuple

from telegram_worker.imperium_vip_formatter import build_signal, build_update, parse_xauusd_signal
from telegram_worker.imperium_vip_trade_update_hardening import classify_trade_update, run_update_self_test
from telegram_worker.reply_hardening import install_reply_hardening, reply_source_ids

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


def format_imperium_vip_text(text: str, original_entities, registry) -> Tuple[str, Any, Optional[str]]:
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
    """Restyle edits on 508 -> VIP7. New posts are restyled by the delivery engine
    (engine/render.py "imperium_vip"), which also recovers a missed signal parent."""
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

    if not callable(getattr(main_module, "edit_existing_destination_in_place", None)):
        raise RuntimeError("worker edit_existing_destination_in_place is unavailable")

    state = {
        "source_chat": SOURCE_CHAT,
        "source_topic": SOURCE_TOPIC,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "new_posts": "engine.render (imperium_vip) + engine.deliver",
        "edits": True,
        "replies": True,
        "cross_account_reply_recovery": True,
        "missing_signal_parent_recovery": True,
        "missing_signal_parent_max_age_seconds": MAX_REPLY_PARENT_RECOVERY_AGE_SECONDS,
        "primary": "edit_wrapper",
    }
    setattr(main_module, "_IMPERIUM_VIP_INLINE_HOOK_STATE", state)

    logger.warning(
        "[IMPERIUM VIP INLINE HOOK ACTIVE] source=%s_%s dest=%s_%s signals=True updates=True replies=True cross_account_replies=True missing_signal_parent_recovery=engine edits=True new_posts=engine",
        SOURCE_CHAT,
        SOURCE_TOPIC,
        DEST_CHAT,
        DEST_TOPIC,
    )

    return state

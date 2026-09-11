import asyncio
import logging
import re
import unicodedata
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


log = logging.getLogger("telegram-reply-hardening")

IMPERIUM_SOURCE_CHAT = -1004367822325
IMPERIUM_SOURCE_TOPIC = 508
IMPERIUM_DEST_CHAT = -1003726286301
IMPERIUM_DEST_TOPIC = 7


def _as_int(value) -> Optional[int]:
    try:
        value = int(value)
        return value if value else None
    except Exception:
        return None


def _append_unique(values: List[int], value) -> None:
    number = _as_int(value)
    if number is not None and number not in values:
        values.append(number)


def reply_source_ids(message) -> List[int]:
    """Read reply IDs from both Message and MessageReplyHeader shapes."""
    ids: List[int] = []

    for attr in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
        _append_unique(ids, getattr(message, attr, None))

    reply = getattr(message, "reply_to", None)
    if reply is not None:
        for attr in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
            _append_unique(ids, getattr(reply, attr, None))

    return ids


def _mapped_ids(main_module, value) -> List[int]:
    helper = getattr(main_module, "mapped_ids_from_value", None)
    if callable(helper):
        try:
            return [int(v) for v in helper(value) if _as_int(v) is not None]
        except Exception:
            pass

    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            _append_unique(out, item)
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            _append_unique(out, item)
        return out
    number = _as_int(value)
    return [number] if number is not None else []


def _exact_map_key(main_module, route: Dict[str, Any], source_msg_id: int) -> str:
    fn = getattr(main_module, "map_key", None)
    if callable(fn):
        return fn(
            route["source_chat"],
            source_msg_id,
            route["dest_chat"],
            route["dest_topic"],
        )
    return f"{route['source_chat']}:{source_msg_id}:{route['dest_chat']}:{route['dest_topic']}"


def mapped_destination_ids(main_module, route: Dict[str, Any], source_msg_id: int) -> List[int]:
    """Resolve mapping across int/list/dict and older compatible map entries."""
    message_map = getattr(main_module, "message_map", {}) or {}
    exact_key = _exact_map_key(main_module, route, int(source_msg_id))

    ids = _mapped_ids(main_module, message_map.get(exact_key))
    if ids:
        return ids

    wanted = (
        str(int(route["source_chat"])),
        str(int(source_msg_id)),
        str(int(route["dest_chat"])),
        str(int(route["dest_topic"])),
    )
    out: List[int] = []
    for key, value in list(message_map.items()):
        try:
            parts = tuple(str(key).split(":"))
            if len(parts) != 4 or parts != wanted:
                continue
            for destination_id in _mapped_ids(main_module, value):
                _append_unique(out, destination_id)
        except Exception:
            continue
    return out


def mapped_reply_id(main_module, message, route: Dict[str, Any]) -> Optional[int]:
    source_topic = _as_int(route.get("source_topic"))
    for source_msg_id in reply_source_ids(message):
        if source_topic is not None and int(source_msg_id) == source_topic:
            continue
        ids = mapped_destination_ids(main_module, route, source_msg_id)
        if ids:
            return int(ids[0])
    return None


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\ufe0f", "").replace("\u200b", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def _message_text(message) -> str:
    return (
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    )


def _is_imperium_route(route: Dict[str, Any]) -> bool:
    try:
        return (
            int(route.get("source_chat")) == IMPERIUM_SOURCE_CHAT
            and int(route.get("source_topic")) == IMPERIUM_SOURCE_TOPIC
            and int(route.get("dest_chat")) == IMPERIUM_DEST_CHAT
            and int(route.get("dest_topic")) == IMPERIUM_DEST_TOPIC
        )
    except Exception:
        return False


def _expected_destination_text(parent, route, registry) -> str:
    source_text = _message_text(parent)
    if not source_text:
        return ""

    if not _is_imperium_route(route) or registry is None:
        return source_text

    try:
        from telegram_worker.imperium_vip_formatter import (
            build_signal,
            build_update,
            parse_xauusd_signal,
        )
        from telegram_worker.imperium_vip_trade_update_hardening import classify_trade_update

        parsed = parse_xauusd_signal(source_text)
        if parsed:
            rendered, _entities = build_signal(registry, parsed)
            return rendered

        update_template = classify_trade_update(source_text)
        if update_template:
            rendered, _entities = build_update(registry, update_template)
            return rendered
    except Exception:
        pass

    return source_text


def _date_distance_seconds(source_message, destination_message) -> float:
    try:
        return abs(
            float(getattr(destination_message, "date").timestamp())
            - float(getattr(source_message, "date").timestamp())
        )
    except Exception:
        return 10**12


async def _destination_topic_messages(main_module, route: Dict[str, Any], limit: int = 400):
    client = getattr(main_module, "client", None)
    if client is None:
        return []

    try:
        messages = await client.get_messages(
            int(route["dest_chat"]),
            limit=max(50, int(limit)),
            reply_to=int(route["dest_topic"]),
        )
        return list(messages or [])
    except Exception:
        pass

    try:
        messages = await client.get_messages(
            int(route["dest_chat"]),
            limit=max(200, int(limit) * 2),
        )
    except Exception:
        return []

    topic_fn = getattr(main_module, "topic_of", None)
    if not callable(topic_fn):
        return list(messages or [])

    out = []
    for candidate in messages or []:
        try:
            if int(topic_fn(candidate, int(route["dest_chat"]))) == int(route["dest_topic"]):
                out.append(candidate)
        except Exception:
            continue
    return out


def _cache_mapping(main_module, route: Dict[str, Any], source_msg_id: int, dest_msg_id: int) -> None:
    message_map = getattr(main_module, "message_map", None)
    if not isinstance(message_map, dict):
        return

    key = _exact_map_key(main_module, route, int(source_msg_id))
    message_map[key] = int(dest_msg_id)

    save_fn = getattr(main_module, "save_map", None) or getattr(main_module, "save_message_map", None)
    if callable(save_fn):
        save_fn()


def _drop_exact_mapping(main_module, route: Dict[str, Any], source_msg_id: int) -> None:
    message_map = getattr(main_module, "message_map", None)
    if not isinstance(message_map, dict):
        return
    key = _exact_map_key(main_module, route, int(source_msg_id))
    if key in message_map:
        message_map.pop(key, None)
        save_fn = getattr(main_module, "save_map", None) or getattr(main_module, "save_message_map", None)
        if callable(save_fn):
            save_fn()


async def ensure_reply_mapping(
    main_module,
    message,
    route: Dict[str, Any],
    registry=None,
    logger=None,
) -> Optional[int]:
    """Resolve reply parent locally or recover it from destination topic history.

    Cross-account recovery is important when one Telegram worker forwarded the
    parent and another worker later receives the reply: their local message-map
    files are not shared. The destination message itself becomes the recovery
    source of truth, matched by exact house-formatted text and nearest time.
    """
    logger = logger or log
    source_topic = _as_int(route.get("source_topic"))
    reply_ids = reply_source_ids(message)
    if not reply_ids:
        return None

    client = getattr(main_module, "client", None)
    if client is None:
        return None

    for source_parent_id in reply_ids:
        if source_topic is not None and int(source_parent_id) == source_topic:
            continue

        existing = mapped_destination_ids(main_module, route, source_parent_id)
        if existing:
            try:
                current_parent = await client.get_messages(
                    int(route["dest_chat"]),
                    ids=int(existing[0]),
                )
            except Exception:
                current_parent = None

            if current_parent:
                logger.info(
                    "[REPLY MAP HIT] source=%s_%s parent=%s dest=%s_%s parent_dest=%s",
                    route.get("source_chat"),
                    route.get("source_topic"),
                    source_parent_id,
                    route.get("dest_chat"),
                    route.get("dest_topic"),
                    existing[0],
                )
                return int(existing[0])

            logger.warning(
                "[REPLY STALE MAP DROPPED] source_parent=%s stale_dest_parent=%s",
                source_parent_id,
                existing[0],
            )
            _drop_exact_mapping(main_module, route, source_parent_id)

        try:
            parent = await client.get_messages(
                int(route["source_chat"]),
                ids=int(source_parent_id),
            )
        except Exception as exc:
            logger.warning(
                "[REPLY PARENT FETCH FAILED] source_parent=%s %s: %s",
                source_parent_id,
                type(exc).__name__,
                exc,
            )
            continue

        if not parent:
            continue

        expected = _expected_destination_text(parent, route, registry)
        expected_key = _normalise_text(expected)
        if not expected_key:
            continue

        # A different forwarding account may still be creating the parent.
        # Retry briefly before giving up and falling back to the topic root.
        for attempt, delay in enumerate((0.0, 0.25, 0.75, 1.5), start=1):
            if delay:
                await asyncio.sleep(delay)

            candidates = await _destination_topic_messages(main_module, route)
            matches = [
                candidate
                for candidate in candidates
                if _normalise_text(_message_text(candidate)) == expected_key
                and _as_int(getattr(candidate, "id", None)) != _as_int(route.get("dest_topic"))
            ]

            if not matches:
                continue

            matches.sort(key=lambda candidate: _date_distance_seconds(parent, candidate))
            chosen = matches[0]
            chosen_id = int(chosen.id)
            distance = _date_distance_seconds(parent, chosen)

            if distance > 6 * 60 * 60:
                logger.warning(
                    "[REPLY CROSS-ACCOUNT REJECTED OLD MATCH] source_parent=%s dest_parent=%s distance=%.1fs",
                    source_parent_id,
                    chosen_id,
                    distance,
                )
                break

            _cache_mapping(main_module, route, source_parent_id, chosen_id)
            logger.warning(
                "[REPLY CROSS-ACCOUNT RECOVERED] source=%s_%s parent=%s dest=%s_%s parent_dest=%s candidates=%s distance=%.1fs attempt=%s",
                route.get("source_chat"),
                route.get("source_topic"),
                source_parent_id,
                route.get("dest_chat"),
                route.get("dest_topic"),
                chosen_id,
                len(matches),
                distance,
                attempt,
            )
            return chosen_id

        logger.warning(
            "[REPLY CROSS-ACCOUNT UNRESOLVED] source_parent=%s dest=%s_%s expected=%r",
            source_parent_id,
            route.get("dest_chat"),
            route.get("dest_topic"),
            expected[:120],
        )

    return None


def install_reply_hardening(main_module, logger=None):
    """Patch reply parsing/mapping while preserving live-only no-history rules."""
    logger = logger or log

    if main_module is None:
        raise RuntimeError("main worker module unavailable")

    existing = getattr(main_module, "_REPLY_HARDENING_STATE", None)
    if existing:
        return existing

    if not isinstance(getattr(main_module, "message_map", None), dict):
        raise RuntimeError("worker message_map unavailable")

    direct = SimpleNamespace(reply_to_msg_id=123, reply_to_top_id=508, top_msg_id=None, reply_to=None)
    nested = SimpleNamespace(
        reply_to_msg_id=None,
        reply_to_top_id=None,
        top_msg_id=None,
        reply_to=SimpleNamespace(reply_to_msg_id=456, reply_to_top_id=508, top_msg_id=None),
    )
    if reply_source_ids(direct) != [123, 508]:
        raise RuntimeError("reply hardening direct-ID self-test failed")
    if reply_source_ids(nested) != [456, 508]:
        raise RuntimeError("reply hardening nested-ID self-test failed")
    if _mapped_ids(main_module, [901, 902]) != [901, 902]:
        raise RuntimeError("reply hardening list-map self-test failed")

    def patched_reply_source_ids(message):
        return reply_source_ids(message)

    def patched_mapped_reply_id(message, route):
        return mapped_reply_id(main_module, message, route)

    def patched_reply_target(message, route):
        reply_ids = reply_source_ids(message)
        if not reply_ids:
            return route["dest_topic"]

        mapped = mapped_reply_id(main_module, message, route)
        if mapped:
            return mapped

        logger.warning(
            "[REPLY FALLBACK TO TOPIC ROOT] route=%s source=%s_%s msg=%s reply_ids=%s dest=%s_%s",
            route.get("name"),
            route.get("source_chat"),
            route.get("source_topic"),
            getattr(message, "id", None),
            reply_ids,
            route.get("dest_chat"),
            route.get("dest_topic"),
        )
        return route["dest_topic"]

    main_module.reply_source_ids = patched_reply_source_ids
    main_module.mapped_reply_id = patched_mapped_reply_id
    main_module.reply_target = patched_reply_target

    state = {
        "reply_metadata": "direct+header",
        "map_values": "int+list+dict",
        "cross_account_recovery": True,
        "cross_account_retries": True,
        "stale_map_verification": True,
        "no_history_parent_copy": "preserved",
    }
    setattr(main_module, "_REPLY_HARDENING_STATE", state)

    logger.warning(
        "[REPLY HARDENING ACTIVE] direct_ids=True nested_header=True list_maps=True cross_account_recovery=True retries=True stale_map_check=True old_parent_import=False"
    )
    return state

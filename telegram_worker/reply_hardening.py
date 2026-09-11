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

# Recovery is deliberately bounded. Normal operation should resolve from the
# persistent message_map without any Telegram history scan at all.
RECOVERY_CHAT_LIMIT = 800
RECOVERY_TOPIC_LIMIT = 220
TEXT_MATCH_MAX_AGE_SECONDS = 6 * 60 * 60
MEDIA_MATCH_MAX_AGE_SECONDS = 10 * 60


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
    """Read real reply metadata from every Telethon shape we encounter.

    Actual parent IDs are deliberately ordered before forum top/root IDs so a
    reply inside a topic cannot accidentally resolve to the topic root first.
    """
    ids: List[int] = []

    _append_unique(ids, getattr(message, "reply_to_msg_id", None))
    reply = getattr(message, "reply_to", None)
    if reply is not None:
        _append_unique(ids, getattr(reply, "reply_to_msg_id", None))

    for attr in ("reply_to_top_id", "top_msg_id"):
        _append_unique(ids, getattr(message, attr, None))
    if reply is not None:
        for attr in ("reply_to_top_id", "top_msg_id"):
            _append_unique(ids, getattr(reply, attr, None))

    return ids


def real_reply_source_ids(message, route: Dict[str, Any]) -> List[int]:
    """Return only message parents, never the source forum-topic root."""
    source_topic = _as_int(route.get("source_topic"))
    out: List[int] = []
    for value in reply_source_ids(message):
        if source_topic is not None and int(value) == source_topic:
            continue
        _append_unique(out, value)
    return out


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
    """Resolve mappings made by all historical map formats: int/list/dict."""
    message_map = getattr(main_module, "message_map", {}) or {}
    exact_key = _exact_map_key(main_module, route, int(source_msg_id))

    ids = _mapped_ids(main_module, message_map.get(exact_key))
    if ids:
        return ids

    # Compatibility with old maps whose keys were serialised manually.
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
    for source_msg_id in real_reply_source_ids(message, route):
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


def _media_kind(message) -> str:
    media = getattr(message, "media", None)
    return type(media).__name__ if media is not None else ""


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


def _message_mentions_topic(message, topic_id: int) -> bool:
    """Identify destination forum membership without using routes_for()."""
    wanted = _as_int(topic_id)
    if wanted is None:
        return False

    values: List[int] = []
    for attr in ("reply_to_top_id", "top_msg_id", "reply_to_msg_id"):
        _append_unique(values, getattr(message, attr, None))
    reply = getattr(message, "reply_to", None)
    if reply is not None:
        for attr in ("reply_to_top_id", "top_msg_id", "reply_to_msg_id"):
            _append_unique(values, getattr(reply, attr, None))
    return wanted in values


async def _destination_topic_messages(main_module, route: Dict[str, Any], limit: int = RECOVERY_CHAT_LIMIT):
    """Get recovery candidates while avoiding GetReplies flood pressure.

    First scan normal chat history (GetHistory) and filter by Telegram forum
    metadata locally. Only if that cannot see the topic do we issue the much
    more flood-prone GetReplies request.
    """
    client = getattr(main_module, "client", None)
    if client is None:
        return []

    dest_chat = int(route["dest_chat"])
    dest_topic = int(route["dest_topic"])

    try:
        messages = list(
            await client.get_messages(
                dest_chat,
                limit=max(200, int(limit)),
            )
            or []
        )
        filtered = [m for m in messages if _message_mentions_topic(m, dest_topic)]
        if filtered:
            return filtered
    except Exception:
        pass

    try:
        return list(
            await client.get_messages(
                dest_chat,
                limit=RECOVERY_TOPIC_LIMIT,
                reply_to=dest_topic,
            )
            or []
        )
    except Exception:
        return []


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


async def _mapped_parent_that_exists(main_module, route, source_parent_id) -> Optional[int]:
    client = getattr(main_module, "client", None)
    if client is None:
        return None

    existing = mapped_destination_ids(main_module, route, source_parent_id)
    for destination_id in existing:
        try:
            current_parent = await client.get_messages(
                int(route["dest_chat"]),
                ids=int(destination_id),
            )
        except Exception:
            current_parent = None

        if current_parent:
            return int(destination_id)

    if existing:
        _drop_exact_mapping(main_module, route, source_parent_id)
    return None


async def ensure_reply_mapping(
    main_module,
    message,
    route: Dict[str, Any],
    registry=None,
    logger=None,
) -> Optional[int]:
    """Resolve the exact forwarded parent for any route.

    Resolution order:
      1. durable local source->destination map;
      2. exact destination text/media recovery, used only if the map is absent;
      3. fail closed to the topic root rather than guessing a wrong parent.

    This works for text, captions, albums and media-only parents. It never bulk
    copies history and therefore preserves the live-only/no-history contract.
    """
    logger = logger or log
    parent_ids = real_reply_source_ids(message, route)
    if not parent_ids:
        return None

    client = getattr(main_module, "client", None)
    if client is None:
        return None

    for source_parent_id in parent_ids:
        mapped = await _mapped_parent_that_exists(main_module, route, source_parent_id)
        if mapped:
            logger.info(
                "[REPLY MAP HIT] route=%s source=%s_%s parent=%s dest=%s_%s parent_dest=%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                source_parent_id,
                route.get("dest_chat"),
                route.get("dest_topic"),
                mapped,
            )
            return mapped

        try:
            parent = await client.get_messages(
                int(route["source_chat"]),
                ids=int(source_parent_id),
            )
        except Exception as exc:
            logger.warning(
                "[REPLY PARENT FETCH FAILED] route=%s source_parent=%s %s: %s",
                route.get("name"),
                source_parent_id,
                type(exc).__name__,
                exc,
            )
            continue

        if not parent:
            continue

        expected = _expected_destination_text(parent, route, registry)
        expected_key = _normalise_text(expected)
        source_media_kind = _media_kind(parent)

        for attempt, delay in enumerate((0.0, 0.20, 0.60, 1.20), start=1):
            if delay:
                await asyncio.sleep(delay)

            candidates = await _destination_topic_messages(main_module, route)
            if expected_key:
                matches = [
                    candidate
                    for candidate in candidates
                    if _normalise_text(_message_text(candidate)) == expected_key
                    and _as_int(getattr(candidate, "id", None)) != _as_int(route.get("dest_topic"))
                ]
                max_age = TEXT_MATCH_MAX_AGE_SECONDS
            elif source_media_kind:
                # Media-only recovery is deliberately stricter: same Telegram
                # media class and a close timestamp, otherwise we do not guess.
                matches = [
                    candidate
                    for candidate in candidates
                    if _media_kind(candidate) == source_media_kind
                    and _as_int(getattr(candidate, "id", None)) != _as_int(route.get("dest_topic"))
                ]
                max_age = MEDIA_MATCH_MAX_AGE_SECONDS
            else:
                matches = []
                max_age = 0

            if not matches:
                continue

            matches.sort(key=lambda candidate: _date_distance_seconds(parent, candidate))
            chosen = matches[0]
            chosen_id = int(chosen.id)
            distance = _date_distance_seconds(parent, chosen)

            if distance > max_age:
                break

            # For media-only recovery, reject ambiguous near-equal candidates.
            if not expected_key and len(matches) > 1:
                second_distance = _date_distance_seconds(parent, matches[1])
                if abs(second_distance - distance) < 2.0:
                    logger.warning(
                        "[REPLY MEDIA RECOVERY AMBIGUOUS] route=%s source_parent=%s candidates=%s",
                        route.get("name"),
                        source_parent_id,
                        len(matches),
                    )
                    break

            _cache_mapping(main_module, route, source_parent_id, chosen_id)
            logger.warning(
                "[REPLY RECOVERED] route=%s source=%s_%s parent=%s dest=%s_%s parent_dest=%s candidates=%s distance=%.1fs attempt=%s mode=%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                source_parent_id,
                route.get("dest_chat"),
                route.get("dest_topic"),
                chosen_id,
                len(matches),
                distance,
                attempt,
                "text" if expected_key else "media",
            )
            return chosen_id

        logger.warning(
            "[REPLY UNRESOLVED] route=%s source=%s_%s msg=%s parent=%s dest=%s_%s",
            route.get("name"),
            route.get("source_chat"),
            route.get("source_topic"),
            getattr(message, "id", None),
            source_parent_id,
            route.get("dest_chat"),
            route.get("dest_topic"),
        )

    return None


def install_reply_hardening(main_module, logger=None):
    """Install reply preservation globally across every main-worker route."""
    logger = logger or log

    if main_module is None:
        raise RuntimeError("main worker module unavailable")

    existing = getattr(main_module, "_REPLY_HARDENING_STATE", None)
    if existing:
        return existing

    if not isinstance(getattr(main_module, "message_map", None), dict):
        raise RuntimeError("worker message_map unavailable")

    original_copy_one = getattr(main_module, "copy_one", None)
    original_copy_album = getattr(main_module, "copy_album", None)
    if not callable(original_copy_one):
        raise RuntimeError("worker copy_one unavailable")

    # Startup regression tests for the Telegram metadata forms that caused the
    # historical failures.
    direct = SimpleNamespace(reply_to_msg_id=123, reply_to_top_id=508, top_msg_id=None, reply_to=None)
    nested = SimpleNamespace(
        reply_to_msg_id=None,
        reply_to_top_id=None,
        top_msg_id=None,
        reply_to=SimpleNamespace(reply_to_msg_id=456, reply_to_top_id=508, top_msg_id=None),
    )
    root_only = SimpleNamespace(reply_to_msg_id=508, reply_to_top_id=508, top_msg_id=None, reply_to=None)
    if reply_source_ids(direct) != [123, 508]:
        raise RuntimeError("reply hardening direct-ID self-test failed")
    if reply_source_ids(nested) != [456, 508]:
        raise RuntimeError("reply hardening nested-ID self-test failed")
    if real_reply_source_ids(root_only, {"source_topic": 508}) != []:
        raise RuntimeError("reply hardening topic-root self-test failed")
    if _mapped_ids(main_module, [901, 902]) != [901, 902]:
        raise RuntimeError("reply hardening list-map self-test failed")
    if _mapped_ids(main_module, {"a": 903, "b": 904}) != [903, 904]:
        raise RuntimeError("reply hardening dict-map self-test failed")

    def patched_reply_source_ids(message):
        return reply_source_ids(message)

    def patched_mapped_reply_id(message, route):
        return mapped_reply_id(main_module, message, route)

    def patched_reply_target(message, route):
        parents = real_reply_source_ids(message, route)
        if not parents:
            return route["dest_topic"]

        mapped = mapped_reply_id(main_module, message, route)
        if mapped:
            return mapped

        logger.warning(
            "[REPLY FALLBACK TO TOPIC ROOT] route=%s source=%s_%s msg=%s real_parent_ids=%s dest=%s_%s",
            route.get("name"),
            route.get("source_chat"),
            route.get("source_topic"),
            getattr(message, "id", None),
            parents,
            route.get("dest_chat"),
            route.get("dest_topic"),
        )
        return route["dest_topic"]

    async def global_copy_one(message, route, edited=False, ensure_reply=True):
        if ensure_reply and real_reply_source_ids(message, route):
            # Fast path: a correct durable mapping costs no history request.
            if mapped_reply_id(main_module, message, route) is None:
                registry = getattr(main_module, "EMOJI_REGISTRY", None)
                await ensure_reply_mapping(
                    main_module,
                    message,
                    route,
                    registry=registry,
                    logger=logger,
                )
        return await original_copy_one(
            message,
            route,
            edited=edited,
            ensure_reply=ensure_reply,
        )

    async def global_copy_album(messages, route):
        messages = list(messages or [])
        probe = next(
            (m for m in messages if real_reply_source_ids(m, route)),
            messages[0] if messages else None,
        )
        if probe is not None and real_reply_source_ids(probe, route):
            if mapped_reply_id(main_module, probe, route) is None:
                registry = getattr(main_module, "EMOJI_REGISTRY", None)
                await ensure_reply_mapping(
                    main_module,
                    probe,
                    route,
                    registry=registry,
                    logger=logger,
                )
        return await original_copy_album(messages, route)

    main_module.reply_source_ids = patched_reply_source_ids
    main_module.mapped_reply_id = patched_mapped_reply_id
    main_module.reply_target = patched_reply_target
    main_module.copy_one = global_copy_one
    if callable(original_copy_album):
        main_module.copy_album = global_copy_album

    state = {
        "reply_metadata": "direct+header",
        "topic_root_filter": True,
        "map_values": "int+list+dict",
        "global_copy_one": True,
        "global_copy_album": callable(original_copy_album),
        "all_routes": True,
        "recovery": "local-map->history-text/media->root",
        "history_scan": "GetHistory-first/GetReplies-last",
        "no_history_parent_copy": "preserved",
    }
    setattr(main_module, "_REPLY_HARDENING_STATE", state)

    logger.warning(
        "[REPLY HARDENING V2 ACTIVE] all_routes=True direct_ids=True nested_header=True topic_root_filter=True list_maps=True dict_maps=True text_recovery=True media_recovery=True gethistory_first=True getreplies_last=True old_parent_import=False"
    )
    return state

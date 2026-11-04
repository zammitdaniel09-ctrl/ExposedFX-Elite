import asyncio
import logging
import re
import unicodedata
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from telethon.errors import FloodWaitError

from telegram_worker import owner_forwarding_policy as owner_policy

log = logging.getLogger("alt-reply-hardening")

# Speed (owner request 3 Nov 2026): a reply never waits on a long history
# search. Mapped parents are trusted; an in-flight parent gets a short
# in-memory wait; recovery is one bounded attempt, then the topic root.
RECOVERY_CHAT_LIMIT = 100
RECOVERY_TOPIC_LIMIT = 100
PARENT_INFLIGHT_WAIT_SECONDS = 1.0
RECOVERY_BUDGET_SECONDS = 2.0
TEXT_MATCH_MAX_AGE_SECONDS = 6 * 60 * 60
MEDIA_MATCH_MAX_AGE_SECONDS = 10 * 60


def _reupload_cannot_help(exc) -> bool:
    return isinstance(exc, FloodWaitError) or bool(owner_policy.permanent_access_error(exc))


def _reply_target_invalid(exc) -> bool:
    from telegram_worker.engine.deliver import reply_target_gone

    return reply_target_gone(exc)


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
    """Read actual Telegram reply IDs before forum top/root IDs."""
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


def real_reply_source_ids(route: Dict[str, Any], message) -> List[int]:
    source_topic = _as_int(route.get("source_topic"))
    out: List[int] = []
    for value in reply_source_ids(message):
        if source_topic is not None and int(value) == source_topic:
            continue
        _append_unique(out, value)
    return out


def _message_text(message) -> str:
    return (
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    )


def _normalise_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "")
    value = value.replace("\ufe0f", "").replace("\u200b", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip().casefold()


def _media_kind(message) -> str:
    media = getattr(message, "media", None)
    return type(media).__name__ if media is not None else ""


def _date_distance_seconds(source_message, destination_message) -> float:
    try:
        return abs(
            float(getattr(destination_message, "date").timestamp())
            - float(getattr(source_message, "date").timestamp())
        )
    except Exception:
        return 10**12


def _message_mentions_topic(message, topic_id: int) -> bool:
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


def _cache_mapping(worker, route, source_parent_id: int, destination_parent_id: int) -> None:
    worker.message_map[worker.map_key(route, int(source_parent_id))] = int(destination_parent_id)
    worker.save_json(worker.MAP_FILE, worker.message_map)


async def _destination_topic_messages(worker, route):
    """Use GetHistory first; GetReplies is last-resort only."""
    dest_chat = int(route["dest_chat"])
    dest_topic = int(route["dest_topic"])

    try:
        messages = list(await worker.client.get_messages(dest_chat, limit=RECOVERY_CHAT_LIMIT) or [])
        filtered = [m for m in messages if _message_mentions_topic(m, dest_topic)]
        if filtered:
            return filtered
    except Exception:
        pass

    try:
        return list(
            await worker.client.get_messages(dest_chat, limit=RECOVERY_TOPIC_LIMIT, reply_to=dest_topic) or []
        )
    except Exception:
        return []


def _mapped_parent(worker, route, source_parent_id: int) -> Optional[int]:
    for destination_id in worker.mapped_ids(route, source_parent_id) or []:
        if _as_int(destination_id):
            return int(destination_id)
    return None


async def _wait_for_mapped_parent(worker, route, source_parent_id: int) -> Optional[int]:
    """A parent posted moments earlier may still be sending; wait in memory only."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PARENT_INFLIGHT_WAIT_SECONDS
    while True:
        mapped = _mapped_parent(worker, route, source_parent_id)
        if mapped or loop.time() >= deadline:
            return mapped
        await asyncio.sleep(0.1)


async def _recover_parent(worker, route, message, source_parent_id: int, logger) -> Optional[int]:
    """One bounded attempt to find an unmapped parent's copy in the destination."""
    try:
        source_parent = await worker.client.get_messages(int(route["source_chat"]), ids=int(source_parent_id))
    except Exception as exc:
        logger.warning(
            "[ALT REPLY PARENT FETCH FAILED] route=%s parent=%s %s: %s",
            route.get("name"),
            source_parent_id,
            type(exc).__name__,
            exc,
        )
        return None

    if not source_parent:
        return None

    # Translation routes must compare against the translated destination
    # caption/text, not the raw Arabic source text.
    try:
        expected_text, _expected_entities = await worker.translated_payload(route, source_parent)
    except Exception:
        expected_text = _message_text(source_parent)

    expected_key = _normalise_text(expected_text)
    source_media_kind = _media_kind(source_parent)

    candidates = await _destination_topic_messages(worker, route)
    if expected_key:
        matches = [
            candidate
            for candidate in candidates
            if _normalise_text(_message_text(candidate)) == expected_key
            and _as_int(getattr(candidate, "id", None)) != _as_int(route.get("dest_topic"))
        ]
        max_age = TEXT_MATCH_MAX_AGE_SECONDS
        mode = "text"
    elif source_media_kind:
        matches = [
            candidate
            for candidate in candidates
            if _media_kind(candidate) == source_media_kind
            and _as_int(getattr(candidate, "id", None)) != _as_int(route.get("dest_topic"))
        ]
        max_age = MEDIA_MATCH_MAX_AGE_SECONDS
        mode = "media"
    else:
        return None

    if not matches:
        return None

    matches.sort(key=lambda candidate: _date_distance_seconds(source_parent, candidate))
    chosen = matches[0]
    distance = _date_distance_seconds(source_parent, chosen)
    if distance > max_age:
        return None

    if mode == "media" and len(matches) > 1:
        second_distance = _date_distance_seconds(source_parent, matches[1])
        if abs(second_distance - distance) < 2.0:
            logger.warning(
                "[ALT REPLY MEDIA RECOVERY AMBIGUOUS] route=%s parent=%s candidates=%s",
                route.get("name"),
                source_parent_id,
                len(matches),
            )
            return None

    chosen_id = int(chosen.id)
    _cache_mapping(worker, route, source_parent_id, chosen_id)
    logger.warning(
        "[ALT REPLY RECOVERED] route=%s source=%s_%s child=%s parent=%s dest=%s_%s parent_dest=%s candidates=%s distance=%.1fs mode=%s",
        route.get("name"),
        route.get("source_chat"),
        route.get("source_topic"),
        getattr(message, "id", None),
        source_parent_id,
        route.get("dest_chat"),
        route.get("dest_topic"),
        chosen_id,
        len(matches),
        distance,
        mode,
    )
    return chosen_id


async def resolve_reply_target(worker, route, message, logger=None) -> int:
    """Resolve source reply -> exact destination parent without flattening it."""
    logger = logger or log
    parent_ids = real_reply_source_ids(route, message)
    if not parent_ids:
        return int(route["dest_topic"])

    for source_parent_id in parent_ids:
        mapped = await _wait_for_mapped_parent(worker, route, source_parent_id)
        if mapped:
            logger.info(
                "[ALT REPLY MAP HIT] route=%s source=%s_%s child=%s parent=%s dest=%s_%s parent_dest=%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                getattr(message, "id", None),
                source_parent_id,
                route.get("dest_chat"),
                route.get("dest_topic"),
                mapped,
            )
            return mapped

        try:
            recovered = await asyncio.wait_for(
                _recover_parent(worker, route, message, source_parent_id, logger), RECOVERY_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            recovered = None
            logger.warning(
                "[ALT REPLY RECOVERY TIMEOUT] route=%s parent=%s budget=%ss",
                route.get("name"),
                source_parent_id,
                RECOVERY_BUDGET_SECONDS,
            )
        if recovered:
            return recovered

        logger.warning(
            "[ALT REPLY UNRESOLVED] route=%s source=%s_%s child=%s parent=%s dest=%s_%s fallback=topic_root",
            route.get("name"),
            route.get("source_chat"),
            route.get("source_topic"),
            getattr(message, "id", None),
            source_parent_id,
            route.get("dest_chat"),
            route.get("dest_topic"),
        )

    return int(route["dest_topic"])


def install_alt_reply_hardening(worker, logger=None):
    """Self-test the reply readers and mark ALT reply recovery ready.

    ALT posts are sent by the delivery engine (engine/alt.py), which calls
    resolve_reply_target directly; nothing is patched.
    """
    logger = logger or getattr(worker, "log", None) or log

    existing = getattr(worker, "_ALT_REPLY_HARDENING_STATE", None)
    if existing:
        return existing

    required = (
        "client",
        "mapped_ids",
        "map_key",
        "message_map",
        "save_json",
        "MAP_FILE",
        "translated_payload",
        "real_media",
        "has_username_mention",
        "unit_has_username_mention",
        "log_username_filter_alt",
        "MEDIA_DIR",
    )
    missing = [name for name in required if not hasattr(worker, name)]
    if missing:
        raise RuntimeError(f"ALT reply hardening missing worker attributes: {missing}")

    # Regression tests: real parent must win; pure topic-root metadata must not
    # be treated as a reply to a message.
    direct = SimpleNamespace(reply_to_msg_id=123, reply_to_top_id=508, top_msg_id=None, reply_to=None)
    nested = SimpleNamespace(
        reply_to_msg_id=None,
        reply_to_top_id=None,
        top_msg_id=None,
        reply_to=SimpleNamespace(reply_to_msg_id=456, reply_to_top_id=508, top_msg_id=None),
    )
    root = SimpleNamespace(reply_to_msg_id=508, reply_to_top_id=508, top_msg_id=None, reply_to=None)
    test_route = {"source_topic": 508}
    if real_reply_source_ids(test_route, direct) != [123]:
        raise RuntimeError("ALT reply direct-ID self-test failed")
    if real_reply_source_ids(test_route, nested) != [456]:
        raise RuntimeError("ALT reply nested-ID self-test failed")
    if real_reply_source_ids(test_route, root) != []:
        raise RuntimeError("ALT reply topic-root self-test failed")

    state = {
        "direct_ids": True,
        "nested_header": True,
        "topic_root_filter": True,
        "local_persistent_map": True,
        "text_recovery": True,
        "media_recovery": True,
        "gethistory_first": True,
        "getreplies_last": True,
        "used_by": "engine.alt (resolve_reply_target)",
    }
    worker._ALT_REPLY_HARDENING_STATE = state

    logger.warning(
        "[ALT REPLY HARDENING V2 ACTIVE] all_routes=True direct_ids=True nested_header=True topic_root_filter=True local_maps=True text_recovery=True media_recovery=True gethistory_first=True getreplies_last=True singles=True albums=True"
    )
    return state

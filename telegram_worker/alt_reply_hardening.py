import asyncio
import logging
import re
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


log = logging.getLogger("alt-reply-hardening")

RECOVERY_CHAT_LIMIT = 700
RECOVERY_TOPIC_LIMIT = 180
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
    worker.message_map[
        worker.map_key(route, int(source_parent_id))
    ] = int(destination_parent_id)
    worker.save_json(worker.MAP_FILE, worker.message_map)


async def _destination_topic_messages(worker, route):
    """Use GetHistory first; GetReplies is last-resort only."""
    dest_chat = int(route["dest_chat"])
    dest_topic = int(route["dest_topic"])

    try:
        messages = list(
            await worker.client.get_messages(
                dest_chat,
                limit=RECOVERY_CHAT_LIMIT,
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
            await worker.client.get_messages(
                dest_chat,
                limit=RECOVERY_TOPIC_LIMIT,
                reply_to=dest_topic,
            )
            or []
        )
    except Exception:
        return []


async def _mapped_parent_that_exists(worker, route, source_parent_id: int) -> Optional[int]:
    for destination_id in worker.mapped_ids(route, source_parent_id):
        try:
            parent = await worker.client.get_messages(
                int(route["dest_chat"]),
                ids=int(destination_id),
            )
        except Exception:
            parent = None
        if parent:
            return int(destination_id)
    return None


async def resolve_reply_target(worker, route, message, logger=None) -> int:
    """Resolve source reply -> exact destination parent without flattening it."""
    logger = logger or log
    parent_ids = real_reply_source_ids(route, message)
    if not parent_ids:
        return int(route["dest_topic"])

    for source_parent_id in parent_ids:
        mapped = await _mapped_parent_that_exists(worker, route, source_parent_id)
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
            source_parent = await worker.client.get_messages(
                int(route["source_chat"]),
                ids=int(source_parent_id),
            )
        except Exception as exc:
            logger.warning(
                "[ALT REPLY PARENT FETCH FAILED] route=%s parent=%s %s: %s",
                route.get("name"),
                source_parent_id,
                type(exc).__name__,
                exc,
            )
            continue

        if not source_parent:
            continue

        # Translation routes must compare against the translated destination
        # caption/text, not the raw Arabic source text.
        try:
            expected_text, _expected_entities = await worker.translated_payload(
                route,
                source_parent,
            )
        except Exception:
            expected_text = _message_text(source_parent)

        expected_key = _normalise_text(expected_text)
        source_media_kind = _media_kind(source_parent)

        for attempt, delay in enumerate((0.0, 0.20, 0.60, 1.20), start=1):
            if delay:
                await asyncio.sleep(delay)

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
                matches = []
                max_age = 0
                mode = "none"

            if not matches:
                continue

            matches.sort(key=lambda candidate: _date_distance_seconds(source_parent, candidate))
            chosen = matches[0]
            distance = _date_distance_seconds(source_parent, chosen)
            if distance > max_age:
                break

            if mode == "media" and len(matches) > 1:
                second_distance = _date_distance_seconds(source_parent, matches[1])
                if abs(second_distance - distance) < 2.0:
                    logger.warning(
                        "[ALT REPLY MEDIA RECOVERY AMBIGUOUS] route=%s parent=%s candidates=%s",
                        route.get("name"),
                        source_parent_id,
                        len(matches),
                    )
                    break

            chosen_id = int(chosen.id)
            _cache_mapping(worker, route, source_parent_id, chosen_id)
            logger.warning(
                "[ALT REPLY RECOVERED] route=%s source=%s_%s child=%s parent=%s dest=%s_%s parent_dest=%s candidates=%s distance=%.1fs attempt=%s mode=%s",
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
                attempt,
                mode,
            )
            return chosen_id

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
    """Patch the ALT worker's send primitives before legacy main() starts."""
    logger = logger or getattr(worker, "log", None) or log

    existing = getattr(worker, "_ALT_REPLY_HARDENING_STATE", None)
    if existing:
        return existing

    required = (
        "client", "mapped_ids", "map_key", "message_map", "save_json", "MAP_FILE",
        "translated_payload", "real_media", "has_username_mention",
        "unit_has_username_mention", "log_username_filter_alt", "MEDIA_DIR",
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

    async def send_single(route, message):
        if worker.has_username_mention(message):
            worker.log_username_filter_alt([message], "send_single_hard_guard", route=route)
            return None

        target_reply = await resolve_reply_target(worker, route, message, logger=logger)
        text, entities = await worker.translated_payload(route, message)

        if not worker.real_media(message):
            if not text:
                return None
            sent = await worker.client.send_message(
                route["dest_chat"],
                text,
                formatting_entities=entities,
                parse_mode=None,
                reply_to=target_reply,
                link_preview=True,
            )
        else:
            try:
                sent = await worker.client.send_file(
                    route["dest_chat"],
                    message.media,
                    caption=text if text else None,
                    formatting_entities=entities if text else None,
                    parse_mode=None,
                    reply_to=target_reply,
                )
            except Exception as exc:
                logger.warning(
                    "[ALT DIRECT MEDIA FAILED - USING REUPLOAD] source_msg=%s %s: %s",
                    getattr(message, "id", None),
                    type(exc).__name__,
                    exc,
                )
                downloaded = await message.download_media(
                    file=str(worker.MEDIA_DIR / f"single_{message.id}")
                )
                if not downloaded:
                    raise RuntimeError("Media download fallback returned nothing")
                try:
                    sent = await worker.client.send_file(
                        route["dest_chat"],
                        downloaded,
                        caption=text if text else None,
                        formatting_entities=entities if text else None,
                        parse_mode=None,
                        reply_to=target_reply,
                    )
                finally:
                    try:
                        Path(downloaded).unlink(missing_ok=True)
                    except Exception:
                        pass

        if target_reply != int(route["dest_topic"]):
            logger.warning(
                "[ALT REPLY PRESERVED] route=%s source_child=%s dest_child=%s dest_parent=%s kind=SINGLE",
                route.get("name"),
                getattr(message, "id", None),
                getattr(sent, "id", None),
                target_reply,
            )
        return sent

    async def send_album(route, messages):
        messages = list(messages or [])
        if worker.unit_has_username_mention(messages):
            worker.log_username_filter_alt(messages, "send_album_hard_guard", route=route)
            return None

        probe = next(
            (m for m in messages if real_reply_source_ids(route, m)),
            messages[0] if messages else None,
        )
        target_reply = (
            await resolve_reply_target(worker, route, probe, logger=logger)
            if probe is not None
            else int(route["dest_topic"])
        )

        caption = ""
        caption_entities = None
        caption_message = next((m for m in messages if worker.text_of(m)), None)
        if caption_message is not None:
            caption, caption_entities = await worker.translated_payload(route, caption_message)

        media_objects = [m.media for m in messages if worker.real_media(m)]
        if not media_objects:
            return None

        try:
            sent = await worker.client.send_file(
                route["dest_chat"],
                media_objects,
                caption=caption if caption else None,
                formatting_entities=caption_entities if caption else None,
                parse_mode=None,
                reply_to=target_reply,
            )
            sent_items = sent if isinstance(sent, list) else [sent]
        except Exception as exc:
            logger.warning(
                "[ALT DIRECT ALBUM FAILED - USING REUPLOAD] %s: %s",
                type(exc).__name__,
                exc,
            )
            downloaded_files = []
            try:
                for message in messages:
                    if not worker.real_media(message):
                        continue
                    downloaded = await message.download_media(
                        file=str(worker.MEDIA_DIR / f"album_{message.id}")
                    )
                    if not downloaded:
                        raise RuntimeError("Album download returned nothing")
                    downloaded_files.append(downloaded)

                sent = await worker.client.send_file(
                    route["dest_chat"],
                    downloaded_files,
                    caption=caption if caption else None,
                    formatting_entities=caption_entities if caption else None,
                    parse_mode=None,
                    reply_to=target_reply,
                )
                sent_items = sent if isinstance(sent, list) else [sent]
            finally:
                for filename in downloaded_files:
                    try:
                        Path(filename).unlink(missing_ok=True)
                    except Exception:
                        pass

        if target_reply != int(route["dest_topic"]):
            logger.warning(
                "[ALT REPLY PRESERVED] route=%s source_child=%s dest_parent=%s kind=ALBUM items=%s",
                route.get("name"),
                getattr(probe, "id", None),
                target_reply,
                len(sent_items),
            )
        return sent_items

    worker.send_single = send_single
    worker.send_album = send_album

    state = {
        "direct_ids": True,
        "nested_header": True,
        "topic_root_filter": True,
        "local_persistent_map": True,
        "text_recovery": True,
        "media_recovery": True,
        "gethistory_first": True,
        "getreplies_last": True,
        "send_single": True,
        "send_album": True,
    }
    worker._ALT_REPLY_HARDENING_STATE = state

    logger.warning(
        "[ALT REPLY HARDENING V2 ACTIVE] all_routes=True direct_ids=True nested_header=True topic_root_filter=True local_maps=True text_recovery=True media_recovery=True gethistory_first=True getreplies_last=True singles=True albums=True"
    )
    return state

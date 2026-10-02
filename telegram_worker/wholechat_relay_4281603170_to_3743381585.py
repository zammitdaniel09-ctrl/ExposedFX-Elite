"""Whole-chat relay: -1004281603170 -> -1003743381585.

Owner-requested behavior:
- mirror every new copyable message from the whole source chat to the whole
  destination chat (no forum topic target);
- on first successful startup, import the latest 50 logical source posts,
  oldest -> newest;
- albums count as one logical post and are never intentionally split;
- preserve text/captions/entities/media and mapped replies where possible;
- persist source->destination message IDs so restarts do not duplicate the
  one-time bootstrap and future replies can target mirrored parents.

This relay intentionally lives outside ROUTES because the main route engine is
forum-topic oriented and assumes integer destination topic IDs in several
places. A whole-chat destination has no topic ID.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError

log = logging.getLogger("wholechat-4281603170-to-3743381585")

SOURCE_CHAT = -1004281603170
DEST_CHAT = -1003743381585
LAST50_COUNT = 50
SOURCE_FETCH_LIMIT = 250

STATE_FILENAME = "wholechat_4281603170_to_3743381585_last50_v1.json"
MAP_FILENAME = "wholechat_4281603170_to_3743381585_map_v1.json"


def _data_dir(main_module):
    value = Path(getattr(main_module, "DATA_DIR", Path("./data")))
    value.mkdir(parents=True, exist_ok=True)
    return value


def _load_json(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def _message_text(message):
    return (
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    )


def _entities(message):
    return getattr(message, "entities", None)


def _is_copyable(message):
    return bool(_message_text(message) or getattr(message, "media", None))


def _source_reply_ids(message):
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return []
    out = []
    for attr in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
        try:
            value = int(getattr(reply, attr, 0) or 0)
        except Exception:
            value = 0
        if value and value not in out:
            out.append(value)
    return out


def _normalise_dest_ids(value):
    if isinstance(value, list):
        out = []
        for item in value:
            try:
                item = int(item)
            except Exception:
                continue
            if item and item not in out:
                out.append(item)
        return out
    try:
        value = int(value)
    except Exception:
        return []
    return [value] if value else []


def _mapped_reply_target(message, message_map):
    for source_parent in _source_reply_ids(message):
        ids = _normalise_dest_ids(message_map.get(str(source_parent)))
        if ids:
            return ids[0]
    return None


def _unit_ids(unit):
    out = []
    for message in unit:
        try:
            value = int(getattr(message, "id", 0) or 0)
        except Exception:
            value = 0
        if value:
            out.append(value)
    return out


def _unit_already_mapped(unit, message_map):
    ids = _unit_ids(unit)
    return bool(ids) and all(
        _normalise_dest_ids(message_map.get(str(mid)))
        for mid in ids
    )


def _build_units(messages):
    messages = [message for message in messages if _is_copyable(message)]
    messages.sort(key=lambda message: int(getattr(message, "id", 0) or 0))

    grouped = {}
    units = []
    used_groups = set()

    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            grouped.setdefault(grouped_id, []).append(message)

    for message in messages:
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            if grouped_id in used_groups:
                continue
            unit = sorted(
                grouped.get(grouped_id, [message]),
                key=lambda item: int(getattr(item, "id", 0) or 0),
            )
            used_groups.add(grouped_id)
            units.append(unit)
        else:
            units.append([message])

    return units[-LAST50_COUNT:]


async def _with_floodwait(call, logger, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[WHOLECHAT FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label,
                attempt,
                attempts,
                wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError("Flood-wait retry budget exhausted: " + label)


async def _reload_unit(client, ids, logger):
    messages = await _with_floodwait(
        lambda: client.get_messages(
            SOURCE_CHAT,
            ids=[int(value) for value in ids],
        ),
        logger,
        "reload:" + ",".join(str(v) for v in ids),
    )
    if not isinstance(messages, list):
        messages = [messages] if messages is not None else []
    messages = [message for message in messages if message is not None]
    messages.sort(key=lambda message: int(getattr(message, "id", 0) or 0))
    return messages


def _remember_sent(unit, sent, message_map):
    sent_items = sent if isinstance(sent, list) else [sent]
    sent_items = [item for item in sent_items if item is not None]
    sent_ids = [
        int(getattr(item, "id", 0) or 0)
        for item in sent_items
        if int(getattr(item, "id", 0) or 0)
    ]
    if not sent_ids:
        return

    if len(sent_items) == len(unit):
        for source, destination in zip(unit, sent_items):
            sid = int(getattr(source, "id", 0) or 0)
            did = int(getattr(destination, "id", 0) or 0)
            if sid and did:
                message_map[str(sid)] = did
        return

    for source in unit:
        sid = int(getattr(source, "id", 0) or 0)
        if sid:
            message_map[str(sid)] = (
                sent_ids[0] if len(sent_ids) == 1 else sent_ids
            )


async def _send_single(client, message, message_map, data_dir, logger):
    text = _message_text(message)
    entities = _entities(message)
    reply_to = _mapped_reply_target(message, message_map)

    if getattr(message, "media", None):
        try:
            return await _with_floodwait(
                lambda: client.send_file(
                    DEST_CHAT,
                    message.media,
                    caption=text or None,
                    formatting_entities=entities if text else None,
                    parse_mode=None,
                    reply_to=reply_to,
                ),
                logger,
                "media:" + str(getattr(message, "id", None)),
            )
        except Exception as exc:
            logger.warning(
                "[WHOLECHAT MEDIA DIRECT FAILED] source_msg=%s %s: %s",
                getattr(message, "id", None),
                type(exc).__name__,
                exc,
            )
            if reply_to is not None:
                try:
                    return await _with_floodwait(
                        lambda: client.send_file(
                            DEST_CHAT,
                            message.media,
                            caption=text or None,
                            formatting_entities=entities if text else None,
                            parse_mode=None,
                            reply_to=None,
                        ),
                        logger,
                        "media-no-reply:" + str(getattr(message, "id", None)),
                    )
                except Exception:
                    pass

        cache_dir = data_dir / "wholechat_media_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        downloaded = await message.download_media(
            file=str(
                cache_dir
                / f"{SOURCE_CHAT}_{getattr(message, 'id', 0)}"
            )
        )
        if not downloaded:
            raise RuntimeError(
                "Could not download media for source message "
                + str(getattr(message, "id", None))
            )
        try:
            return await _with_floodwait(
                lambda: client.send_file(
                    DEST_CHAT,
                    downloaded,
                    caption=text or None,
                    formatting_entities=entities if text else None,
                    parse_mode=None,
                    reply_to=reply_to,
                ),
                logger,
                "media-reupload:" + str(getattr(message, "id", None)),
            )
        finally:
            try:
                Path(downloaded).unlink(missing_ok=True)
            except Exception:
                pass

    if not text:
        return None

    try:
        return await _with_floodwait(
            lambda: client.send_message(
                DEST_CHAT,
                text,
                formatting_entities=entities,
                parse_mode=None,
                reply_to=reply_to,
                link_preview=True,
            ),
            logger,
            "text:" + str(getattr(message, "id", None)),
        )
    except Exception as exc:
        if reply_to is None:
            raise
        logger.warning(
            "[WHOLECHAT REPLY FALLBACK] source_msg=%s mode=text error=%s",
            getattr(message, "id", None),
            type(exc).__name__,
        )
        return await _with_floodwait(
            lambda: client.send_message(
                DEST_CHAT,
                text,
                formatting_entities=entities,
                parse_mode=None,
                reply_to=None,
                link_preview=True,
            ),
            logger,
            "text-no-reply:" + str(getattr(message, "id", None)),
        )


async def _send_album(client, unit, message_map, data_dir, logger):
    files = [
        message.media
        for message in unit
        if getattr(message, "media", None)
    ]
    if not files:
        return await _send_single(
            client,
            unit[0],
            message_map,
            data_dir,
            logger,
        )

    caption_message = next(
        (message for message in unit if _message_text(message)),
        unit[0],
    )
    caption = _message_text(caption_message)
    entities = _entities(caption_message)
    reply_to = _mapped_reply_target(unit[0], message_map)
    ids_text = ",".join(str(v) for v in _unit_ids(unit))

    try:
        return await _with_floodwait(
            lambda: client.send_file(
                DEST_CHAT,
                files,
                caption=caption or None,
                formatting_entities=entities if caption else None,
                parse_mode=None,
                reply_to=reply_to,
            ),
            logger,
            "album:" + ids_text,
        )
    except Exception as exc:
        logger.warning(
            "[WHOLECHAT ALBUM DIRECT FAILED] source_ids=%s %s: %s",
            _unit_ids(unit),
            type(exc).__name__,
            exc,
        )
        if reply_to is not None:
            try:
                return await _with_floodwait(
                    lambda: client.send_file(
                        DEST_CHAT,
                        files,
                        caption=caption or None,
                        formatting_entities=entities if caption else None,
                        parse_mode=None,
                        reply_to=None,
                    ),
                    logger,
                    "album-no-reply:" + ids_text,
                )
            except Exception:
                pass

    cache_dir = data_dir / "wholechat_media_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    downloaded = []
    try:
        for message in unit:
            if not getattr(message, "media", None):
                continue
            path = await message.download_media(
                file=str(
                    cache_dir
                    / f"album_{SOURCE_CHAT}_{getattr(message, 'id', 0)}"
                )
            )
            if path:
                downloaded.append(path)

        if not downloaded:
            raise RuntimeError(
                "Album download returned no files source_ids="
                + str(_unit_ids(unit))
            )

        return await _with_floodwait(
            lambda: client.send_file(
                DEST_CHAT,
                downloaded,
                caption=caption or None,
                formatting_entities=entities if caption else None,
                parse_mode=None,
                reply_to=reply_to,
            ),
            logger,
            "album-reupload:" + ids_text,
        )
    finally:
        for path in downloaded:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


async def _deliver_unit(
    client,
    unit,
    message_map,
    map_path,
    data_dir,
    lock,
    logger,
    reason,
):
    if not unit:
        return "empty"

    async with lock:
        if _unit_already_mapped(unit, message_map):
            logger.info(
                "[WHOLECHAT ALREADY MAPPED] reason=%s source_ids=%s",
                reason,
                _unit_ids(unit),
            )
            return "existing"

        if len(unit) > 1 and getattr(unit[0], "grouped_id", None):
            sent = await _send_album(
                client,
                unit,
                message_map,
                data_dir,
                logger,
            )
        else:
            sent = await _send_single(
                client,
                unit[0],
                message_map,
                data_dir,
                logger,
            )

        if sent is None:
            logger.info(
                "[WHOLECHAT SKIP UNSUPPORTED] reason=%s source_ids=%s",
                reason,
                _unit_ids(unit),
            )
            return "skipped"

        _remember_sent(unit, sent, message_map)
        _save_json(map_path, message_map)
        logger.warning(
            "[WHOLECHAT SENT] reason=%s source=%s dest=%s source_ids=%s",
            reason,
            SOURCE_CHAT,
            DEST_CHAT,
            _unit_ids(unit),
        )
        return "sent"


async def run_wholechat_4281603170_to_3743381585(
    main_module,
    logger=None,
):
    logger = logger or log
    client = getattr(main_module, "client", None)
    if client is None:
        raise RuntimeError("Main Telegram client is unavailable")

    data_dir = _data_dir(main_module)
    state_path = data_dir / STATE_FILENAME
    map_path = data_dir / MAP_FILENAME
    state = _load_json(state_path)
    message_map = _load_json(map_path)

    lock = asyncio.Lock()
    bootstrap_ready = asyncio.Event()

    source_entity = await _with_floodwait(
        lambda: client.get_entity(SOURCE_CHAT),
        logger,
        "source-access",
    )
    dest_entity = await _with_floodwait(
        lambda: client.get_entity(DEST_CHAT),
        logger,
        "dest-access",
    )

    logger.warning(
        "[WHOLECHAT RELAY ACCESS OK] source=%s title=%r dest=%s title=%r",
        SOURCE_CHAT,
        getattr(source_entity, "title", None),
        DEST_CHAT,
        getattr(dest_entity, "title", None),
    )

    async def on_new_message(event):
        try:
            message = event.message
            if getattr(message, "grouped_id", None):
                return
            await bootstrap_ready.wait()
            await _deliver_unit(
                client,
                [message],
                message_map,
                map_path,
                data_dir,
                lock,
                logger,
                "live",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "[WHOLECHAT LIVE FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    async def on_album(event):
        try:
            await bootstrap_ready.wait()
            unit = list(getattr(event, "messages", None) or [])
            await _deliver_unit(
                client,
                unit,
                message_map,
                map_path,
                data_dir,
                lock,
                logger,
                "live-album",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "[WHOLECHAT LIVE ALBUM FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    client.add_event_handler(
        on_new_message,
        events.NewMessage(chats=SOURCE_CHAT),
    )
    client.add_event_handler(
        on_album,
        events.Album(chats=SOURCE_CHAT),
    )

    setattr(
        main_module,
        "WHOLECHAT_4281603170_NEW_HANDLER",
        on_new_message,
    )
    setattr(
        main_module,
        "WHOLECHAT_4281603170_ALBUM_HANDLER",
        on_album,
    )

    if state.get("status") == "done":
        bootstrap_ready.set()
        logger.warning(
            "[WHOLECHAT LAST50 STATE SKIP] already_done=True "
            "selected=%s sent=%s existing=%s skipped=%s",
            state.get("selected", 0),
            state.get("sent", 0),
            state.get("existing", 0),
            state.get("skipped", 0),
        )
        logger.warning(
            "[WHOLECHAT RELAY READY] source=%s dest=%s "
            "live=True last50_complete=True",
            SOURCE_CHAT,
            DEST_CHAT,
        )
        return {
            "source": SOURCE_CHAT,
            "dest": DEST_CHAT,
            "live": True,
            "last50_complete": True,
        }

    selected_units = (
        state.get("selected_units")
        if state.get("status") == "running"
        else None
    )

    if not selected_units:
        history = await _with_floodwait(
            lambda: client.get_messages(
                SOURCE_CHAT,
                limit=SOURCE_FETCH_LIMIT,
            ),
            logger,
            "source-last50-history",
        )
        units = _build_units(list(history or []))
        selected_units = [_unit_ids(unit) for unit in units]
        selected_units = [ids for ids in selected_units if ids]

        if not selected_units:
            bootstrap_ready.set()
            raise RuntimeError(
                "Source returned no copyable messages for last-50 bootstrap"
            )

        state = {
            "status": "running",
            "source": SOURCE_CHAT,
            "dest": DEST_CHAT,
            "selected_units": selected_units,
            "completed": [],
            "selected": len(selected_units),
            "sent": 0,
            "existing": 0,
            "skipped": 0,
            "started_at": time.time(),
        }
        _save_json(state_path, state)

        logger.warning(
            "[WHOLECHAT LAST50 START] source=%s dest=%s "
            "selected_posts=%s albums_one_post=True oldest_to_newest=True",
            SOURCE_CHAT,
            DEST_CHAT,
            len(selected_units),
        )

    completed = set(str(value) for value in (state.get("completed") or []))
    sent_count = int(state.get("sent", 0) or 0)
    existing_count = int(state.get("existing", 0) or 0)
    skipped_count = int(state.get("skipped", 0) or 0)

    try:
        for index, ids in enumerate(selected_units, start=1):
            token = ",".join(str(int(value)) for value in ids)
            if token in completed:
                continue

            unit = await _reload_unit(client, ids, logger)
            if not unit:
                skipped_count += 1
                outcome = "skipped"
                logger.warning(
                    "[WHOLECHAT LAST50 SOURCE MISSING] "
                    "post=%s/%s source_ids=%s",
                    index,
                    len(selected_units),
                    ids,
                )
            else:
                outcome = await _deliver_unit(
                    client,
                    unit,
                    message_map,
                    map_path,
                    data_dir,
                    lock,
                    logger,
                    "last50",
                )
                if outcome == "sent":
                    sent_count += 1
                elif outcome == "existing":
                    existing_count += 1
                else:
                    skipped_count += 1

            completed.add(token)
            state.update(
                {
                    "completed": sorted(completed),
                    "sent": sent_count,
                    "existing": existing_count,
                    "skipped": skipped_count,
                    "updated_at": time.time(),
                    "last_outcome": outcome,
                }
            )
            _save_json(state_path, state)
            await asyncio.sleep(0.35)

        state.update(
            {
                "status": "done",
                "completed_at": time.time(),
                "selected": len(selected_units),
                "sent": sent_count,
                "existing": existing_count,
                "skipped": skipped_count,
            }
        )
        _save_json(state_path, state)

        logger.warning(
            "[WHOLECHAT LAST50 DONE] source=%s dest=%s "
            "selected=%s sent=%s existing=%s skipped=%s",
            SOURCE_CHAT,
            DEST_CHAT,
            len(selected_units),
            sent_count,
            existing_count,
            skipped_count,
        )
    finally:
        bootstrap_ready.set()

    logger.warning(
        "[WHOLECHAT RELAY READY] source=%s dest=%s "
        "live=True last50_complete=%s",
        SOURCE_CHAT,
        DEST_CHAT,
        state.get("status") == "done",
    )

    return {
        "source": SOURCE_CHAT,
        "dest": DEST_CHAT,
        "live": True,
        "last50_complete": state.get("status") == "done",
    }

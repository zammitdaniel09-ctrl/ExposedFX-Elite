"""Whole-chat relay: -1003743381585 -> -1003216105903.

Owner-requested behavior:
- mirror every new copyable message from the whole source chat to the whole
  destination chat;
- send exactly ONE historical logical post on first successful startup: the
  latest copyable source post (an album counts as one post);
- preserve source text, entities/custom emoji, captions and media exactly;
- preserve replies when the replied-to source message has already been mapped;
- persist bootstrap and message-map state so restarts do not resend history.

This intentionally lives outside ROUTES because both ends are whole chats and
there is no destination forum-topic id.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError


log = logging.getLogger("wholechat-3743381585-to-3216105903")

SOURCE_CHAT = -1003743381585
DEST_CHAT = -1003216105903
SOURCE_FETCH_LIMIT = 80

STATE_FILENAME = "wholechat_3743381585_to_3216105903_last1_v1.json"
MAP_FILENAME = "wholechat_3743381585_to_3216105903_map_v1.json"


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


def _text(message):
    return (
        getattr(message, "message", None)
        or getattr(message, "raw_text", None)
        or getattr(message, "text", None)
        or ""
    )


def _copyable(message):
    return bool(_text(message) or getattr(message, "media", None))


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


def _normalise_ids(value):
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


def _reply_target(message, message_map):
    for source_parent in _source_reply_ids(message):
        ids = _normalise_ids(message_map.get(str(source_parent)))
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


def _unit_mapped(unit, message_map):
    ids = _unit_ids(unit)
    return bool(ids) and all(
        _normalise_ids(message_map.get(str(mid)))
        for mid in ids
    )


def _latest_unit(messages):
    messages = [message for message in messages if _copyable(message)]
    messages.sort(
        key=lambda message: int(getattr(message, "id", 0) or 0)
    )
    if not messages:
        return []

    latest = messages[-1]
    grouped_id = getattr(latest, "grouped_id", None)
    if not grouped_id:
        return [latest]

    unit = [
        message
        for message in messages
        if getattr(message, "grouped_id", None) == grouped_id
    ]
    unit.sort(
        key=lambda message: int(getattr(message, "id", 0) or 0)
    )
    return unit


async def _with_floodwait(call, logger, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[DOWNSTREAM RELAY FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label,
                attempt,
                attempts,
                wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(
        "Flood-wait retry budget exhausted: " + label
    )


async def _reload_unit(client, ids, logger):
    messages = await _with_floodwait(
        lambda: client.get_messages(
            SOURCE_CHAT,
            ids=[int(value) for value in ids],
        ),
        logger,
        "reload:" + ",".join(str(value) for value in ids),
    )
    if not isinstance(messages, list):
        messages = [messages] if messages is not None else []
    messages = [
        message for message in messages if message is not None
    ]
    messages.sort(
        key=lambda message: int(getattr(message, "id", 0) or 0)
    )
    return messages


def _remember(unit, sent, message_map):
    sent_items = sent if isinstance(sent, list) else [sent]
    sent_items = [item for item in sent_items if item is not None]
    dest_ids = [
        int(getattr(item, "id", 0) or 0)
        for item in sent_items
        if int(getattr(item, "id", 0) or 0)
    ]
    if not dest_ids:
        return

    if len(sent_items) == len(unit):
        for source, destination in zip(unit, sent_items):
            sid = int(getattr(source, "id", 0) or 0)
            did = int(getattr(destination, "id", 0) or 0)
            if sid and did:
                message_map[str(sid)] = did
        return

    value = dest_ids[0] if len(dest_ids) == 1 else dest_ids
    for source in unit:
        sid = int(getattr(source, "id", 0) or 0)
        if sid:
            message_map[str(sid)] = value


async def _send_single(
    client,
    message,
    message_map,
    data_dir,
    logger,
):
    text = _text(message)
    entities = getattr(message, "entities", None)
    reply_to = _reply_target(message, message_map)

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
                "media:" + str(getattr(message, "id", 0)),
            )
        except Exception as exc:
            logger.warning(
                "[DOWNSTREAM MEDIA DIRECT FAILED] source_msg=%s %s: %s",
                getattr(message, "id", None),
                type(exc).__name__,
                exc,
            )

        cache_dir = data_dir / "downstream_3216105903_media_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        downloaded = await message.download_media(
            file=str(
                cache_dir
                / f"{SOURCE_CHAT}_{getattr(message, 'id', 0)}"
            )
        )
        if not downloaded:
            raise RuntimeError(
                "Could not download source media msg="
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
                "media-reupload:" + str(getattr(message, "id", 0)),
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
            "text:" + str(getattr(message, "id", 0)),
        )
    except Exception:
        if reply_to is None:
            raise
        logger.warning(
            "[DOWNSTREAM REPLY FALLBACK] source_msg=%s",
            getattr(message, "id", None),
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
            "text-no-reply:" + str(getattr(message, "id", 0)),
        )


async def _send_album(
    client,
    unit,
    message_map,
    data_dir,
    logger,
):
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
        (message for message in unit if _text(message)),
        unit[0],
    )
    caption = _text(caption_message)
    entities = getattr(caption_message, "entities", None)
    reply_to = _reply_target(unit[0], message_map)
    ids_text = ",".join(str(value) for value in _unit_ids(unit))

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
            "[DOWNSTREAM ALBUM DIRECT FAILED] source_ids=%s %s: %s",
            _unit_ids(unit),
            type(exc).__name__,
            exc,
        )

    cache_dir = data_dir / "downstream_3216105903_media_cache"
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
                "Album fallback downloaded no files source_ids="
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


async def _deliver(
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
        if _unit_mapped(unit, message_map):
            logger.info(
                "[DOWNSTREAM ALREADY MAPPED] reason=%s source_ids=%s",
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
                "[DOWNSTREAM SKIP UNSUPPORTED] reason=%s source_ids=%s",
                reason,
                _unit_ids(unit),
            )
            return "skipped"

        _remember(unit, sent, message_map)
        _save_json(map_path, message_map)

        logger.warning(
            "[DOWNSTREAM SENT] reason=%s source=%s dest=%s source_ids=%s",
            reason,
            SOURCE_CHAT,
            DEST_CHAT,
            _unit_ids(unit),
        )
        return "sent"


async def run_wholechat_3743381585_to_3216105903(
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
        "[DOWNSTREAM RELAY ACCESS OK] source=%s title=%r dest=%s title=%r",
        SOURCE_CHAT,
        getattr(source_entity, "title", None),
        DEST_CHAT,
        getattr(dest_entity, "title", None),
    )

    lock = asyncio.Lock()
    bootstrap_ready = asyncio.Event()

    async def on_new_message(event):
        try:
            message = event.message
            if getattr(message, "grouped_id", None):
                return
            await bootstrap_ready.wait()
            await _deliver(
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
                "[DOWNSTREAM LIVE FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    async def on_album(event):
        try:
            await bootstrap_ready.wait()
            await _deliver(
                client,
                list(getattr(event, "messages", None) or []),
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
                "[DOWNSTREAM LIVE ALBUM FAILED] %s: %s",
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
        "WHOLECHAT_3743381585_NEW_HANDLER",
        on_new_message,
    )
    setattr(
        main_module,
        "WHOLECHAT_3743381585_ALBUM_HANDLER",
        on_album,
    )

    if state.get("status") == "done":
        bootstrap_ready.set()
        logger.warning(
            "[DOWNSTREAM LAST1 STATE SKIP] already_done=True "
            "source_ids=%s outcome=%s",
            state.get("source_ids"),
            state.get("outcome"),
        )
        logger.warning(
            "[DOWNSTREAM RELAY READY] source=%s dest=%s "
            "live=True past_messages=1 bootstrap_complete=True",
            SOURCE_CHAT,
            DEST_CHAT,
        )
        return state

    try:
        selected_ids = state.get("source_ids")

        if not selected_ids:
            history = await _with_floodwait(
                lambda: client.get_messages(
                    SOURCE_CHAT,
                    limit=SOURCE_FETCH_LIMIT,
                ),
                logger,
                "source-last1-history",
            )
            unit = _latest_unit(list(history or []))
            selected_ids = _unit_ids(unit)

            if not selected_ids:
                raise RuntimeError(
                    "Source returned no copyable historical message"
                )

            state = {
                "status": "running",
                "source": SOURCE_CHAT,
                "dest": DEST_CHAT,
                "source_ids": selected_ids,
                "selected_posts": 1,
                "started_at": time.time(),
            }
            _save_json(state_path, state)

            logger.warning(
                "[DOWNSTREAM LAST1 SELECTED] source=%s dest=%s "
                "source_ids=%s albums_one_post=True",
                SOURCE_CHAT,
                DEST_CHAT,
                selected_ids,
            )

        unit = await _reload_unit(client, selected_ids, logger)
        if not unit:
            raise RuntimeError(
                "Could not reload selected historical source post"
            )

        outcome = await _deliver(
            client,
            unit,
            message_map,
            map_path,
            data_dir,
            lock,
            logger,
            "last1",
        )

        state.update(
            {
                "status": "done",
                "outcome": outcome,
                "completed_at": time.time(),
            }
        )
        _save_json(state_path, state)

        logger.warning(
            "[DOWNSTREAM LAST1 DONE] source=%s dest=%s "
            "source_ids=%s outcome=%s exactly_one_past_post=True",
            SOURCE_CHAT,
            DEST_CHAT,
            selected_ids,
            outcome,
        )
    finally:
        bootstrap_ready.set()

    logger.warning(
        "[DOWNSTREAM RELAY READY] source=%s dest=%s "
        "live=True past_messages=1 bootstrap_complete=%s",
        SOURCE_CHAT,
        DEST_CHAT,
        state.get("status") == "done",
    )
    return state

"""Whole-chat relay: -1004281603170 -> -1003743381585.

Owner-requested behavior:
- mirror every new copyable message from the whole source chat to the whole
  destination chat (no forum topic target);
- on first successful startup, import the latest 50 logical source posts,
  oldest -> newest;
- albums count as one logical post and are never intentionally split;
- preserve text/captions/entities/media and mapped replies where possible;
- persist source->destination message IDs so restarts do not duplicate the
  one-time bootstrap and future replies can target mirrored parents;
- catch up (4 Nov 2026): live delivery is push only, so a post made while the
  worker was down (every deploy, any disconnect) or whose live send failed is
  read back from a durable checkpoint every CATCHUP_SECONDS and sent, unless
  it is older than STALE_SECONDS (reported, like the main intake).

This relay intentionally lives outside ROUTES because the main route engine is
forum-topic oriented and assumes integer destination topic IDs in several
places. A whole-chat destination has no topic ID.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon.errors import ChannelPrivateError
from telethon import events
from telethon.errors import FloodWaitError, MessageNotModifiedError

from telegram_worker import route_table
from telegram_worker.samgtrades_house_formatter import (
    format_samgtrades_text,
    parse_samgtrades_signal,
    registry_ready,
    run_self_test as run_samgtrades_formatter_self_test,
)
from telegram_worker.state_store import SENDS

log = logging.getLogger("wholechat-4281603170-to-3743381585")

_ROUTE = route_table.one("SamGtrades To Exposed VIP")
ROUTE_NAME = _ROUTE.name
SOURCE_CHAT = _ROUTE.source_chat
DEST_CHAT = _ROUTE.dest_chat
LAST50_COUNT = 50
SOURCE_FETCH_LIMIT = 250

STATE_FILENAME = "wholechat_4281603170_to_3743381585_last50_v1.json"
MAP_FILENAME = "wholechat_4281603170_to_3743381585_map_v1.json"
RESTYLE_FILENAME = "wholechat_4281603170_to_3743381585_restyle_last100_v1.json"
RESTYLE_COUNT = 100

CATCHUP_FILENAME = "wholechat_4281603170_to_3743381585_catchup_v1.json"
CATCHUP_SECONDS = 30
CATCHUP_PAGE = 50
STALE_SECONDS = 600  # same rule as the main intake: a late signal misleads members
GIVE_UP_SECONDS = 3600
ALBUM_SETTLE_SECONDS = 2.0
ALBUM_BUFFER_SECONDS = 60

NEW_HANDLER_ATTR = "WHOLECHAT_4281603170_NEW_HANDLER"
ALBUM_HANDLER_ATTR = "WHOLECHAT_4281603170_ALBUM_HANDLER"
CATCHUP_TASK_ATTR = "WHOLECHAT_4281603170_CATCHUP_TASK"
ALBUM_SWEEP_TASK_ATTR = "WHOLECHAT_4281603170_ALBUM_SWEEP_TASK"

# Source signal texts seen by this process, so a TP/SL reply can recover its
# signal's RR without a Telegram round-trip.
_SIGNAL_TEXTS = {}
_SIGNAL_TEXTS_MAX = 500


def _remember_signal_text(message):
    text = _message_text(message)
    if parse_samgtrades_signal(text):
        _SIGNAL_TEXTS[int(message.id)] = text
        while len(_SIGNAL_TEXTS) > _SIGNAL_TEXTS_MAX:
            _SIGNAL_TEXTS.pop(next(iter(_SIGNAL_TEXTS)))


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
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
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
    return bool(ids) and all(_normalise_dest_ids(message_map.get(str(mid))) for mid in ids)


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
                grouped.get(grouped_id, [message]), key=lambda item: int(getattr(item, "id", 0) or 0)
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
                "[WHOLECHAT FLOODWAIT] label=%s attempt=%s/%s wait=%ss", label, attempt, attempts, wait_for
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError("Flood-wait retry budget exhausted: " + label)


async def _reload_unit(client, ids, logger):
    messages = await _with_floodwait(
        lambda: client.get_messages(SOURCE_CHAT, ids=[int(value) for value in ids]),
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
    sent_ids = [int(getattr(item, "id", 0) or 0) for item in sent_items if int(getattr(item, "id", 0) or 0)]
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
            message_map[str(sid)] = sent_ids[0] if len(sent_ids) == 1 else sent_ids


async def _resolve_parent_signal_text(client, message, logger, max_hops=4):
    """Recover the original SamGtrades signal behind an update reply.

    Updates normally reply directly to the source signal, but we follow a few
    parent hops so RR recovery stays correct even if Telegram/provider replies
    to an intermediate update.
    """
    current = message
    seen = set()

    for hop in range(max_hops):
        candidates = _source_reply_ids(current)
        parent_id = next((value for value in candidates if value not in seen), None)
        if not parent_id:
            return None

        seen.add(parent_id)
        if int(parent_id) in _SIGNAL_TEXTS:
            return _SIGNAL_TEXTS[int(parent_id)]

        try:
            parent = await _with_floodwait(
                lambda: client.get_messages(SOURCE_CHAT, ids=int(parent_id)), logger, f"rr-parent:{parent_id}"
            )
        except Exception as exc:
            logger.warning(
                "[WHOLECHAT RR PARENT FETCH FAILED] source_msg=%s parent=%s %s: %s",
                getattr(message, "id", None),
                parent_id,
                type(exc).__name__,
                exc,
            )
            return None

        if isinstance(parent, list):
            parent = parent[0] if parent else None
        if parent is None:
            return None

        parent_text = _message_text(parent)
        if parse_samgtrades_signal(parent_text):
            logger.info(
                "[WHOLECHAT RR PARENT RESOLVED] source_msg=%s parent=%s hop=%s",
                getattr(message, "id", None),
                parent_id,
                hop + 1,
            )
            return parent_text

        current = parent

    return None


async def _send_single(client, message, message_map, data_dir, registry, logger):
    text = _message_text(message)
    entities = _entities(message)
    _remember_signal_text(message)
    parent_text = None
    if not parse_samgtrades_signal(text) and _source_reply_ids(message):
        parent_text = await _resolve_parent_signal_text(client, message, logger)
    text, entities, format_kind = format_samgtrades_text(text, entities, registry, parent_text=parent_text)
    if format_kind:
        logger.warning(
            "[WHOLECHAT HOUSE FORMAT] kind=%s source_msg=%s source=%s dest=%s",
            format_kind,
            getattr(message, "id", None),
            SOURCE_CHAT,
            DEST_CHAT,
        )
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
            file=str(cache_dir / f"{SOURCE_CHAT}_{getattr(message, 'id', 0)}")
        )
        if not downloaded:
            raise RuntimeError(
                "Could not download media for source message " + str(getattr(message, "id", None))
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


async def _send_album(client, unit, message_map, data_dir, registry, logger):
    files = [message.media for message in unit if getattr(message, "media", None)]
    if not files:
        return await _send_single(client, unit[0], message_map, data_dir, registry, logger)

    caption_message = next((message for message in unit if _message_text(message)), unit[0])
    caption = _message_text(caption_message)
    entities = _entities(caption_message)
    parent_text = None
    if not parse_samgtrades_signal(caption) and _source_reply_ids(caption_message):
        parent_text = await _resolve_parent_signal_text(client, caption_message, logger)
    caption, entities, format_kind = format_samgtrades_text(
        caption, entities, registry, parent_text=parent_text
    )
    if format_kind:
        logger.warning(
            "[WHOLECHAT HOUSE FORMAT] kind=%s source_msg=%s source=%s dest=%s album=True",
            format_kind,
            getattr(caption_message, "id", None),
            SOURCE_CHAT,
            DEST_CHAT,
        )
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
            "[WHOLECHAT ALBUM DIRECT FAILED] source_ids=%s %s: %s", _unit_ids(unit), type(exc).__name__, exc
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
                file=str(cache_dir / f"album_{SOURCE_CHAT}_{getattr(message, 'id', 0)}")
            )
            if path:
                downloaded.append(path)

        if not downloaded:
            raise RuntimeError("Album download returned no files source_ids=" + str(_unit_ids(unit)))

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


async def _deliver_unit(client, unit, message_map, map_path, data_dir, registry, lock, logger, reason):
    if not unit:
        return "empty"

    async with lock:
        if _unit_already_mapped(unit, message_map):
            logger.info("[WHOLECHAT ALREADY MAPPED] reason=%s source_ids=%s", reason, _unit_ids(unit))
            return "existing"

        # On SIGTERM the shutdown waits for this block, so a post Telegram
        # accepted is in the map before exit, and no new send starts.
        async with SENDS.sending():
            if len(unit) > 1 and getattr(unit[0], "grouped_id", None):
                sent = await _send_album(client, unit, message_map, data_dir, registry, logger)
            else:
                sent = await _send_single(client, unit[0], message_map, data_dir, registry, logger)

            if sent is None:
                logger.info("[WHOLECHAT SKIP UNSUPPORTED] reason=%s source_ids=%s", reason, _unit_ids(unit))
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


def _entity_signature(entities):
    return sorted(
        (
            type(entity).__name__,
            int(entity.offset),
            int(entity.length),
            int(getattr(entity, "document_id", 0) or 0),
        )
        for entity in entities or []
    )


async def _restyle_one(client, dest, source, registry, logger):
    if source is None:
        return "source_missing"
    text = _message_text(source)
    if not text:
        return "no_text"
    parent_text = None
    if not parse_samgtrades_signal(text) and _source_reply_ids(source):
        parent_text = await _resolve_parent_signal_text(client, source, logger)
    new_text, new_entities, kind = format_samgtrades_text(
        text, _entities(source), registry, parent_text=parent_text
    )
    if not kind:
        return "passthrough"
    if new_text == _message_text(dest) and _entity_signature(new_entities) == _entity_signature(
        _entities(dest)
    ):
        return "unchanged"
    try:
        await _with_floodwait(
            lambda: client.edit_message(DEST_CHAT, int(dest.id), new_text, formatting_entities=new_entities),
            logger,
            f"restyle:{dest.id}",
        )
    except MessageNotModifiedError:
        return "unchanged"
    except Exception as exc:
        logger.warning("[WHOLECHAT RESTYLE SKIPPED] dest_msg=%s %s: %s", dest.id, type(exc).__name__, exc)
        return "not_editable"
    return "edited"


async def _restyle_recent_once(client, message_map, data_dir, registry, logger):
    """One-time owner request (3 Nov 2026): re-render the latest 100 posts in
    the destination in the new library style. Only posts this relay formats
    (signals and updates) are edited; passthrough posts stay as they are.
    Progress is durable, so a restart resumes without editing twice."""
    path = data_dir / RESTYLE_FILENAME
    state = _load_json(path)
    if state.get("status") == "done":
        return state
    done = set(str(value) for value in state.get("done") or [])
    counts = dict(state.get("counts") or {})

    by_dest = {}
    for source_id, value in list(message_map.items()):
        ids = _normalise_dest_ids(value)
        if len(ids) == 1:
            by_dest[ids[0]] = int(source_id)

    recent = await _with_floodwait(
        lambda: client.get_messages(DEST_CHAT, limit=RESTYLE_COUNT), logger, "restyle-dest"
    )
    recent = sorted(
        (m for m in recent or [] if m is not None and int(m.id) in by_dest), key=lambda m: int(m.id)
    )
    source_ids = [by_dest[int(m.id)] for m in recent]
    sources = {}
    if source_ids:
        fetched = await _with_floodwait(
            lambda: client.get_messages(SOURCE_CHAT, ids=source_ids), logger, "restyle-source"
        )
        fetched = fetched if isinstance(fetched, list) else [fetched]
        sources = {int(m.id): m for m in fetched if m is not None}
    for message in sources.values():
        _remember_signal_text(message)

    for dest in recent:
        if str(dest.id) in done:
            continue
        outcome = await _restyle_one(client, dest, sources.get(by_dest[int(dest.id)]), registry, logger)
        counts[outcome] = counts.get(outcome, 0) + 1
        done.add(str(dest.id))
        state.update(status="running", done=sorted(done), counts=counts)
        _save_json(path, state)
        if outcome == "edited":
            await asyncio.sleep(0.3)

    state.update(status="done", counts=counts, completed_at=time.time())
    _save_json(path, state)
    logger.warning(
        "[WHOLECHAT RESTYLE LAST100 DONE] dest=%s relayed_posts=%s counts=%s", DEST_CHAT, len(recent), counts
    )
    return state


async def _restyle_recent_guarded(client, message_map, data_dir, registry, logger):
    try:
        await _restyle_recent_once(client, message_map, data_dir, registry, logger)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "[WHOLECHAT RESTYLE FAILED SAFE] %s: %s LIVE_UNAFFECTED=True", type(exc).__name__, exc
        )


def _unit_age(unit, now):
    dates = [message.date.timestamp() for message in unit if getattr(message, "date", None)]
    return now - max(dates) if dates else None


def _page_units(raw, checkpoint):
    """Units after the checkpoint, oldest first, and how far the checkpoint may
    move once they are all handled."""
    page = [message for message in raw if message is not None and int(message.id) > checkpoint]
    units = _build_units(page)
    end = max([checkpoint] + [int(message.id) for message in page])
    if len(raw) >= CATCHUP_PAGE and len(units) > 1 and getattr(units[-1][0], "grouped_id", None):
        end = min(_unit_ids(units.pop())) - 1  # a full page may end part-way through an album
    return units, end


class _CatchUp:
    """Reads the posts after a durable checkpoint every CATCHUP_SECONDS and
    sends the ones not mapped yet. The checkpoint ("every post up to here is
    handled") only moves over a contiguous run of handled posts, so a later
    success never skips an earlier failure."""

    def __init__(self, client, deliver, message_map, data_dir, logger, clock=time.time):
        self.client = client
        self.deliver = deliver
        self.message_map = message_map
        self.path = data_dir / CATCHUP_FILENAME
        self.logger = logger
        self.clock = clock
        self.checkpoint = None
        self.failures = {}  # first source id -> (first failure time, attempts)

    async def run(self, sleep=asyncio.sleep):
        while True:
            try:
                if self.checkpoint is None:
                    self.checkpoint = await self.start()
                await self.run_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("[WHOLECHAT CATCHUP PASS FAILED] %s: %s", type(exc).__name__, exc)
            await sleep(CATCHUP_SECONDS)

    async def start(self):
        checkpoint = self.stored()
        if checkpoint is None:
            checkpoint = await self.first_checkpoint()
            self.save(checkpoint)
        self.logger.warning(
            "[WHOLECHAT CATCHUP READY] source=%s dest=%s checkpoint=%s every=%ss stale_after=%ss",
            SOURCE_CHAT,
            DEST_CHAT,
            checkpoint,
            CATCHUP_SECONDS,
            STALE_SECONDS,
        )
        return checkpoint

    def stored(self):
        try:
            return int(_load_json(self.path)["catchup_last_id"])
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, checkpoint):
        _save_json(self.path, {"catchup_last_id": int(checkpoint), "updated_at": time.time()})

    async def first_checkpoint(self):
        """The newest post already relayed or, with an empty map, the newest in
        the chat: a first start sends nothing old."""
        mapped = [int(key) for key in self.message_map if str(key).isdigit()]
        if mapped:
            return max(mapped)
        newest = await _with_floodwait(
            lambda: self.client.get_messages(SOURCE_CHAT, limit=1), self.logger, "catchup-start"
        )
        newest = [message for message in newest or [] if message is not None]
        return int(newest[0].id) if newest else 0

    async def run_pass(self):
        checkpoint = self.checkpoint
        raw = await _with_floodwait(
            lambda: self.client.get_messages(
                SOURCE_CHAT, min_id=checkpoint, reverse=True, limit=CATCHUP_PAGE
            ),
            self.logger,
            "catchup",
        )
        units, advance = _page_units(list(raw or []), checkpoint)
        sent = []
        for unit in units:
            ids = _unit_ids(unit)
            outcome = await self.handle(unit, ids)
            if outcome == "wait":
                advance = ids[0] - 1
                break
            self.failures.pop(ids[0], None)
            if outcome == "sent":
                sent.extend(ids)
        if sent:
            self.logger.warning("[WHOLECHAT CATCHUP SENT] source_ids=%s", sent)
        if advance > checkpoint:
            self.save(advance)
            self.checkpoint = advance

    async def handle(self, unit, ids):
        """'sent', 'handled' (nothing left to do) or 'wait' (stop the pass here)."""
        now = self.clock()
        age = _unit_age(unit, now)
        if getattr(unit[0], "grouped_id", None) and age is not None and age < ALBUM_SETTLE_SECONDS:
            return "wait"  # Telegram may still be adding items to this album
        if _unit_already_mapped(unit, self.message_map):
            return "handled"
        if age is not None and age > STALE_SECONDS:
            self.logger.warning(
                "[GAP STALE NOT SENT] route=%s ids=%s age=%ss max=%ss",
                ROUTE_NAME,
                ids,
                int(age),
                STALE_SECONDS,
            )
            return "handled"
        try:
            outcome = await self.deliver(unit, "catchup")
        except FloodWaitError as exc:  # not the post's fault: retry next pass, uncounted
            self.logger.warning("[WHOLECHAT CATCHUP FLOODWAIT] source_ids=%s wait=%ss", ids, exc.seconds)
            return "wait"
        except Exception as exc:
            return self.failed(ids, exc, now)
        return "sent" if outcome == "sent" else "handled"

    def failed(self, ids, exc, now):
        first_failure, attempts = self.failures.get(ids[0], (now, 0))
        attempts += 1
        self.failures[ids[0]] = (first_failure, attempts)
        if now - first_failure >= GIVE_UP_SECONDS:
            self.logger.error("[INTAKE GAVE UP] route=%s ids=%s attempts=%s", ROUTE_NAME, ids, attempts)
            return "handled"
        self.logger.warning(
            "[WHOLECHAT CATCHUP RETRY] source_ids=%s attempts=%s %s: %s",
            ids,
            attempts,
            type(exc).__name__,
            exc,
        )
        return "wait"


def _sweep_album_buffer(client):
    """Telethon 1.45 keeps an AlbumHack in client._albums for every album seen
    (in any chat) while an events.Album handler is registered, and never drops
    it. Drop the ones delivered over ALBUM_BUFFER_SECONDS ago."""
    albums = getattr(client, "_albums", None)
    if not isinstance(albums, dict):
        return 0
    cutoff = asyncio.get_running_loop().time() - ALBUM_BUFFER_SECONDS
    expired = [key for key, hack in list(albums.items()) if getattr(hack, "_due", cutoff) < cutoff]
    for key in expired:
        albums.pop(key, None)
    return len(expired)


async def _album_buffer_sweeper(client, logger):
    while True:
        await asyncio.sleep(ALBUM_BUFFER_SECONDS)
        try:
            _sweep_album_buffer(client)
        except Exception as exc:
            logger.warning("[WHOLECHAT ALBUM BUFFER SWEEP FAILED] %s: %s", type(exc).__name__, exc)


def _detach_previous(main_module, client):
    """A retry after the handlers were added must not leave the last attempt's
    handlers (with their own map) live: every post would be sent twice."""
    for name in (NEW_HANDLER_ATTR, ALBUM_HANDLER_ATTR):
        handler = getattr(main_module, name, None)
        if handler is not None:
            client.remove_event_handler(handler)
    for name in (CATCHUP_TASK_ATTR, ALBUM_SWEEP_TASK_ATTR):
        task = getattr(main_module, name, None)
        if task is not None:
            task.cancel()


async def run_wholechat_4281603170_to_3743381585(main_module, logger=None):
    logger = logger or log
    client = getattr(main_module, "client", None)
    if client is None:
        raise RuntimeError("Main Telegram client is unavailable")

    signal_tests, update_tests = run_samgtrades_formatter_self_test()
    # The registry loads from Saved Messages during startup; wait for it here
    # rather than failing and sitting out the outer 10s retry.
    registry = getattr(main_module, "EMOJI_REGISTRY", None)
    for _ in range(240):
        if registry_ready(registry):
            break
        await asyncio.sleep(0.25)
        registry = getattr(main_module, "EMOJI_REGISTRY", None)
    else:
        raise RuntimeError("SamGtrades house formatter is waiting for the VIP custom-emoji registry")

    logger.warning(
        "[WHOLECHAT HOUSE FORMATTER READY] source=%s dest=%s "
        "style=VIP_TOPIC_7 signal_tests=%s update_tests=%s aliases=Boom,GreenTick,RedCross,Warning",
        SOURCE_CHAT,
        DEST_CHAT,
        signal_tests,
        update_tests,
    )

    data_dir = _data_dir(main_module)
    state_path = data_dir / STATE_FILENAME
    map_path = data_dir / MAP_FILENAME
    state = _load_json(state_path)
    message_map = _load_json(map_path)

    lock = asyncio.Lock()
    bootstrap_ready = asyncio.Event()

    source_entity = await _with_floodwait(lambda: client.get_entity(SOURCE_CHAT), logger, "source-access")
    dest_entity = await _with_floodwait(lambda: client.get_entity(DEST_CHAT), logger, "dest-access")

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
                client, [message], message_map, map_path, data_dir, registry, lock, logger, "live"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[WHOLECHAT LIVE FAILED] %s: %s", type(exc).__name__, exc)

    async def on_album(event):
        try:
            await bootstrap_ready.wait()
            unit = list(getattr(event, "messages", None) or [])
            await _deliver_unit(
                client, unit, message_map, map_path, data_dir, registry, lock, logger, "live-album"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[WHOLECHAT LIVE ALBUM FAILED] %s: %s", type(exc).__name__, exc)

    async def deliver(unit, reason):
        return await _deliver_unit(
            client, unit, message_map, map_path, data_dir, registry, lock, logger, reason
        )

    _detach_previous(main_module, client)
    client.add_event_handler(on_new_message, events.NewMessage(chats=SOURCE_CHAT))
    client.add_event_handler(on_album, events.Album(chats=SOURCE_CHAT))

    setattr(main_module, NEW_HANDLER_ATTR, on_new_message)
    setattr(main_module, ALBUM_HANDLER_ATTR, on_album)
    setattr(main_module, ALBUM_SWEEP_TASK_ATTR, asyncio.create_task(_album_buffer_sweeper(client, logger)))

    def start_background():
        setattr(
            main_module,
            "WHOLECHAT_4281603170_RESTYLE_TASK",
            asyncio.create_task(_restyle_recent_guarded(client, message_map, data_dir, registry, logger)),
        )
        catchup = _CatchUp(client, deliver, message_map, data_dir, logger)
        setattr(main_module, CATCHUP_TASK_ATTR, asyncio.create_task(catchup.run()))

    if state.get("status") == "done":
        bootstrap_ready.set()
        start_background()
        logger.warning(
            "[WHOLECHAT LAST50 STATE SKIP] already_done=True " "selected=%s sent=%s existing=%s skipped=%s",
            state.get("selected", 0),
            state.get("sent", 0),
            state.get("existing", 0),
            state.get("skipped", 0),
        )
        logger.warning(
            "[WHOLECHAT RELAY READY] source=%s dest=%s " "live=True last50_complete=True",
            SOURCE_CHAT,
            DEST_CHAT,
        )
        return {"source": SOURCE_CHAT, "dest": DEST_CHAT, "live": True, "last50_complete": True}

    selected_units = state.get("selected_units") if state.get("status") == "running" else None

    if not selected_units:
        history = await _with_floodwait(
            lambda: client.get_messages(SOURCE_CHAT, limit=SOURCE_FETCH_LIMIT),
            logger,
            "source-last50-history",
        )
        units = _build_units(list(history or []))
        selected_units = [_unit_ids(unit) for unit in units]
        selected_units = [ids for ids in selected_units if ids]

        if not selected_units:
            bootstrap_ready.set()
            raise RuntimeError("Source returned no copyable messages for last-50 bootstrap")

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
                    "[WHOLECHAT LAST50 SOURCE MISSING] " "post=%s/%s source_ids=%s",
                    index,
                    len(selected_units),
                    ids,
                )
            else:
                outcome = await _deliver_unit(
                    client, unit, message_map, map_path, data_dir, registry, lock, logger, "last50"
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
            "[WHOLECHAT LAST50 DONE] source=%s dest=%s " "selected=%s sent=%s existing=%s skipped=%s",
            SOURCE_CHAT,
            DEST_CHAT,
            len(selected_units),
            sent_count,
            existing_count,
            skipped_count,
        )
    finally:
        bootstrap_ready.set()

    start_background()
    logger.warning(
        "[WHOLECHAT RELAY READY] source=%s dest=%s " "live=True last50_complete=%s",
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


def _is_access_error(exc):
    text = str(exc)
    return isinstance(exc, ChannelPrivateError) or (
        isinstance(exc, ValueError)
        and ("Could not find the input entity" in text or "Could not find input entity" in text)
    )


async def run_with_retry(main_module, logger=None):
    """Run the relay for the life of the worker; lost access stops it for good."""
    logger = logger or log
    while True:
        try:
            await run_wholechat_4281603170_to_3743381585(main_module, logger=logger)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_access_error(exc):
                logger.warning("[OWNER WHOLECHAT STOPPED] error=%s RETRY=False", type(exc).__name__)
                return
            logger.exception("[WHOLECHAT RELAY WAIT/RETRY] %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(10)

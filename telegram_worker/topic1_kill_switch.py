"""Permanent kill-switch for the dead ExposedFX hub topic 1.

The owner removed this forum topic and wants it to remain completely dead.
This module therefore does three things:

1. Blocks any explicit route to -1003918958200 / topic 1.
2. Detects routes whose sends unexpectedly land in topic 1 (for example after
   a destination topic was deleted), immediately deletes the escaped message,
   and persistently quarantines that route.
3. Purges every existing child message in topic 1 and keeps deleting any new
   message that appears there, regardless of which connected worker/account
   produced it. The forum root message itself is never deleted.

Quarantine state is stored under DATA_DIR so Railway restarts do not re-enable
an offending route accidentally.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError


log = logging.getLogger("topic1-kill-switch")

HUB_CHAT = -1003918958200
DEAD_TOPIC = 1
STATE_FILE = "dead_topic1_quarantine_v1.json"
PURGE_LIMIT = 2000
SWEEP_SECONDS = 20


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _route_sig(route):
    return (
        _as_int(route.get("source_chat")),
        _as_int(route.get("source_topic")) if route.get("source_topic") is not None else None,
        _as_int(route.get("dest_chat")),
        _as_int(route.get("dest_topic")),
    )


def _sig_key(sig):
    return ":".join("None" if value is None else str(value) for value in sig)


def _state_path(main_module):
    data_dir = Path(getattr(main_module, "DATA_DIR", Path("./data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / STATE_FILE


def _load_state(path):
    if not path.exists():
        return {"quarantined": {}, "purge_runs": 0}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.setdefault("quarantined", {})
            value.setdefault("purge_runs", 0)
            return value
    except Exception:
        pass
    return {"quarantined": {}, "purge_runs": 0}


def _save_state(path, state):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _mapped_ids(value):
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            try:
                out.append(int(item))
            except Exception:
                pass
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_mapped_ids(item))
        return out
    try:
        return [int(value)]
    except Exception:
        return []


def _topic_id(message):
    if message is None:
        return None

    for obj in (message, getattr(message, "reply_to", None)):
        if obj is None:
            continue
        for attr in ("reply_to_top_id", "top_msg_id"):
            value = _as_int(getattr(obj, attr, None))
            if value is not None:
                return value

    if getattr(message, "is_topic_message", False):
        for obj in (message, getattr(message, "reply_to", None)):
            if obj is None:
                continue
            value = _as_int(getattr(obj, "reply_to_msg_id", None))
            if value is not None:
                return value

    return None


def _message_ids(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            mid = _as_int(getattr(item, "id", None))
            if mid is not None:
                out.append(mid)
        return out
    mid = _as_int(getattr(value, "id", None))
    return [mid] if mid is not None else []


async def _delete_ids(client, ids, logger, reason):
    ids = sorted({int(mid) for mid in ids if _as_int(mid) not in (None, DEAD_TOPIC)})
    deleted = 0
    for start in range(0, len(ids), 100):
        batch = ids[start : start + 100]
        while True:
            try:
                await client.delete_messages(HUB_CHAT, batch, revoke=True)
                deleted += len(batch)
                break
            except FloodWaitError as exc:
                wait_for = max(1, int(exc.seconds)) + 1
                logger.warning(
                    "[TOPIC1 PURGE FLOODWAIT] reason=%s wait=%ss batch=%s",
                    reason,
                    wait_for,
                    len(batch),
                )
                await asyncio.sleep(wait_for)
            except Exception as exc:
                logger.exception(
                    "[TOPIC1 PURGE DELETE FAILED] reason=%s ids=%s %s: %s",
                    reason,
                    batch,
                    type(exc).__name__,
                    exc,
                )
                break
    return deleted


async def _fetch_topic_children(client, logger):
    messages = []
    try:
        raw = await client.get_messages(HUB_CHAT, limit=PURGE_LIMIT, reply_to=DEAD_TOPIC)
        messages.extend(list(raw or []))
    except FloodWaitError as exc:
        await asyncio.sleep(max(1, int(exc.seconds)) + 1)
        try:
            raw = await client.get_messages(HUB_CHAT, limit=PURGE_LIMIT, reply_to=DEAD_TOPIC)
            messages.extend(list(raw or []))
        except Exception as retry_exc:
            logger.warning(
                "[TOPIC1 THREAD FETCH FAILED] %s: %s",
                type(retry_exc).__name__,
                retry_exc,
            )
    except Exception as exc:
        logger.warning("[TOPIC1 THREAD FETCH FAILED] %s: %s", type(exc).__name__, exc)

    # Deduplicate and keep only actual children. The General topic root id=1 is
    # intentionally preserved even if Telegram includes it in a thread query.
    out = {}
    for message in messages:
        mid = _as_int(getattr(message, "id", None))
        if mid is None or mid == DEAD_TOPIC:
            continue
        out[mid] = message
    return list(out.values())


def install_topic1_kill_switch(main_module, logger=None):
    """Install permanent route quarantine + purge protection.

    This function is deliberately synchronous so wrappers/event handlers are
    active immediately. Startup audit/purge runs in an async background task.
    """
    logger = logger or log
    client = getattr(main_module, "client", None)
    routes = getattr(main_module, "ROUTES", None)
    if client is None or not isinstance(routes, list):
        raise RuntimeError("topic1 kill-switch requires main worker client and ROUTES")

    state_path = _state_path(main_module)
    state = _load_state(state_path)
    quarantined = state.setdefault("quarantined", {})

    # Explicit topic-1 routes are never permitted.
    explicit = [
        route
        for route in routes
        if _as_int(route.get("dest_chat")) == HUB_CHAT
        and _as_int(route.get("dest_topic")) == DEAD_TOPIC
    ]
    for route in explicit:
        sig = _route_sig(route)
        quarantined[_sig_key(sig)] = {
            "reason": "explicit_dead_topic_destination",
            "route_name": str(route.get("name") or ""),
            "ts": time.time(),
        }

    def is_quarantined(route):
        sig = _route_sig(route)
        return (
            (_as_int(route.get("dest_chat")) == HUB_CHAT and _as_int(route.get("dest_topic")) == DEAD_TOPIC)
            or _sig_key(sig) in quarantined
        )

    def apply_quarantine_to_routes():
        before = len(routes)
        routes[:] = [route for route in routes if not is_quarantined(route)]
        return before - len(routes)

    removed_now = apply_quarantine_to_routes()
    _save_state(state_path, state)

    original_copy_one = getattr(main_module, "copy_one", None)
    original_copy_album = getattr(main_module, "copy_album", None)
    if not callable(original_copy_one) or not callable(original_copy_album):
        raise RuntimeError("main worker copy_one/copy_album unavailable")

    async def quarantine_route(route, reason, escaped_ids=None):
        sig = _route_sig(route)
        key = _sig_key(sig)
        if key not in quarantined:
            quarantined[key] = {
                "reason": str(reason),
                "route_name": str(route.get("name") or ""),
                "ts": time.time(),
                "escaped_ids": list(escaped_ids or []),
            }
            removed = apply_quarantine_to_routes()
            _save_state(state_path, state)
            logger.error(
                "[TOPIC1 ROUTE QUARANTINED] route=%r source=%s_%s intended_dest=%s_%s "
                "reason=%s removed_from_routes=%s escaped_ids=%s",
                route.get("name"),
                sig[0],
                sig[1],
                sig[2],
                sig[3],
                reason,
                removed,
                list(escaped_ids or []),
            )

    async def inspect_sent_result(route, result):
        messages = result if isinstance(result, (list, tuple)) else [result]
        escaped = []
        for message in messages:
            if message is None:
                continue
            if _as_int(getattr(message, "chat_id", None)) not in (None, HUB_CHAT):
                continue
            if _topic_id(message) == DEAD_TOPIC:
                mid = _as_int(getattr(message, "id", None))
                if mid is not None and mid != DEAD_TOPIC:
                    escaped.append(mid)
        if escaped:
            await _delete_ids(client, escaped, logger, "post-send-escape")
            await quarantine_route(route, "send_landed_in_dead_topic_1", escaped)
        return result

    async def guarded_copy_one(message, route, *args, **kwargs):
        if is_quarantined(route):
            logger.error(
                "[TOPIC1 SEND BLOCKED] route=%r source=%s_%s intended_dest=%s_%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                route.get("dest_chat"),
                route.get("dest_topic"),
            )
            return None
        result = await original_copy_one(message, route, *args, **kwargs)
        return await inspect_sent_result(route, result)

    async def guarded_copy_album(messages, route, *args, **kwargs):
        if is_quarantined(route):
            logger.error(
                "[TOPIC1 ALBUM BLOCKED] route=%r source=%s_%s intended_dest=%s_%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                route.get("dest_chat"),
                route.get("dest_topic"),
            )
            return None
        result = await original_copy_album(messages, route, *args, **kwargs)
        return await inspect_sent_result(route, result)

    main_module.copy_one = guarded_copy_one
    main_module.copy_album = guarded_copy_album

    async def purge_and_reverse_audit(reason):
        children = await _fetch_topic_children(client, logger)
        ids = {int(message.id) for message in children if _as_int(getattr(message, "id", None)) not in (None, DEAD_TOPIC)}

        # Reverse-map any destination IDs found in the persistent message map.
        # This identifies which historical route(s) actually created messages
        # in the dead topic, even when their configured destination was another
        # topic that had subsequently been removed.
        reverse_hits = []
        message_map = getattr(main_module, "message_map", {}) or {}
        for key, value in list(message_map.items()):
            mapped = set(_mapped_ids(value))
            overlap = sorted(ids.intersection(mapped))
            if not overlap:
                continue

            parts = str(key).split(":")
            if len(parts) < 4:
                continue
            source_chat = _as_int(parts[0])
            source_msg = _as_int(parts[1])
            dest_chat = _as_int(parts[2])
            dest_topic = _as_int(parts[3])
            reverse_hits.append((source_chat, source_msg, dest_chat, dest_topic, overlap))

            for route in list(routes):
                if (
                    _as_int(route.get("source_chat")) == source_chat
                    and _as_int(route.get("dest_chat")) == dest_chat
                    and _as_int(route.get("dest_topic")) == dest_topic
                ):
                    await quarantine_route(
                        route,
                        "historical_message_map_proves_delivery_into_dead_topic_1",
                        overlap,
                    )

        if reverse_hits:
            for hit in reverse_hits[:100]:
                logger.error(
                    "[TOPIC1 WRITER IDENTIFIED] source_chat=%s source_msg=%s intended_dest=%s_%s actual_topic=1 dest_ids=%s",
                    hit[0], hit[1], hit[2], hit[3], hit[4],
                )
        else:
            logger.warning(
                "[TOPIC1 WRITER AUDIT] no persistent message-map hit found for current topic1 messages; "
                "protection remains active for external/legacy writers"
            )

        deleted = await _delete_ids(client, ids, logger, reason)
        state["purge_runs"] = int(state.get("purge_runs", 0) or 0) + 1
        state["last_purge"] = {
            "reason": reason,
            "found": len(ids),
            "deleted": deleted,
            "ts": time.time(),
        }
        _save_state(state_path, state)
        logger.warning(
            "[TOPIC1 PURGE COMPLETE] reason=%s found=%s deleted=%s root_preserved=True quarantined_routes=%s",
            reason,
            len(ids),
            deleted,
            len(quarantined),
        )
        return deleted

    @client.on(events.NewMessage(chats=HUB_CHAT))
    async def dead_topic_intrusion_guard(event):
        message = getattr(event, "message", None)
        if message is None or _as_int(getattr(message, "id", None)) == DEAD_TOPIC:
            return
        if _topic_id(message) != DEAD_TOPIC:
            return
        sender_id = _as_int(getattr(message, "sender_id", None))
        text = (getattr(message, "message", None) or "").replace("\n", " ")[:180]
        logger.error(
            "[TOPIC1 INTRUSION DELETED] msg=%s sender=%s outgoing=%s text=%r",
            getattr(message, "id", None),
            sender_id,
            bool(getattr(message, "out", False)),
            text,
        )
        await _delete_ids(client, [message.id], logger, "live-intrusion")

    async def sweep_loop():
        await purge_and_reverse_audit("startup-full-purge")
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                children = await _fetch_topic_children(client, logger)
                ids = [
                    int(message.id)
                    for message in children
                    if _as_int(getattr(message, "id", None)) not in (None, DEAD_TOPIC)
                ]
                if ids:
                    logger.error(
                        "[TOPIC1 SWEEP FOUND INTRUSIONS] count=%s newest=%s",
                        len(ids),
                        max(ids),
                    )
                    await _delete_ids(client, ids, logger, "periodic-sweep")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[TOPIC1 SWEEP FAILED] %s: %s", type(exc).__name__, exc)

    task = asyncio.create_task(sweep_loop())
    main_module.TOPIC1_KILL_SWITCH_TASK = task
    main_module.TOPIC1_KILL_SWITCH_STATE = state

    logger.error(
        "[TOPIC1 KILL SWITCH ACTIVE] dest=%s_%s explicit_routes_removed=%s "
        "persistent_quarantine=%s live_delete=True periodic_sweep=%ss root_preserved=True",
        HUB_CHAT,
        DEAD_TOPIC,
        removed_now,
        len(quarantined),
        SWEEP_SECONDS,
    )

    return state

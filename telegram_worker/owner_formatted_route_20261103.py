"""Owner route 2026-11-03: relay -1004367822325/8323 -> VIP -1003726286301/12289, formatted.

Signals, trade updates and end-of-day recaps are rewritten in the owner's
library format (library_signal_format); chatter, promos, links and bare media
are not posted.
Runs inside the already-authorized MAIN process with its client. First start
takes the latest 30 posts of the source topic, then continues live: a
NewMessage event wakes the ordered checkpoint reader at once, and a short poll
is the backstop. Updates reply to their formatted signal in the destination.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError

from telegram_worker import library_signal_format as fmt
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.owner_requested_routes_20261102 import (
    load_state,
    rpc,
    save_state,
    topic_id,
    verify_roots,
)

log = logging.getLogger("owner-formatted-20261103")
ROUTE = {
    "name": "Owner Relay8323 To VIP12289 Formatted",
    "source_chat": -1004367822325,
    "source_topic": 8323,
    "dest_chat": -1003726286301,
    "dest_topic": 12289,
}
BACKFILL = 30
POLL_SECONDS = 3
ORPHAN_MAX_AGE = 24 * 3600
STATE_FILE = "owner_formatted_8323_to_12289_v1.json"


def parent_id(message):
    """Replied-to post inside the topic; None for a top-level topic post."""
    reply = getattr(message, "reply_to", None)
    if reply is None or not getattr(reply, "reply_to_top_id", None):
        return None
    return getattr(reply, "reply_to_msg_id", None)


def _copy(value):
    return json.loads(json.dumps(value))


async def send(worker, text, entities, reply_to):
    return await rpc(
        lambda: worker.client.send_message(
            ROUTE["dest_chat"], text, formatting_entities=entities, reply_to=reply_to, link_preview=False
        )
    )


async def process(worker, state, message):
    text = getattr(message, "message", None) or ""
    source_id = str(message.id)
    signal = fmt.parse_signal(text)
    if signal:
        out, entities = fmt.render_signal(signal)
        sent = await send(worker, out, entities, ROUTE["dest_topic"])
        state["signals"][source_id] = {
            "signal": signal,
            "dest": int(sent.id),
            "progress": {},
            "at": message.date.timestamp(),
        }
        state["latest_signal"] = source_id
        state["posted"][source_id] = int(sent.id)
        return "signal"
    recap = fmt.parse_recap(text)
    if recap:
        out, entities = fmt.render_recap(recap)
        sent = await send(worker, out, entities, ROUTE["dest_topic"])
        state["posted"][source_id] = int(sent.id)
        return "recap"
    update = fmt.classify_update(text)
    if not update:
        return "skipped"
    parent = parent_id(message)
    owner_id = state["thread"].get(str(parent), str(parent)) if parent else None
    if owner_id is None:
        # The provider sometimes posts an update without replying. Attach it
        # only to the newest signal, and only while that signal is recent.
        latest = state.get("latest_signal")
        record = state["signals"].get(latest) if latest else None
        if record and message.date.timestamp() - record["at"] <= ORPHAN_MAX_AGE:
            owner_id = latest
    record = state["signals"].get(owner_id) if owner_id else None
    if not record:
        return "orphan"
    signal, progress = _copy(record["signal"]), _copy(record["progress"])
    resolved = fmt.resolve_update(update, signal, progress)
    if not resolved:
        return "repeat"
    out, entities = fmt.render_update(resolved, signal)
    sent = await send(worker, out, entities, record["dest"])
    record["signal"], record["progress"] = signal, progress
    state["thread"][source_id] = owner_id
    state["posted"][source_id] = int(sent.id)
    return "update"


async def initialize(worker, state, path):
    latest = await rpc(
        lambda: worker.client.get_messages(
            ROUTE["source_chat"], reply_to=ROUTE["source_topic"], limit=BACKFILL + 1
        )
    )
    ids = sorted(int(m.id) for m in latest or [] if int(m.id) != ROUTE["source_topic"])[-BACKFILL:]
    # An empty topic starts at 0, so the first posts that arrive are formatted.
    state.update(
        initialized=True,
        checkpoint=(ids[0] - 1) if ids else 0,
        backfill_selected=len(ids),
        started_at=time.time(),
    )
    save_state(path, state)
    log.warning(
        "[OWNER FORMATTED BACKFILL SELECTED] contract=%s requested=%s selected=%s",
        policy.identity(ROUTE),
        BACKFILL,
        len(ids),
    )


async def cycle(worker, state, path):
    checkpoint = int(state["checkpoint"])
    messages = [
        m
        async for m in worker.client.iter_messages(
            ROUTE["source_chat"], reply_to=ROUTE["source_topic"], min_id=checkpoint, reverse=True, limit=50
        )
    ]
    for message in messages:
        if int(message.id) <= checkpoint or int(message.id) == ROUTE["source_topic"]:
            continue
        if topic_id(message) not in (None, ROUTE["source_topic"]):
            raise RuntimeError("Owner formatted source topic mismatch")
        outcome = await process(worker, state, message)
        state["checkpoint"] = checkpoint = int(message.id)
        state["counts"][outcome] = state["counts"].get(outcome, 0) + 1
        save_state(path, state)
        log.warning(
            "[OWNER FORMATTED POST] contract=%s source_id=%s outcome=%s lag=%.1fs",
            policy.identity(ROUTE),
            message.id,
            outcome,
            time.time() - message.date.timestamp(),
        )


async def reformat_edited_signal(worker, state, path, message):
    record = state["signals"].get(str(message.id))
    signal = fmt.parse_signal(getattr(message, "message", None) or "") if record else None
    if not signal or signal == record["signal"]:
        return
    out, entities = fmt.render_signal(signal)
    await rpc(
        lambda: worker.client.edit_message(
            ROUTE["dest_chat"], record["dest"], out, formatting_entities=entities, link_preview=False
        )
    )
    record["signal"] = signal
    save_state(path, state)
    log.warning("[OWNER FORMATTED SIGNAL EDITED] source_id=%s dest_id=%s", message.id, record["dest"])


def stop(state, path, reason):
    """Returns False while the policy is still confirming a transient-looking reason."""
    policy.stop_route(ROUTE, reason, log)
    if not policy.is_stopped(ROUTE):
        return False
    state.update(status="stopped", stopped_reason=reason, stopped_at=time.time())
    save_state(path, state)
    return True


async def run_owner_formatted_route(worker):
    path = Path(worker.DATA_DIR) / STATE_FILE
    state = load_state(path)
    for key in ("signals", "thread", "posted", "counts"):
        state.setdefault(key, {})
    if policy.is_stopped(ROUTE) or state.get("status") == "stopped":
        log.warning("[OWNER FORMATTED STOPPED] contract=%s RETRY=False", policy.identity(ROUTE))
        return
    client = worker.client
    while not client.is_connected() or not await client.is_user_authorized():
        await asyncio.sleep(0.5)
    wake = asyncio.Event()
    lock = asyncio.Lock()

    async def on_new(event):
        if topic_id(event.message) == ROUTE["source_topic"]:
            wake.set()

    async def on_edit(event):
        if topic_id(event.message) != ROUTE["source_topic"]:
            return
        try:
            async with lock:
                await reformat_edited_signal(worker, state, path, event.message)
        except Exception as exc:
            log.warning(
                "[OWNER FORMATTED EDIT SKIPPED] source_id=%s error=%s", event.message.id, type(exc).__name__
            )

    handlers = (
        (on_new, events.NewMessage(chats=ROUTE["source_chat"])),
        (on_edit, events.MessageEdited(chats=ROUTE["source_chat"])),
    )
    for callback, event in handlers:
        client.add_event_handler(callback, event)
    try:
        while True:
            try:
                async with lock:
                    await verify_roots(worker, ROUTE)
                    if not state.get("initialized"):
                        await initialize(worker, state, path)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = policy.permanent_access_error(exc)
                if reason and stop(state, path, reason):
                    return
                log.warning(
                    "[OWNER FORMATTED WAIT] contract=%s error=%s", policy.identity(ROUTE), type(exc).__name__
                )
                await asyncio.sleep(15)
        log.warning(
            "[OWNER FORMATTED LIVE READY] contract=%s checkpoint=%s poll=%ss EVENT_WAKE=True",
            policy.identity(ROUTE),
            state["checkpoint"],
            POLL_SECONDS,
        )
        while True:
            wake.clear()
            try:
                async with lock:
                    await cycle(worker, state, path)
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                await asyncio.sleep(max(1, int(exc.seconds)) + 1)
            except Exception as exc:
                reason = policy.permanent_access_error(exc)
                if reason and stop(state, path, reason):
                    return
                log.warning(
                    "[OWNER FORMATTED LIVE WAIT] contract=%s error=%s",
                    policy.identity(ROUTE),
                    type(exc).__name__,
                )
                await asyncio.sleep(5)
            try:
                await asyncio.wait_for(wake.wait(), POLL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        for callback, event in handlers:
            client.remove_event_handler(callback, event)

"""Exact owner routes; sequential 50/20/20/20 bootstrap and durable live polling.

Runs inside the already-authorized MAIN process, using its own copy pipeline.
No new Telegram session, whole-chat fallback, deletion, or credential changes.
"""
import asyncio
import json
import logging
import time
from pathlib import Path

from telethon import events
from telethon.errors import FloodWaitError
from telegram_worker import owner_forwarding_policy as policy

log = logging.getLogger("owner-routes-20261102")
TARGETS = (
    (-1004367822325, 7250, -1003726286301, 12161, 50),
    (-1003726286301, 1364, -1003786932333, 28, 20),
    (-1003726286301, 10877, -1003786932333, 8, 20),
    (-1003726286301, 12161, -1003786932333, 24, 20),
)
STATE_FILE = "owner_requested_routes_20261102_v1.json"
LIVE_POLL_SECONDS = 5


def load_state(path):
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("Invalid owner backfill state; refusing to overwrite it")
    return value


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def route_identity(route):
    return tuple(route[k] for k in ("source_chat", "source_topic", "dest_chat", "dest_topic"))


def topic_id(message):
    reply = getattr(message, "reply_to", None)
    return (getattr(reply, "reply_to_top_id", None)
            or getattr(message, "reply_to_top_id", None)
            or getattr(reply, "reply_to_msg_id", None)
            or getattr(message, "reply_to_msg_id", None))


def mapped_ids(worker, route, unit):
    out = []
    for message in unit:
        values = worker.existing_destination_ids(message, route)
        if not isinstance(values, (list, tuple, set)):
            values = [values] if values else []
        for value in values:
            if value and int(value) not in out:
                out.append(int(value))
    return out


async def rpc(call):
    while True:
        try:
            return await call()
        except FloodWaitError as exc:
            await asyncio.sleep(max(1, int(exc.seconds)) + 1)


async def verify_roots(worker, route):
    for side in ("source", "dest"):
        root = await rpc(lambda: worker.client.get_messages(
            route[side + "_chat"], ids=route[side + "_topic"]))
        if not root or not getattr(root, "action", None) or type(root.action).__name__ != "MessageActionTopicCreate":
            raise RuntimeError("Owner topic unavailable: " + side)


async def verify_copies(worker, route, ids):
    if not ids:
        return 0
    current = await rpc(lambda: worker.client.get_messages(route["dest_chat"], ids=ids))
    current = current if isinstance(current, (list, tuple)) else [current]
    found = {int(m.id) for m in current if m and topic_id(m) == route["dest_topic"]}
    if set(ids) != found:
        raise RuntimeError("Owner destination mapping verification incomplete")
    return len(found)


async def copy_unit(worker, route, unit):
    # The same route is never handled by any legacy live writer.
    # Never import a reply's older parent merely to satisfy this history request.
    unit = sorted(unit, key=lambda m: int(m.id))
    if all(worker.existing_destination_ids(m, route) for m in unit):
        return "existing", mapped_ids(worker, route, unit)
    before = mapped_ids(worker, route, unit)
    if len(unit) > 1 and getattr(unit[0], "grouped_id", None):
        await worker.copy_album(unit, route)
    else:
        await worker.copy_one(unit[0], route, edited=False, ensure_reply=False)
    after = mapped_ids(worker, route, unit)
    if any(i not in before for i in after):
        return "sent", after
    return "filtered", after


def _copyable(worker, message, route):
    if message is None or int(getattr(message, "id", 0) or 0) == route.get("source_topic"):
        return False
    return bool(worker.text_of(message) or worker.is_real_media(message))


def units(worker, route, messages, count=None):
    """Oldest-first logical posts; an album is one unit and is never split."""
    messages = sorted((m for m in messages if _copyable(worker, m, route)),
                      key=lambda m: int(getattr(m, "id", 0) or 0))
    result, used = [], set()
    for message in messages:
        if int(message.id) in used:
            continue
        grouped_id = getattr(message, "grouped_id", None)
        unit = [m for m in messages if grouped_id and getattr(m, "grouped_id", None) == grouped_id] or [message]
        used.update(int(m.id) for m in unit)
        result.append(unit)
    result = result[-50:]
    return result[-count:] if count else result


async def bootstrap_route(worker, route, count, entry, state, path):
    key = policy.identity(route)
    if entry.get("status") in {"done", "stopped"}:
        return
    await verify_roots(worker, route)
    if not entry.get("selected_units"):
        messages = list(await rpc(lambda: worker.client.get_messages(
            route["source_chat"], reply_to=route["source_topic"], limit=1000)) or [])
        # Topic API is authoritative; positive metadata for a different topic
        # is rejected too. No whole-chat fallback.
        messages = [m for m in messages if int(m.id) != route["source_topic"]
                    and (topic_id(m) in (None, route["source_topic"]))]
        selected = units(worker, route, messages, count)
        if not selected:
            raise RuntimeError("Owner source returned no copyable posts")
        entry.update(status="running", selected_units=[[int(m.id) for m in u] for u in selected],
                     requested=count, selected=len(selected), completed={}, sent=0,
                     existing=0, filtered=0, last_source_id=0, started_at=time.time())
        state[key] = entry
        save_state(path, state)
        log.warning("[OWNER BACKFILL SELECTED] contract=%s requested=%s selected=%s FIRST_ROUTE_PRIORITY=True",
                    key, count, len(selected))
    for ids in entry["selected_units"]:
        token = ",".join(str(i) for i in ids)
        if token in entry["completed"]:
            continue
        raw = await rpc(lambda: worker.client.get_messages(route["source_chat"], ids=ids))
        raw = raw if isinstance(raw, (list, tuple)) else [raw]
        unit = [m for m in raw if m]
        if len(unit) != len(ids) or any(topic_id(m) not in (None, route["source_topic"]) for m in unit):
            raise RuntimeError("Owner source selection changed or crossed topics")
        outcome, destinations = await copy_unit(worker, route, unit)
        entry["completed"][token] = {"outcome": outcome, "dest_ids": destinations}
        entry[outcome] += 1
        entry["last_source_id"] = max(entry["last_source_id"], max(ids))
        save_state(path, state)
        log.warning("[OWNER BACKFILL POST] contract=%s outcome=%s progress=%s/%s",
                    key, outcome, len(entry["completed"]), entry["selected"])
        await asyncio.sleep(0.4)
    dest_ids = sorted({i for value in entry["completed"].values() for i in value["dest_ids"]})
    entry["verified_destination_messages"] = await verify_copies(worker, route, dest_ids)
    entry.update(status="done", completed_at=time.time())
    save_state(path, state)
    log.warning("[OWNER BACKFILL DONE] contract=%s selected=%s sent=%s existing=%s filtered=%s verified_messages=%s",
                key, entry["selected"], entry["sent"], entry["existing"], entry["filtered"],
                entry["verified_destination_messages"])


def stop(worker, route, entry, state, path, reason):
    """Returns False while the policy is still confirming a transient-looking reason."""
    policy.stop_route(route, reason, log)
    if not policy.is_stopped(route):
        return False
    entry.update(status="stopped", stopped_reason=reason, stopped_at=time.time())
    state[policy.identity(route)] = entry
    save_state(path, state)
    return True


async def live_cycle(worker, route, entry, state, path):
    checkpoint = int(entry["last_source_id"])
    # Oldest unseen first, rather than truncating a recent-history catch-up.
    messages = [m async for m in worker.client.iter_messages(
        route["source_chat"], reply_to=route["source_topic"], min_id=checkpoint,
        reverse=True, limit=50)]
    if messages and getattr(messages[-1], "grouped_id", None):
        # A page boundary may cut a ten-item album. Read the exact topic's
        # neighbouring IDs and include only members of that same album.
        last = messages[-1]
        neighbours = await rpc(lambda: worker.client.get_messages(
            route["source_chat"], reply_to=route["source_topic"],
            min_id=max(checkpoint, int(last.id) - 10), max_id=int(last.id) + 11,
            limit=30))
        combined = {int(m.id): m for m in messages}
        for candidate in neighbours or []:
            if (getattr(candidate, "grouped_id", None) == last.grouped_id
                    and int(candidate.id) > checkpoint):
                combined[int(candidate.id)] = candidate
        messages = sorted(combined.values(), key=lambda m: int(m.id))
    selected = units(worker, route, messages)
    for unit in selected:
        if any(topic_id(m) not in (None, route["source_topic"]) for m in unit):
            raise RuntimeError("Owner live topic mismatch")
        if getattr(unit[0], "grouped_id", None):
            dates = [m.date.timestamp() for m in unit if getattr(m, "date", None)]
            if dates and time.time() - max(dates) < 2:
                break
        outcome, destinations = await copy_unit(worker, route, unit)
        entry["last_source_id"] = max(int(m.id) for m in unit)
        entry["last_live_outcome"] = outcome
        entry["last_live_at"] = time.time()
        if destinations:
            entry["last_live_dest_ids"] = destinations
        save_state(path, state)
        log.warning("[OWNER LIVE POST] contract=%s outcome=%s source_id=%s",
                    policy.identity(route), outcome, entry["last_source_id"])


async def run_owner_requested_routes(worker):
    path = Path(worker.DATA_DIR) / STATE_FILE
    state = load_state(path)
    # Let the existing runtime install its reply/media wrappers first.
    for _ in range(240):
        if getattr(worker, "GLOBAL_REPLY_HARDENING", None) is not None:
            break
        await asyncio.sleep(0.25)
    else:
        raise RuntimeError("Owner routes waiting for production copy hardening")
    active = []
    for contract in TARGETS:
        matches = [r for r in worker.ROUTES if route_identity(r) == contract[:4]]
        if len(matches) != 1:
            log.error("[OWNER ROUTE MISSING] contract=%s matches=%s", contract[:4], len(matches))
            continue
        route = matches[0]
        key = policy.identity(route)
        entry = state.setdefault(key, {})
        if policy.is_stopped(route) or entry.get("status") == "stopped":
            continue
        # A transient failure holds this sequence; the 20-post jobs cannot
        # overtake the first 50-post job. Permanent access/payment errors stop
        # only the failing route under the owner's explicit instruction.
        while True:
            try:
                await bootstrap_route(worker, route, contract[4], entry, state, path)
                active.append(route)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason = policy.permanent_access_error(exc)
                if reason and stop(worker, route, entry, state, path, reason):
                    break
                log.warning("[OWNER BACKFILL WAIT] contract=%s error=%s", key, type(exc).__name__)
                await asyncio.sleep(15)
    log.warning("[OWNER REQUESTED LIVE READY] active_routes=%s SEQUENTIAL_BACKFILLS_COMPLETE=True EVENT_WAKE=True",
                len(active))
    # Speed (owner request 3 Nov 2026): a NewMessage in a route's exact source
    # topic wakes that route at once; a full sweep every LIVE_POLL_SECONDS
    # remains the backstop for any missed update.
    wake = asyncio.Event()
    pending = set()
    by_source = {}
    for route in active:
        by_source.setdefault(int(route["source_chat"]), []).append(route)

    async def on_new(event):
        topic = topic_id(event.message)
        for candidate in by_source.get(int(event.chat_id or 0), ()):
            if topic == candidate["source_topic"]:
                pending.add(policy.identity(candidate))
                wake.set()

    add_handler = getattr(worker.client, "add_event_handler", None)
    if active and add_handler:
        add_handler(on_new, events.NewMessage(chats=list(by_source)))
    last_full = 0.0
    while active:
        full = not pending or time.monotonic() - last_full >= LIVE_POLL_SECONDS
        due = [r for r in active if full or policy.identity(r) in pending]
        if full:
            last_full = time.monotonic()
        pending.clear()
        wake.clear()
        for route in due:
            if route not in active:
                continue
            entry = state[policy.identity(route)]
            if policy.is_stopped(route):
                active.remove(route)
                continue
            try:
                await live_cycle(worker, route, entry, state, path)
            except asyncio.CancelledError:
                raise
            except FloodWaitError as exc:
                await asyncio.sleep(max(1, int(exc.seconds)) + 1)
            except Exception as exc:
                reason = policy.permanent_access_error(exc)
                if reason and stop(worker, route, entry, state, path, reason):
                    active.remove(route)
                else:
                    log.warning("[OWNER LIVE WAIT] contract=%s error=%s", policy.identity(route), type(exc).__name__)
        try:
            await asyncio.wait_for(wake.wait(), LIVE_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


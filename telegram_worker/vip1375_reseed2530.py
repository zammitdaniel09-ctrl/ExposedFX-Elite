"""One-time VIP1375 rebuild from relay topic 2530.

The old relay115 -> VIP1375 path is retired. On the first deployment after the
route swap this module:
  1) purges every child message in VIP topic 1375 (root preserved);
  2) clears stale message-map entries for that destination;
  3) copies the latest 50 logical posts from relay topic 2530 oldest->newest
     through the normal production copy functions;
  4) writes a durable done marker so restarts never purge/reseed again.

The caller runs this before private-live/watchdog/FASTVIP2 tasks start.
"""

import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

SOURCE_CHAT = -1004367822325
SOURCE_TOPIC = 2530
DEST_CHAT = -1003726286301
DEST_TOPIC = 1375
COUNT = 50
FETCH_LIMIT = 500
DONE_FILE = "vip1375_reseed2530_v1.done.json"
PROGRESS_FILE = "vip1375_reseed2530_v1.progress.json"


def _path(main_module, name):
    data_dir = Path(getattr(main_module, "DATA_DIR", Path("./data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / name


def _write(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


async def _call(call, log, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            log.warning(
                "[VIP1375 RESEED FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label, attempt, attempts, wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"flood-wait retry budget exhausted: {label}")


def _route(main_module):
    matches = [
        r for r in main_module.ROUTES
        if int(r.get("source_chat", 0)) == SOURCE_CHAT
        and int(r.get("source_topic", 0) or 0) == SOURCE_TOPIC
        and int(r.get("dest_chat", 0)) == DEST_CHAT
        and int(r.get("dest_topic", 0)) == DEST_TOPIC
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"VIP1375 reseed expected exactly one relay2530 route; found={len(matches)}"
        )

    # No other route is allowed to write into the rebuilt destination.
    extras = [
        r for r in main_module.ROUTES
        if int(r.get("dest_chat", 0)) == DEST_CHAT
        and int(r.get("dest_topic", 0)) == DEST_TOPIC
        and r is not matches[0]
    ]
    if extras:
        raise RuntimeError(
            "VIP1375 has unexpected extra writers: "
            + ",".join(str(r.get("name")) for r in extras)
        )
    return matches[0]


def disable_route_fail_closed(main_module, logger, reason):
    removed = []
    for route in list(getattr(main_module, "ROUTES", [])):
        if (
            int(route.get("source_chat", 0)) == SOURCE_CHAT
            and int(route.get("source_topic", 0) or 0) == SOURCE_TOPIC
            and int(route.get("dest_chat", 0)) == DEST_CHAT
            and int(route.get("dest_topic", 0)) == DEST_TOPIC
        ):
            removed.append(route)
            main_module.ROUTES.remove(route)

    groups = getattr(main_module, "FASTVIP2_GROUPS", None)
    if isinstance(groups, dict):
        groups.pop((SOURCE_CHAT, SOURCE_TOPIC), None)

    logger.error(
        "[VIP1375 RESEED FAIL-CLOSED] reason=%s route_removed=%s live_forwarding_1375=False",
        reason, len(removed),
    )


async def _purge_topic(client, log):
    total = 0
    rounds = 0

    while True:
        rounds += 1
        raw = await _call(
            lambda: client.get_messages(DEST_CHAT, limit=1000, reply_to=DEST_TOPIC),
            log,
            f"purge-fetch:{DEST_CHAT}_{DEST_TOPIC}",
        )
        ids = sorted({
            int(m.id)
            for m in list(raw or [])
            if getattr(m, "id", None) and int(m.id) != DEST_TOPIC
        })
        if not ids:
            break

        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            await _call(
                lambda b=batch: client.delete_messages(DEST_CHAT, b, revoke=True),
                log,
                f"purge-delete:{len(batch)}",
            )
            total += len(batch)
            await asyncio.sleep(0.2)

        if rounds >= 20:
            raise RuntimeError("VIP1375 purge exceeded 20 rounds")

    log.warning(
        "[VIP1375 PURGE DONE] dest=%s_%s deleted=%s root_preserved=True",
        DEST_CHAT, DEST_TOPIC, total,
    )
    return total


def _clear_stale_maps(main_module, log):
    message_map = getattr(main_module, "message_map", None)
    if not isinstance(message_map, dict):
        return 0

    remove = []
    for key in list(message_map.keys()):
        parts = str(key).split(":")
        if len(parts) != 4:
            continue
        try:
            if int(parts[2]) == DEST_CHAT and int(parts[3]) == DEST_TOPIC:
                remove.append(key)
        except Exception:
            continue

    for key in remove:
        message_map.pop(key, None)

    save_map = getattr(main_module, "save_map", None)
    if remove and callable(save_map):
        save_map()

    log.warning("[VIP1375 MAP RESET] removed_entries=%s", len(remove))
    return len(remove)


def _build_units(main_module, messages):
    messages = [
        m for m in list(messages or [])
        if getattr(m, "id", None)
        and int(m.id) != SOURCE_TOPIC
        and bool(main_module.text_of(m) or main_module.is_real_media(m))
    ]
    messages.sort(key=lambda m: int(m.id))

    units = []
    used = set()
    for message in messages:
        mid = int(message.id)
        if mid in used:
            continue
        gid = getattr(message, "grouped_id", None)
        if gid:
            unit = [m for m in messages if getattr(m, "grouped_id", None) == gid]
            unit.sort(key=lambda m: int(m.id))
        else:
            unit = [message]
        for item in unit:
            used.add(int(item.id))
        units.append(unit)
    return units[-COUNT:]


async def run_vip1375_reseed2530_once(main_module, logger=None):
    log = logger or main_module.log
    done_path = _path(main_module, DONE_FILE)
    progress_path = _path(main_module, PROGRESS_FILE)

    if done_path.exists():
        log.warning("[VIP1375 RESEED ALREADY DONE] resent=False purge_repeated=False")
        return True

    route = _route(main_module)
    client = main_module.client

    # Ensure the hardened reply wrappers installed by runtime_guard are ready.
    for _ in range(120):
        if getattr(main_module, "GLOBAL_REPLY_HARDENING", None) is not None:
            break
        await asyncio.sleep(0.1)

    source_root = await _call(
        lambda: client.get_messages(SOURCE_CHAT, ids=SOURCE_TOPIC),
        log,
        f"source-root:{SOURCE_CHAT}_{SOURCE_TOPIC}",
    )
    dest_root = await _call(
        lambda: client.get_messages(DEST_CHAT, ids=DEST_TOPIC),
        log,
        f"dest-root:{DEST_CHAT}_{DEST_TOPIC}",
    )
    if not source_root:
        raise RuntimeError("relay topic 2530 is inaccessible/missing")
    if not dest_root:
        raise RuntimeError("VIP topic 1375 is inaccessible/missing")

    _write(progress_path, {
        "status": "purging",
        "source_chat": SOURCE_CHAT,
        "source_topic": SOURCE_TOPIC,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "started_at": time.time(),
    })

    deleted = await _purge_topic(client, log)
    stale_maps = _clear_stale_maps(main_module, log)

    raw = await _call(
        lambda: client.get_messages(
            SOURCE_CHAT,
            limit=FETCH_LIMIT,
            reply_to=SOURCE_TOPIC,
        ),
        log,
        f"source-history:{SOURCE_CHAT}_{SOURCE_TOPIC}",
    )
    selected = _build_units(main_module, raw)
    if not selected:
        raise RuntimeError("relay topic 2530 returned no copyable messages")

    _write(progress_path, {
        "status": "seeding",
        "deleted": deleted,
        "stale_maps_removed": stale_maps,
        "selected": [[int(m.id) for m in unit] for unit in selected],
        "started_at": time.time(),
    })

    sent = 0
    filtered = 0

    for index, unit in enumerate(selected, start=1):
        first = unit[0]
        ids = [int(m.id) for m in unit]
        grouped = bool(getattr(first, "grouped_id", None) and len(unit) > 1)

        if grouped:
            result = await main_module.copy_album(unit, route)
        else:
            result = await main_module.copy_one(first, route, edited=False, ensure_reply=True)

        # Production policy filters intentionally return None. Preserve them,
        # but report filtered vs visibly sent accurately.
        if result is None:
            filtered += 1
            log.warning(
                "[VIP1375 RESEED FILTERED] post=%s/%s ids=%s",
                index, len(selected), ids,
            )
        else:
            sent += 1
            log.warning(
                "[VIP1375 RESEED SENT] post=%s/%s ids=%s",
                index, len(selected), ids,
            )
        await asyncio.sleep(0.35)

    result = {
        "done": True,
        "source_chat": SOURCE_CHAT,
        "source_topic": SOURCE_TOPIC,
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "deleted": deleted,
        "selected": len(selected),
        "sent": sent,
        "filtered": filtered,
        "completed_at": time.time(),
    }
    _write(done_path, result)
    _write(progress_path, {**result, "status": "done"})

    log.warning(
        "[VIP1375 RESEED DONE] deleted=%s selected=%s sent=%s filtered=%s source=%s_%s dest=%s_%s live_forwarding=True old_relay115=False",
        deleted, len(selected), sent, filtered,
        SOURCE_CHAT, SOURCE_TOPIC, DEST_CHAT, DEST_TOPIC,
    )
    return True

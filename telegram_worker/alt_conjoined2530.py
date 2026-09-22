"""One-time combined last-50 seed for ALT routes into relay topic 2530.

Two source topics in the same Telegram forum are treated as one chronological
stream. Live forwarding remains owned by the legacy ALT worker; this module
only seeds the latest 50 logical posts once, using the exact same copy_unit
pipeline (filters, albums, replies, mappings, translation hooks if applicable).
"""

import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

SOURCE_CHAT = -1004349952583
SOURCE_TOPICS = (185, 9)
DEST_CHAT = -1004367822325
DEST_TOPIC = 2530
COUNT = 50
FETCH_PER_TOPIC = 300
DONE_FILE = "alt_conjoined2530_last50_v1.done.json"
PROGRESS_FILE = "alt_conjoined2530_last50_v1.progress.json"


def _path(legacy, name):
    return Path(legacy.DATA_DIR) / name


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
                "[ALT CONJOINED2530 FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label, attempt, attempts, wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"flood-wait retry budget exhausted: {label}")


def _routes(legacy):
    matches = {}
    for topic in SOURCE_TOPICS:
        found = [
            r for r in legacy.ROUTES
            if int(r.get("source_chat", 0)) == SOURCE_CHAT
            and int(r.get("source_topic", 0) or 0) == int(topic)
            and int(r.get("dest_chat", 0)) == DEST_CHAT
            and int(r.get("dest_topic", 0)) == DEST_TOPIC
        ]
        if len(found) != 1:
            raise RuntimeError(
                f"ALT conjoined2530 expected one route for topic={topic}; found={len(found)}"
            )
        matches[int(topic)] = found[0]
    return matches


def _copyable(legacy, message, topic):
    if message is None:
        return False
    if int(getattr(message, "id", 0) or 0) == int(topic):
        return False
    return bool(legacy.text_of(message) or legacy.real_media(message))


async def run_alt_conjoined2530_last50_once(legacy, logger=None):
    log = logger or legacy.log
    done_path = _path(legacy, DONE_FILE)
    progress_path = _path(legacy, PROGRESS_FILE)

    if done_path.exists():
        log.warning("[ALT CONJOINED2530 LAST50 ALREADY DONE] resent=False")
        return True

    # This task is started before legacy.main(); wait for that main routine to
    # connect/authorize the ALT account, then warm dialogs ourselves so the new
    # source can be resolved even if it was not in the old entity cache.
    while not legacy.client.is_connected():
        await asyncio.sleep(0.25)
    while not await legacy.client.is_user_authorized():
        await asyncio.sleep(0.5)

    await legacy.client.get_dialogs(limit=None)
    routes = _routes(legacy)

    # Fail closed if either source topic or destination topic is inaccessible.
    for topic, route in routes.items():
        root = await _call(
            lambda t=topic: legacy.client.get_messages(SOURCE_CHAT, ids=int(t)),
            log,
            f"source-root:{SOURCE_CHAT}_{topic}",
        )
        if not root:
            raise RuntimeError(f"source topic missing: {SOURCE_CHAT}_{topic}")

    dest_root = await _call(
        lambda: legacy.client.get_messages(DEST_CHAT, ids=DEST_TOPIC),
        log,
        f"dest-root:{DEST_CHAT}_{DEST_TOPIC}",
    )
    if not dest_root:
        raise RuntimeError(f"destination topic missing: {DEST_CHAT}_{DEST_TOPIC}")

    combined = []
    for topic, route in routes.items():
        raw = await _call(
            lambda r=route: legacy.fetch_route_messages(r, FETCH_PER_TOPIC),
            log,
            f"source-history:{SOURCE_CHAT}_{topic}",
        )
        units = legacy.build_units(list(raw or []))
        for unit in units:
            unit = [m for m in unit if _copyable(legacy, m, topic)]
            if not unit:
                continue
            unit.sort(key=lambda m: int(m.id))
            combined.append((max(int(m.id) for m in unit), topic, route, unit))

    # Same source chat => Telegram message ids give a single chronological
    # ordering across both forum topics. Albums remain one logical post.
    combined.sort(key=lambda item: item[0])
    selected = combined[-COUNT:]

    if not selected:
        raise RuntimeError("no copyable messages found across source topics 185 and 9")

    selected_desc = [
        {"topic": topic, "ids": [int(m.id) for m in unit]}
        for _, topic, _, unit in selected
    ]
    _write(progress_path, {
        "status": "running",
        "source_chat": SOURCE_CHAT,
        "source_topics": list(SOURCE_TOPICS),
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "selected": selected_desc,
        "started_at": time.time(),
    })

    log.warning(
        "[ALT CONJOINED2530 LAST50 START] source=%s topics=%s dest=%s_%s selected=%s order=combined_oldest_to_newest",
        SOURCE_CHAT, list(SOURCE_TOPICS), DEST_CHAT, DEST_TOPIC, len(selected),
    )

    sent = 0
    already_mapped = 0
    filtered = 0

    for index, (_, topic, route, unit) in enumerate(selected, start=1):
        ids = [int(m.id) for m in unit]
        if all(bool(legacy.mapped_ids(route, m.id)) for m in unit):
            already_mapped += 1
            log.info(
                "[ALT CONJOINED2530 ALREADY MAPPED] post=%s/%s topic=%s ids=%s",
                index, len(selected), topic, ids,
            )
            continue

        # copy_unit owns filters, album handling, persistent mapping and the
        # installed ALT reply hardening. It returns True for policy-filtered
        # units as well, so determine visible delivery by checking the map.
        ok = await legacy.copy_unit(route, unit, "conjoined2530_last50")
        if not ok:
            raise RuntimeError(f"copy_unit failed topic={topic} ids={ids}")

        mapped_after = all(bool(legacy.mapped_ids(route, m.id)) for m in unit)
        if mapped_after:
            sent += 1
            log.warning(
                "[ALT CONJOINED2530 SENT] post=%s/%s topic=%s ids=%s",
                index, len(selected), topic, ids,
            )
        else:
            filtered += 1
            log.warning(
                "[ALT CONJOINED2530 FILTERED] post=%s/%s topic=%s ids=%s",
                index, len(selected), topic, ids,
            )
        await asyncio.sleep(0.35)

    result = {
        "done": True,
        "source_chat": SOURCE_CHAT,
        "source_topics": list(SOURCE_TOPICS),
        "dest_chat": DEST_CHAT,
        "dest_topic": DEST_TOPIC,
        "selected": len(selected),
        "sent": sent,
        "already_mapped": already_mapped,
        "filtered": filtered,
        "completed_at": time.time(),
    }
    _write(done_path, result)
    _write(progress_path, {**result, "status": "done"})

    log.warning(
        "[ALT CONJOINED2530 LAST50 DONE] selected=%s sent=%s already_mapped=%s filtered=%s live_forwarding=True",
        len(selected), sent, already_mapped, filtered,
    )
    return True

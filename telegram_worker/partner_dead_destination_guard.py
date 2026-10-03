"""Permanent block + one-time purge for retired partner topics.

Retired destinations:
- -1003885074241 / 4663
- -1003885074241 / 4665

Behavior:
1. Remove any runtime route whose destination is one of the retired topics.
2. Wrap the production copy pipeline so even an accidentally reintroduced
   route cannot send into either topic.
3. Delete every existing child message from both topics once at startup.
4. Remove stale durable source->destination map entries for those topics.

The forum topic root messages themselves are preserved.
"""

import asyncio
import logging
from pathlib import Path

from telethon.errors import FloodWaitError


log = logging.getLogger("partner-dead-destination-guard")

DEST_CHAT = -1003885074241
DEAD_TOPICS = {4663, 4665}
DELETE_BATCH = 100


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _dead_destination(route):
    if not isinstance(route, dict):
        return False
    return (
        _as_int(route.get("dest_chat")) == DEST_CHAT
        and _as_int(route.get("dest_topic")) in DEAD_TOPICS
    )


async def _delete_batch(client, ids, logger, topic):
    ids = [
        int(value)
        for value in ids
        if _as_int(value) not in (None, topic)
    ]
    if not ids:
        return 0

    while True:
        try:
            await client.delete_messages(
                DEST_CHAT,
                ids,
                revoke=True,
            )
            return len(ids)
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[PARTNER DEAD PURGE FLOODWAIT] dest=%s_%s wait=%ss batch=%s",
                DEST_CHAT,
                topic,
                wait_for,
                len(ids),
            )
            await asyncio.sleep(wait_for)


async def _purge_topic(client, topic, logger):
    deleted = 0
    found = 0
    batch = []

    while True:
        try:
            async for message in client.iter_messages(
                DEST_CHAT,
                reply_to=topic,
            ):
                mid = _as_int(getattr(message, "id", None))
                if mid is None or mid == topic:
                    continue

                found += 1
                batch.append(mid)

                if len(batch) >= DELETE_BATCH:
                    deleted += await _delete_batch(
                        client,
                        batch,
                        logger,
                        topic,
                    )
                    batch = []

            if batch:
                deleted += await _delete_batch(
                    client,
                    batch,
                    logger,
                    topic,
                )
                batch = []
            break

        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[PARTNER DEAD PURGE FETCH FLOODWAIT] dest=%s_%s wait=%ss",
                DEST_CHAT,
                topic,
                wait_for,
            )
            await asyncio.sleep(wait_for)

    # Verify with a fresh thread query. Any leftovers get a second delete pass.
    remaining = []
    try:
        check = await client.get_messages(
            DEST_CHAT,
            limit=200,
            reply_to=topic,
        )
        remaining = [
            int(message.id)
            for message in list(check or [])
            if _as_int(getattr(message, "id", None)) not in (None, topic)
        ]
    except Exception as exc:
        logger.warning(
            "[PARTNER DEAD PURGE VERIFY FAILED] dest=%s_%s %s: %s",
            DEST_CHAT,
            topic,
            type(exc).__name__,
            exc,
        )

    if remaining:
        deleted += await _delete_batch(
            client,
            remaining,
            logger,
            topic,
        )

    logger.warning(
        "[PARTNER DEAD PURGE DONE] dest=%s_%s found=%s deleted=%s "
        "verify_remaining=%s root_preserved=True",
        DEST_CHAT,
        topic,
        found,
        deleted,
        max(0, len(remaining)),
    )

    return {
        "topic": topic,
        "found": found,
        "deleted": deleted,
        "verify_remaining": max(0, len(remaining)),
    }


def _purge_dead_message_map_entries(main_module, logger):
    message_map = getattr(main_module, "message_map", None)
    if not isinstance(message_map, dict):
        return 0

    removed = 0
    for key in list(message_map.keys()):
        parts = str(key).split(":")
        if len(parts) != 4:
            continue

        dest_chat = _as_int(parts[2])
        dest_topic = _as_int(parts[3])
        if dest_chat == DEST_CHAT and dest_topic in DEAD_TOPICS:
            message_map.pop(key, None)
            removed += 1

    if removed:
        save_fn = getattr(main_module, "save_map", None)
        if callable(save_fn):
            save_fn()

    logger.warning(
        "[PARTNER DEAD MAP CLEANUP] dest_chat=%s topics=%s removed=%s",
        DEST_CHAT,
        sorted(DEAD_TOPICS),
        removed,
    )
    return removed


def install_partner_dead_destination_guard(main_module, logger=None):
    logger = logger or log

    if getattr(main_module, "PARTNER_DEAD_DESTINATION_GUARD_INSTALLED", False):
        return getattr(
            main_module,
            "PARTNER_DEAD_DESTINATION_GUARD_STATE",
            {},
        )

    client = getattr(main_module, "client", None)
    routes = getattr(main_module, "ROUTES", None)
    original_copy_one = getattr(main_module, "copy_one", None)
    original_copy_album = getattr(main_module, "copy_album", None)

    if client is None or not isinstance(routes, list):
        raise RuntimeError(
            "partner dead-destination guard requires client and ROUTES"
        )
    if not callable(original_copy_one) or not callable(original_copy_album):
        raise RuntimeError(
            "partner dead-destination guard requires copy_one/copy_album"
        )

    removed_routes = [
        dict(route)
        for route in routes
        if _dead_destination(route)
    ]
    routes[:] = [
        route
        for route in routes
        if not _dead_destination(route)
    ]

    async def guarded_copy_one(message, route, *args, **kwargs):
        if _dead_destination(route):
            logger.error(
                "[PARTNER DEAD SEND BLOCKED] route=%r source=%s_%s dest=%s_%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                route.get("dest_chat"),
                route.get("dest_topic"),
            )
            return None
        return await original_copy_one(
            message,
            route,
            *args,
            **kwargs,
        )

    async def guarded_copy_album(messages, route, *args, **kwargs):
        if _dead_destination(route):
            logger.error(
                "[PARTNER DEAD ALBUM BLOCKED] route=%r source=%s_%s dest=%s_%s",
                route.get("name"),
                route.get("source_chat"),
                route.get("source_topic"),
                route.get("dest_chat"),
                route.get("dest_topic"),
            )
            return None
        return await original_copy_album(
            messages,
            route,
            *args,
            **kwargs,
        )

    main_module.copy_one = guarded_copy_one
    main_module.copy_album = guarded_copy_album

    map_entries_removed = _purge_dead_message_map_entries(
        main_module,
        logger,
    )

    async def purge_once():
        results = []
        for topic in sorted(DEAD_TOPICS):
            results.append(
                await _purge_topic(
                    client,
                    topic,
                    logger,
                )
            )

        state = getattr(
            main_module,
            "PARTNER_DEAD_DESTINATION_GUARD_STATE",
            {},
        )
        state["purge_results"] = results
        state["purge_complete"] = True

        logger.error(
            "[PARTNER DEAD DESTINATIONS PURGED] dest_chat=%s topics=%s "
            "all_existing_messages_deleted=True roots_preserved=True",
            DEST_CHAT,
            sorted(DEAD_TOPICS),
        )

    purge_task = asyncio.create_task(purge_once())

    state = {
        "dest_chat": DEST_CHAT,
        "dead_topics": sorted(DEAD_TOPICS),
        "removed_routes": removed_routes,
        "removed_route_count": len(removed_routes),
        "map_entries_removed": map_entries_removed,
        "purge_complete": False,
    }

    main_module.PARTNER_DEAD_DESTINATION_GUARD_INSTALLED = True
    main_module.PARTNER_DEAD_DESTINATION_GUARD_STATE = state
    main_module.PARTNER_DEAD_DESTINATION_PURGE_TASK = purge_task

    logger.error(
        "[PARTNER DEAD DESTINATION GUARD ACTIVE] dest_chat=%s topics=%s "
        "routes_removed=%s copy_guard=True purge_started=True",
        DEST_CHAT,
        sorted(DEAD_TOPICS),
        len(removed_routes),
    )

    return state

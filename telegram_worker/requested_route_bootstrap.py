"""Requested ExposedFX route contract + safe last-50 bootstrap.

This module is intentionally narrow:
- confirms the exact source -> destination routes requested by the owner;
- disables any main-worker route whose destination is hub topic 68237;
- for the requested routes only, copies the latest 50 logical posts ONLY when
  the destination topic is empty;
- preserves the normal copy_one/copy_album path so mapping, replies, media,
  edits and existing content filters remain compatible;
- persists progress so a Railway restart can resume a bootstrap that already
  began while the destination was empty.

It never bulk-backfills non-empty destination topics.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telethon.errors import FloodWaitError


log = logging.getLogger("requested-route-bootstrap")

HUB_CHAT = -1003918958200
DISABLED_DEST_TOPIC = 68237
LAST50_COUNT = 50
SOURCE_FETCH_LIMIT = 250
STATE_FILENAME = "requested_empty_topic_last50_v1.json"

# Exact owner-requested contracts.  source_topic=None means the whole source
# group/channel.  These are deliberately data-only; the normal worker ROUTES
# remain the source of all forwarding behaviour/features.
REQUESTED_ROUTES = [
    (-1002817163788, 13419, HUB_CHAT, 6),
    (-1002385852838, 40484, HUB_CHAT, 11),
    (-1003393003521, None, HUB_CHAT, 36),
    (-1002186832814, None, HUB_CHAT, 26902),
    (-1002817163788, 16218, HUB_CHAT, 38930),
    (-1002817163788, 6357, HUB_CHAT, 4),
    (-1002817163788, 20774, HUB_CHAT, 28840),
    (-1002385852838, 35671, HUB_CHAT, 12),
    (-1002385852838, 81492, HUB_CHAT, 82629),
    (-1002385852838, 62159, HUB_CHAT, 82617),
]


def _as_int(value):
    try:
        return int(value)
    except Exception:
        return None


def _route_tuple(route):
    return (
        _as_int(route.get("source_chat")),
        _as_int(route.get("source_topic")) if route.get("source_topic") is not None else None,
        _as_int(route.get("dest_chat")),
        _as_int(route.get("dest_topic")),
    )


def _contract_key(contract):
    source_chat, source_topic, dest_chat, dest_topic = contract
    return f"{source_chat}:{source_topic}:{dest_chat}:{dest_topic}"


def _state_path(main_module):
    data_dir = Path(getattr(main_module, "DATA_DIR", Path("./data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / STATE_FILENAME


def _load_state(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save_state(path, state):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(state, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def enforce_requested_route_contracts(main_module, logger=None):
    """Disable hub topic 68237 and audit every requested forwarding contract."""
    logger = logger or log
    routes = getattr(main_module, "ROUTES", None)
    if not isinstance(routes, list):
        raise RuntimeError("main worker ROUTES list unavailable")

    removed = [
        route
        for route in routes
        if _as_int(route.get("dest_chat")) == HUB_CHAT
        and _as_int(route.get("dest_topic")) == DISABLED_DEST_TOPIC
    ]

    if removed:
        routes[:] = [
            route
            for route in routes
            if not (
                _as_int(route.get("dest_chat")) == HUB_CHAT
                and _as_int(route.get("dest_topic")) == DISABLED_DEST_TOPIC
            )
        ]

    logger.warning(
        "[REQUESTED DESTINATION DISABLED] dest=%s_%s routes_removed=%s forwarding=False",
        HUB_CHAT,
        DISABLED_DEST_TOPIC,
        len(removed),
    )

    confirmed = []
    missing = []
    duplicates = []

    for contract in REQUESTED_ROUTES:
        matches = [route for route in routes if _route_tuple(route) == contract]
        if len(matches) == 1:
            confirmed.append(matches[0])
            logger.warning(
                "[REQUESTED ROUTE CONFIRMED] source=%s_%s dest=%s_%s name=%r",
                contract[0],
                contract[1],
                contract[2],
                contract[3],
                matches[0].get("name"),
            )
        elif not matches:
            missing.append(contract)
            logger.error(
                "[REQUESTED ROUTE MISSING] source=%s_%s dest=%s_%s",
                *contract,
            )
        else:
            duplicates.append(contract)
            logger.error(
                "[REQUESTED ROUTE DUPLICATED] source=%s_%s dest=%s_%s matches=%s",
                contract[0],
                contract[1],
                contract[2],
                contract[3],
                len(matches),
            )

    state = {
        "confirmed": len(confirmed),
        "missing": missing,
        "duplicates": duplicates,
        "disabled_68237_routes": len(removed),
        "routes": confirmed,
    }
    setattr(main_module, "REQUESTED_ROUTE_CONTRACT_STATE", state)
    return state


async def _telegram_call_with_floodwait(call, logger, label, attempts=4):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[REQUESTED LAST50 FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label,
                attempt,
                attempts,
                wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"Telegram flood-wait retry budget exhausted: {label}")


def _topic_membership_ids(message):
    ids = []
    for obj in (message, getattr(message, "reply_to", None)):
        if obj is None:
            continue
        for attr in ("reply_to_top_id", "top_msg_id", "reply_to_msg_id"):
            value = _as_int(getattr(obj, attr, None))
            if value is not None and value not in ids:
                ids.append(value)
    return ids


async def _destination_topic_is_empty(client, route, logger):
    dest_chat = int(route["dest_chat"])
    dest_topic = int(route["dest_topic"])

    # Confirm the destination topic root actually exists before deciding it is
    # empty.  Missing/inaccessible destinations fail closed and are not filled.
    root = await _telegram_call_with_floodwait(
        lambda: client.get_messages(dest_chat, ids=dest_topic),
        logger,
        f"dest-root:{dest_chat}_{dest_topic}",
    )
    if not root:
        logger.error(
            "[REQUESTED LAST50 DESTINATION INACCESSIBLE] dest=%s_%s action=SKIP",
            dest_chat,
            dest_topic,
        )
        return False

    # Exact forum-topic fetch is authoritative.  Only a tiny limit is required
    # because we only care whether at least one real child message exists.
    children = await _telegram_call_with_floodwait(
        lambda: client.get_messages(dest_chat, limit=3, reply_to=dest_topic),
        logger,
        f"dest-empty-check:{dest_chat}_{dest_topic}",
    )

    real_children = [
        message
        for message in list(children or [])
        if _as_int(getattr(message, "id", None)) != dest_topic
    ]

    return len(real_children) == 0


async def _source_messages(client, route, logger):
    source_chat = int(route["source_chat"])
    source_topic = route.get("source_topic")

    if source_topic is None:
        messages = await _telegram_call_with_floodwait(
            lambda: client.get_messages(source_chat, limit=SOURCE_FETCH_LIMIT),
            logger,
            f"source-history:{source_chat}",
        )
    else:
        source_topic = int(source_topic)
        messages = await _telegram_call_with_floodwait(
            lambda: client.get_messages(
                source_chat,
                limit=SOURCE_FETCH_LIMIT,
                reply_to=source_topic,
            ),
            logger,
            f"source-topic-history:{source_chat}_{source_topic}",
        )

    return list(messages or [])


def _copyable(main_module, message, route):
    if message is None:
        return False
    if route.get("source_topic") is not None and _as_int(getattr(message, "id", None)) == _as_int(route.get("source_topic")):
        return False

    text_fn = getattr(main_module, "text_of", None)
    media_fn = getattr(main_module, "is_real_media", None)

    text = text_fn(message) if callable(text_fn) else (getattr(message, "message", None) or "")
    media = media_fn(message) if callable(media_fn) else bool(getattr(message, "media", None))
    return bool(text or media)


def _build_units(main_module, route, messages):
    messages = [m for m in messages if _copyable(main_module, m, route)]
    messages.sort(key=lambda m: int(getattr(m, "id", 0) or 0))

    units = []
    used = set()
    for message in messages:
        mid = int(getattr(message, "id", 0) or 0)
        if not mid or mid in used:
            continue

        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            unit = [m for m in messages if getattr(m, "grouped_id", None) == grouped_id]
            unit.sort(key=lambda m: int(getattr(m, "id", 0) or 0))
        else:
            unit = [message]

        for item in unit:
            used.add(int(getattr(item, "id", 0) or 0))
        units.append(unit)

    # Count a Telegram album as one logical post so it is never cut in half.
    return units[-LAST50_COUNT:]


async def _reload_unit(client, route, ids, logger):
    source_chat = int(route["source_chat"])
    raw = await _telegram_call_with_floodwait(
        lambda: client.get_messages(source_chat, ids=[int(value) for value in ids]),
        logger,
        f"reload-unit:{source_chat}:{','.join(str(v) for v in ids)}",
    )
    if isinstance(raw, list):
        messages = [m for m in raw if m is not None]
    else:
        messages = [raw] if raw is not None else []
    messages.sort(key=lambda m: int(getattr(m, "id", 0) or 0))
    if len(messages) != len(ids):
        raise RuntimeError(f"Could not reload complete source unit ids={ids}")
    return messages


def _unit_already_mapped(main_module, route, unit):
    existing = getattr(main_module, "existing_destination_ids", None)
    if not callable(existing):
        return False
    return all(bool(existing(message, route)) for message in unit)


async def _copy_unit(main_module, route, unit):
    if not unit:
        return True

    # Existing filters remain authoritative.  If a message is intentionally
    # blocked by those policies, consider that bootstrap unit handled rather
    # than bypassing the production filter.
    mention_filter = getattr(main_module, "unit_has_username_mention", None)
    if callable(mention_filter) and mention_filter(unit):
        return True

    first = unit[0]
    grouped_id = getattr(first, "grouped_id", None)

    if grouped_id and len(unit) > 1:
        copy_album = getattr(main_module, "copy_album", None)
        if not callable(copy_album):
            raise RuntimeError("worker copy_album unavailable")
        result = await copy_album(unit, route)
    else:
        copy_one = getattr(main_module, "copy_one", None)
        if not callable(copy_one):
            raise RuntimeError("worker copy_one unavailable")
        result = await copy_one(first, route, edited=False, ensure_reply=True)

    # Production copy functions raise for transport failures.  None is normally
    # an intentional policy skip (sender/link/etc.), so do not retry forever.
    return True if result is None else bool(result)


async def run_requested_empty_topic_last50(main_module, logger=None):
    """Fill only currently-empty requested destination topics, once and safely."""
    logger = logger or log
    client = getattr(main_module, "client", None)
    routes = getattr(main_module, "ROUTES", None)
    if client is None or not isinstance(routes, list):
        raise RuntimeError("main worker client/routes unavailable")

    state_path = _state_path(main_module)
    state = _load_state(state_path)

    logger.warning(
        "[REQUESTED LAST50 START] routes=%s empty_destinations_only=True count=%s preserve_copy_pipeline=True",
        len(REQUESTED_ROUTES),
        LAST50_COUNT,
    )

    for contract in REQUESTED_ROUTES:
        key = _contract_key(contract)
        route_matches = [route for route in routes if _route_tuple(route) == contract]
        if len(route_matches) != 1:
            logger.error(
                "[REQUESTED LAST50 ROUTE INVALID] key=%s matches=%s action=SKIP",
                key,
                len(route_matches),
            )
            continue

        route = route_matches[0]
        entry = state.get(key) if isinstance(state.get(key), dict) else {}

        if entry.get("status") in {"done", "skipped_nonempty", "skipped_empty_source"}:
            logger.info(
                "[REQUESTED LAST50 STATE SKIP] key=%s status=%s",
                key,
                entry.get("status"),
            )
            continue

        selected_units = entry.get("selected_units") if entry.get("status") == "running" else None

        if not selected_units:
            empty = await _destination_topic_is_empty(client, route, logger)
            if not empty:
                state[key] = {
                    "status": "skipped_nonempty",
                    "checked_at": time.time(),
                }
                _save_state(state_path, state)
                logger.warning(
                    "[REQUESTED LAST50 NONEMPTY SKIP] source=%s_%s dest=%s_%s copied=0",
                    *contract,
                )
                await asyncio.sleep(0.5)
                continue

            source_messages = await _source_messages(client, route, logger)
            units = _build_units(main_module, route, source_messages)
            if not units:
                state[key] = {
                    "status": "skipped_empty_source",
                    "checked_at": time.time(),
                }
                _save_state(state_path, state)
                logger.warning(
                    "[REQUESTED LAST50 EMPTY SOURCE] source=%s_%s dest=%s_%s copied=0",
                    *contract,
                )
                await asyncio.sleep(0.5)
                continue

            selected_units = [
                [int(getattr(message, "id", 0) or 0) for message in unit]
                for unit in units
            ]

            # Re-check immediately before committing the bootstrap selection.
            # This closes most of the race where a live message lands while the
            # source history was being fetched.
            if not await _destination_topic_is_empty(client, route, logger):
                state[key] = {
                    "status": "skipped_nonempty",
                    "checked_at": time.time(),
                    "reason": "became_nonempty_before_bootstrap",
                }
                _save_state(state_path, state)
                logger.warning(
                    "[REQUESTED LAST50 RACE NONEMPTY SKIP] source=%s_%s dest=%s_%s copied=0",
                    *contract,
                )
                await asyncio.sleep(0.5)
                continue

            state[key] = {
                "status": "running",
                "selected_units": selected_units,
                "completed": [],
                "started_at": time.time(),
            }
            _save_state(state_path, state)
            entry = state[key]

            logger.warning(
                "[REQUESTED LAST50 EMPTY DESTINATION CONFIRMED] source=%s_%s dest=%s_%s selected_posts=%s",
                contract[0],
                contract[1],
                contract[2],
                contract[3],
                len(selected_units),
            )

        completed = set(str(token) for token in (entry.get("completed") or []))

        for index, ids in enumerate(selected_units, start=1):
            ids = [int(value) for value in ids]
            token = ",".join(str(value) for value in ids)
            if token in completed:
                continue

            unit = await _reload_unit(client, route, ids, logger)

            if _unit_already_mapped(main_module, route, unit):
                logger.info(
                    "[REQUESTED LAST50 ALREADY MAPPED] dest=%s_%s post=%s/%s ids=%s",
                    contract[2],
                    contract[3],
                    index,
                    len(selected_units),
                    ids,
                )
            else:
                copied = await _copy_unit(main_module, route, unit)
                if not copied:
                    raise RuntimeError(
                        f"requested last50 copy returned false key={key} ids={ids}"
                    )
                logger.warning(
                    "[REQUESTED LAST50 COPIED] source=%s_%s dest=%s_%s post=%s/%s ids=%s",
                    contract[0],
                    contract[1],
                    contract[2],
                    contract[3],
                    index,
                    len(selected_units),
                    ids,
                )

            completed.add(token)
            state[key]["completed"] = sorted(completed)
            state[key]["updated_at"] = time.time()
            _save_state(state_path, state)
            await asyncio.sleep(0.35)

        state[key]["status"] = "done"
        state[key]["completed_at"] = time.time()
        _save_state(state_path, state)
        logger.warning(
            "[REQUESTED LAST50 DONE] source=%s_%s dest=%s_%s posts=%s empty_at_start=True",
            contract[0],
            contract[1],
            contract[2],
            contract[3],
            len(selected_units),
        )
        await asyncio.sleep(0.75)

    logger.warning("[REQUESTED LAST50 COMPLETE] all_requested_routes_checked=True")
    return state

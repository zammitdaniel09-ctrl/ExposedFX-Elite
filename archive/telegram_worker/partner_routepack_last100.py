"""One-time last-100 bootstrap for the September partner route pack.

This module seeds the latest 100 logical posts for each fully-specified route
requested by the owner. It uses the production copy_one/copy_album functions,
so the existing filters, reply hardening, media handling and persistent mapping
remain authoritative. Albums count as one logical post.

The source -1003087047858 is intentionally NOT included because the owner's
message omitted its destination URL/topic; that route must not be guessed.
"""

import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

COUNT = 100
FETCH_LIMIT = 1400
STATE_FILE = "partner_routepack_last100_v1.json"

CONTRACTS = (
    (-1004367822325, 2583, -1003918958200, 68237),
    (-1004367822325, 2585, -1004442967052, 2),
    (-1004367822325, 508, -1004442967052, 9),
    (-1004367822325, 115, -1004442967052, 11),
    (-1004367822325, 2, -1004442967052, 15),
    (-1003887896696, None, -1004442967052, 20),
    (-1002385852838, 8, -1004442967052, 22),
)


def _path(main_module):
    data_dir = Path(getattr(main_module, "DATA_DIR", Path("./data")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / STATE_FILE


def _load(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _save(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _route_tuple(route):
    return (
        int(route.get("source_chat", 0)),
        int(route["source_topic"]) if route.get("source_topic") is not None else None,
        int(route.get("dest_chat", 0)),
        int(route.get("dest_topic", 0)),
    )


def _key(contract):
    return ":".join("None" if value is None else str(value) for value in contract)


async def _call(call, logger, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[PARTNER LAST100 FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label, attempt, attempts, wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"flood-wait retry budget exhausted: {label}")


def _copyable(main_module, message, source_topic):
    if message is None:
        return False
    if source_topic is not None and int(getattr(message, "id", 0) or 0) == int(source_topic):
        return False
    return bool(main_module.text_of(message) or main_module.is_real_media(message))


def _build_units(main_module, messages, source_topic):
    messages = [
        message for message in list(messages or [])
        if _copyable(main_module, message, source_topic)
    ]
    messages.sort(key=lambda message: int(message.id))

    units = []
    used = set()
    for message in messages:
        mid = int(message.id)
        if mid in used:
            continue
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            unit = [
                candidate for candidate in messages
                if getattr(candidate, "grouped_id", None) == grouped_id
            ]
            unit.sort(key=lambda candidate: int(candidate.id))
        else:
            unit = [message]
        for item in unit:
            used.add(int(item.id))
        units.append(unit)

    return units[-COUNT:]


async def run_partner_routepack_last100_once(main_module, logger=None):
    log = logger or main_module.log
    path = _path(main_module)
    state = _load(path)

    # Wait briefly for runtime_guard to install global reply hardening.
    for _ in range(150):
        if getattr(main_module, "GLOBAL_REPLY_HARDENING", None) is not None:
            break
        await asyncio.sleep(0.1)

    completed_routes = 0
    failed_routes = 0

    for contract in CONTRACTS:
        key = _key(contract)

        if isinstance(state.get(key), dict) and state[key].get("status") == "done":
            completed_routes += 1
            log.warning("[PARTNER LAST100 STATE SKIP] key=%s already_done=True", key)
            continue

        try:
            matches = [route for route in main_module.ROUTES if _route_tuple(route) == contract]
            if len(matches) != 1:
                raise RuntimeError(f"route match count={len(matches)}")
            route = matches[0]

            # Fail closed on missing/inaccessible destination topic.
            root = await _call(
                lambda r=route: main_module.client.get_messages(
                    r["dest_chat"], ids=int(r["dest_topic"])
                ),
                log,
                f"dest-root:{contract[2]}_{contract[3]}",
            )
            if not root:
                raise RuntimeError(f"destination topic missing: {contract[2]}_{contract[3]}")

            if contract[1] is None:
                raw = await _call(
                    lambda r=route: main_module.client.get_messages(
                        r["source_chat"], limit=FETCH_LIMIT
                    ),
                    log,
                    f"source-history:{contract[0]}",
                )
            else:
                raw = await _call(
                    lambda r=route: main_module.client.get_messages(
                        r["source_chat"],
                        limit=FETCH_LIMIT,
                        reply_to=int(r["source_topic"]),
                    ),
                    log,
                    f"source-topic-history:{contract[0]}_{contract[1]}",
                )

            units = _build_units(main_module, raw, contract[1])
            selected = units[-COUNT:]
            selected.sort(key=lambda unit: min(int(message.id) for message in unit))

            if not selected:
                raise RuntimeError("source returned no copyable posts")

            state[key] = {
                "status": "running",
                "selected_units": [[int(message.id) for message in unit] for unit in selected],
                "started_at": time.time(),
            }
            _save(path, state)

            log.warning(
                "[PARTNER LAST100 START] source=%s_%s dest=%s_%s selected=%s order=oldest_to_newest",
                contract[0], contract[1], contract[2], contract[3], len(selected),
            )

            sent = 0
            already = 0
            filtered = 0

            for index, unit in enumerate(selected, start=1):
                ids = [int(message.id) for message in unit]
                existing = getattr(main_module, "existing_destination_ids", None)
                already_mapped = bool(existing) and all(
                    bool(existing(message, route)) for message in unit
                )

                if already_mapped:
                    already += 1
                    log.info(
                        "[PARTNER LAST100 ALREADY MAPPED] dest=%s_%s post=%s/%s ids=%s",
                        contract[2], contract[3], index, len(selected), ids,
                    )
                    continue

                first = unit[0]
                if getattr(first, "grouped_id", None) and len(unit) > 1:
                    result = await main_module.copy_album(unit, route)
                else:
                    result = await main_module.copy_one(
                        first, route, edited=False, ensure_reply=True
                    )

                if result is None:
                    filtered += 1
                    log.warning(
                        "[PARTNER LAST100 FILTERED] dest=%s_%s post=%s/%s ids=%s",
                        contract[2], contract[3], index, len(selected), ids,
                    )
                else:
                    sent += 1
                    log.warning(
                        "[PARTNER LAST100 SENT] dest=%s_%s post=%s/%s ids=%s",
                        contract[2], contract[3], index, len(selected), ids,
                    )

                await asyncio.sleep(0.30)

            state[key] = {
                "status": "done",
                "selected": len(selected),
                "sent": sent,
                "already_mapped": already,
                "filtered": filtered,
                "completed_at": time.time(),
            }
            _save(path, state)

            completed_routes += 1
            log.warning(
                "[PARTNER LAST100 DONE] source=%s_%s dest=%s_%s selected=%s sent=%s already_mapped=%s filtered=%s live_forwarding=True",
                contract[0], contract[1], contract[2], contract[3],
                len(selected), sent, already, filtered,
            )

        except Exception as exc:
            failed_routes += 1
            state[key] = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "failed_at": time.time(),
            }
            _save(path, state)
            log.exception(
                "[PARTNER LAST100 ROUTE FAILED SAFE] source=%s_%s dest=%s_%s %s: %s",
                contract[0], contract[1], contract[2], contract[3],
                type(exc).__name__, exc,
            )

    log.warning(
        "[PARTNER LAST100 ALL DONE] configured=%s completed=%s failed=%s incomplete_source_3087047858_skipped=True",
        len(CONTRACTS), completed_routes, failed_routes,
    )
    return state

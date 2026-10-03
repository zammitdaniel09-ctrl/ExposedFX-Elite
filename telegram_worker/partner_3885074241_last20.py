"""One-time last-20 bootstrap for three VIP -> partner topic routes.

Contracts:
- -1003726286301 / 11   -> -1003885074241 / 4663
- -1003726286301 / 1364 -> -1003885074241 / 4660
- -1003726286301 / 7    -> -1003885074241 / 4665

The normal ROUTES table owns live forwarding. This module only imports the
latest 20 logical source posts once, oldest -> newest, using the production
copy pipeline so media, entities, filters, durable maps and mapped replies stay
compatible. Albums count as one logical post. Persistent state prevents
duplicate bootstrap sends after Railway restarts.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from telegram_worker.requested_route_bootstrap import (
    _copy_unit,
    _reload_unit,
    _route_tuple,
    _source_messages,
    _unit_already_mapped,
    _build_units,
)


log = logging.getLogger("partner-3885074241-last20")

DEST_CHAT = -1003885074241
LAST20_COUNT = 20
STATE_FILENAME = "partner_3885074241_last20_v1.json"

TARGETS = [
    (-1003726286301, 11, DEST_CHAT, 4663),
    (-1003726286301, 1364, DEST_CHAT, 4660),
    (-1003726286301, 7, DEST_CHAT, 4665),
]


def _key(contract):
    return ":".join("None" if value is None else str(value) for value in contract)


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
        json.dumps(state, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _normalise_ids(value):
    if value is None:
        return []
    if isinstance(value, dict):
        values = value.values()
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = [value]

    out = []
    for item in values:
        try:
            item = int(item)
        except Exception:
            continue
        if item and item not in out:
            out.append(item)
    return out


def _mapped_ids(main_module, route, unit):
    fn = getattr(main_module, "existing_destination_ids", None)
    if not callable(fn):
        return []

    out = []
    for message in unit:
        try:
            values = _normalise_ids(fn(message, route))
        except Exception:
            values = []
        for value in values:
            if value not in out:
                out.append(value)
    return out


async def _verify_topic_access(client, contract, logger):
    source_chat, source_topic, dest_chat, dest_topic = contract

    source_root = await client.get_messages(source_chat, ids=source_topic)
    if not source_root:
        raise RuntimeError(
            f"source topic inaccessible source={source_chat}_{source_topic}"
        )

    dest_root = await client.get_messages(dest_chat, ids=dest_topic)
    if not dest_root:
        raise RuntimeError(
            f"destination topic inaccessible dest={dest_chat}_{dest_topic}"
        )

    logger.warning(
        "[PARTNER LAST20 ACCESS OK] source=%s_%s dest=%s_%s",
        source_chat,
        source_topic,
        dest_chat,
        dest_topic,
    )


async def run_partner_3885074241_last20(main_module, logger=None):
    logger = logger or log
    client = getattr(main_module, "client", None)
    routes = getattr(main_module, "ROUTES", None)

    if client is None or not isinstance(routes, list):
        raise RuntimeError("partner last20 requires main worker client/routes")

    state_path = _state_path(main_module)
    state = _load_state(state_path)

    logger.warning(
        "[PARTNER LAST20 START] routes=%s count=%s "
        "destination_may_be_nonempty=True albums_one_post=True "
        "oldest_to_newest=True skip_already_mapped=True",
        len(TARGETS),
        LAST20_COUNT,
    )

    for contract in TARGETS:
        key = _key(contract)
        entry = state.get(key) if isinstance(state.get(key), dict) else {}

        if entry.get("status") == "done":
            logger.warning(
                "[PARTNER LAST20 STATE SKIP] source=%s_%s dest=%s_%s "
                "already_done=True selected=%s sent=%s existing=%s filtered=%s",
                contract[0],
                contract[1],
                contract[2],
                contract[3],
                entry.get("selected", 0),
                entry.get("sent", 0),
                entry.get("existing", 0),
                entry.get("filtered", 0),
            )
            continue

        matches = [route for route in routes if _route_tuple(route) == contract]
        if len(matches) != 1:
            raise RuntimeError(
                f"partner route contract invalid contract={contract} matches={len(matches)}"
            )
        route = matches[0]

        await _verify_topic_access(client, contract, logger)

        selected_units = (
            entry.get("selected_units")
            if entry.get("status") == "running"
            else None
        )

        if not selected_units:
            source_messages = await _source_messages(client, route, logger)
            units = _build_units(main_module, route, source_messages)[-LAST20_COUNT:]
            selected_units = [
                [
                    int(getattr(message, "id", 0) or 0)
                    for message in unit
                    if int(getattr(message, "id", 0) or 0)
                ]
                for unit in units
            ]
            selected_units = [ids for ids in selected_units if ids]

            if not selected_units:
                raise RuntimeError(
                    f"source returned no copyable messages contract={contract}"
                )

            state[key] = {
                "status": "running",
                "contract": list(contract),
                "selected_units": selected_units,
                "completed": [],
                "selected": len(selected_units),
                "sent": 0,
                "existing": 0,
                "filtered": 0,
                "started_at": time.time(),
            }
            _save_state(state_path, state)
            entry = state[key]

            logger.warning(
                "[PARTNER LAST20 SELECTED] source=%s_%s dest=%s_%s posts=%s",
                contract[0],
                contract[1],
                contract[2],
                contract[3],
                len(selected_units),
            )

        completed = set(str(value) for value in (entry.get("completed") or []))
        sent_count = int(entry.get("sent", 0) or 0)
        existing_count = int(entry.get("existing", 0) or 0)
        filtered_count = int(entry.get("filtered", 0) or 0)

        for index, ids in enumerate(selected_units, start=1):
            ids = [int(value) for value in ids]
            token = ",".join(str(value) for value in ids)
            if token in completed:
                continue

            unit = await _reload_unit(client, route, ids, logger)
            before_ids = _mapped_ids(main_module, route, unit)

            if _unit_already_mapped(main_module, route, unit):
                existing_count += 1
                outcome = "existing"
                logger.info(
                    "[PARTNER LAST20 ALREADY MAPPED] source=%s_%s dest=%s_%s "
                    "post=%s/%s source_ids=%s dest_ids=%s",
                    contract[0],
                    contract[1],
                    contract[2],
                    contract[3],
                    index,
                    len(selected_units),
                    ids,
                    before_ids,
                )
            else:
                copied = await _copy_unit(main_module, route, unit)
                if not copied:
                    raise RuntimeError(
                        f"partner last20 copy returned false contract={contract} ids={ids}"
                    )

                after_ids = _mapped_ids(main_module, route, unit)
                new_ids = [value for value in after_ids if value not in before_ids]

                if new_ids:
                    sent_count += 1
                    outcome = "sent"
                    logger.warning(
                        "[PARTNER LAST20 SENT] source=%s_%s dest=%s_%s "
                        "post=%s/%s source_ids=%s dest_ids=%s",
                        contract[0],
                        contract[1],
                        contract[2],
                        contract[3],
                        index,
                        len(selected_units),
                        ids,
                        new_ids,
                    )
                else:
                    filtered_count += 1
                    outcome = "filtered"
                    logger.warning(
                        "[PARTNER LAST20 FILTERED] source=%s_%s dest=%s_%s "
                        "post=%s/%s source_ids=%s policy_pipeline_preserved=True",
                        contract[0],
                        contract[1],
                        contract[2],
                        contract[3],
                        index,
                        len(selected_units),
                        ids,
                    )

            completed.add(token)
            state[key].update(
                {
                    "completed": sorted(completed),
                    "sent": sent_count,
                    "existing": existing_count,
                    "filtered": filtered_count,
                    "updated_at": time.time(),
                    "last_outcome": outcome,
                }
            )
            _save_state(state_path, state)
            await asyncio.sleep(0.35)

        state[key].update(
            {
                "status": "done",
                "completed_at": time.time(),
                "selected": len(selected_units),
                "sent": sent_count,
                "existing": existing_count,
                "filtered": filtered_count,
            }
        )
        _save_state(state_path, state)

        logger.warning(
            "[PARTNER LAST20 DONE] source=%s_%s dest=%s_%s "
            "selected=%s sent=%s existing=%s filtered=%s coverage=%s",
            contract[0],
            contract[1],
            contract[2],
            contract[3],
            len(selected_units),
            sent_count,
            existing_count,
            filtered_count,
            existing_count + sent_count,
        )

        await asyncio.sleep(0.75)

    logger.warning(
        "[PARTNER LAST20 COMPLETE] dest_chat=%s routes=%s",
        DEST_CHAT,
        len(TARGETS),
    )
    return state

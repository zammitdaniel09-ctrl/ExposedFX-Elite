"""One-time targeted last-50 backfill for ExposedFX hub topic 82617.

Target contract:
    -1002385852838 / topic 62159 -> -1003918958200 / topic 82617

This deliberately does NOT require the destination to be empty. It fills the
latest 50 logical source posts through the existing production copy pipeline,
while skipping source units that already have a durable destination mapping.
Existing filters, replies, albums, media handling and mapping remain in force.
Progress is persistent so Railway restarts cannot duplicate completed units.
"""

import json
import logging
import time
from pathlib import Path

from telegram_worker.requested_route_bootstrap import (
    _as_int,
    _build_units,
    _copy_unit,
    _reload_unit,
    _route_tuple,
    _source_messages,
    _unit_already_mapped,
)


log = logging.getLogger("force-82617-last50")

TARGET = (-1002385852838, 62159, -1003918958200, 82617)
STATE_FILENAME = "forced_82617_last50_v1.json"


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
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _mapped_ids(main_module, route, unit):
    fn = getattr(main_module, "existing_destination_ids", None)
    if not callable(fn):
        return []
    out = []
    for message in unit:
        try:
            for value in fn(message, route) or []:
                try:
                    iv = int(value)
                except Exception:
                    continue
                if iv not in out:
                    out.append(iv)
        except Exception:
            pass
    return out


async def run_force_82617_last50(main_module, logger=None):
    logger = logger or log
    client = getattr(main_module, "client", None)
    routes = getattr(main_module, "ROUTES", None)
    if client is None or not isinstance(routes, list):
        raise RuntimeError("82617 forced last50 requires main worker client/routes")

    state_path = _state_path(main_module)
    state = _load_state(state_path)
    if state.get("status") == "done":
        logger.warning(
            "[82617 LAST50 STATE SKIP] already_done=True selected=%s existing=%s sent=%s filtered=%s",
            state.get("selected", 0),
            state.get("existing", 0),
            state.get("sent", 0),
            state.get("filtered", 0),
        )
        return state

    matches = [route for route in routes if _route_tuple(route) == TARGET]
    if len(matches) != 1:
        raise RuntimeError(f"82617 route contract invalid matches={len(matches)} target={TARGET}")
    route = matches[0]

    selected_units = state.get("selected_units") if state.get("status") == "running" else None
    if not selected_units:
        source_messages = await _source_messages(client, route, logger)
        units = _build_units(main_module, route, source_messages)
        selected_units = [
            [int(getattr(message, "id", 0) or 0) for message in unit]
            for unit in units
        ]
        selected_units = [ids for ids in selected_units if ids and all(ids)]
        if not selected_units:
            raise RuntimeError("82617 source returned no copyable messages")

        state = {
            "status": "running",
            "target": list(TARGET),
            "selected_units": selected_units,
            "completed": [],
            "started_at": time.time(),
            "selected": len(selected_units),
            "existing": 0,
            "sent": 0,
            "filtered": 0,
        }
        _save_state(state_path, state)
        logger.warning(
            "[82617 LAST50 START] source=%s_%s dest=%s_%s selected_posts=%s destination_may_be_nonempty=True preserve_copy_pipeline=True",
            TARGET[0], TARGET[1], TARGET[2], TARGET[3], len(selected_units),
        )

    completed = set(str(value) for value in (state.get("completed") or []))
    existing_count = int(state.get("existing", 0) or 0)
    sent_count = int(state.get("sent", 0) or 0)
    filtered_count = int(state.get("filtered", 0) or 0)

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
                "[82617 LAST50 ALREADY MAPPED] post=%s/%s source_ids=%s dest_ids=%s",
                index, len(selected_units), ids, before_ids,
            )
        else:
            copied = await _copy_unit(main_module, route, unit)
            if not copied:
                raise RuntimeError(f"82617 copy returned false ids={ids}")

            after_ids = _mapped_ids(main_module, route, unit)
            new_ids = [value for value in after_ids if value not in before_ids]
            if new_ids:
                sent_count += 1
                outcome = "sent"
                logger.warning(
                    "[82617 LAST50 SENT] post=%s/%s source_ids=%s dest_ids=%s",
                    index, len(selected_units), ids, new_ids,
                )
            else:
                # Normal production copy functions return None for intentional
                # policy skips such as blocked senders / mention filters.
                filtered_count += 1
                outcome = "filtered"
                logger.warning(
                    "[82617 LAST50 FILTERED] post=%s/%s source_ids=%s policy_pipeline_preserved=True",
                    index, len(selected_units), ids,
                )

        completed.add(token)
        state.update({
            "completed": sorted(completed),
            "existing": existing_count,
            "sent": sent_count,
            "filtered": filtered_count,
            "updated_at": time.time(),
            "last_outcome": outcome,
        })
        _save_state(state_path, state)

    state.update({
        "status": "done",
        "completed_at": time.time(),
        "selected": len(selected_units),
        "existing": existing_count,
        "sent": sent_count,
        "filtered": filtered_count,
    })
    _save_state(state_path, state)
    logger.warning(
        "[82617 LAST50 DONE] source=%s_%s dest=%s_%s selected=%s already_present=%s newly_sent=%s filtered=%s coverage=%s",
        TARGET[0], TARGET[1], TARGET[2], TARGET[3],
        len(selected_units), existing_count, sent_count, filtered_count,
        existing_count + sent_count,
    )
    return state

"""One-time last-20 bootstrap for the new ALT-account topic routes.

Owner contract:
  -1004349952583 / 5246 -> -1004367822325 / 7248
  -1004349952583 / 5247 -> -1004367822325 / 7250

Live forwarding is owned by alt_worker_legacy through ALT_ROUTES_JSON. This
module only imports the latest 20 logical posts per route once, oldest ->
newest. Albums count as one logical post. The existing ALT copy_unit pipeline
remains authoritative for filters, replies, media, mapping and edits.
"""

import asyncio
import json
import time
from pathlib import Path

from telethon.errors import FloodWaitError


SOURCE_CHAT = -1004349952583
DEST_CHAT = -1004367822325
COUNT = 20
FETCH_LIMIT = 300

CONTRACTS = (
    (SOURCE_CHAT, 5246, DEST_CHAT, 7248),
    (SOURCE_CHAT, 5247, DEST_CHAT, 7250),
)

STATE_FILE = "alt_topics5246_5247_to_7248_7250_last20_v1.json"


def _state_path(legacy):
    return Path(legacy.DATA_DIR) / STATE_FILE


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
    tmp.write_text(
        json.dumps(value, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _route_tuple(route):
    return (
        int(route.get("source_chat", 0)),
        int(route["source_topic"])
        if route.get("source_topic") is not None
        else None,
        int(route.get("dest_chat", 0)),
        int(route.get("dest_topic", 0)),
    )


def _key(contract):
    return ":".join(
        "None" if value is None else str(value)
        for value in contract
    )


async def _call(call, log, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            log.warning(
                "[ALT NEWTOPICS LAST20 FLOODWAIT] "
                "label=%s attempt=%s/%s wait=%ss",
                label,
                attempt,
                attempts,
                wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(
        "ALT new-topics flood-wait retry budget exhausted: "
        + label
    )


def _copyable(legacy, message, source_topic):
    if message is None:
        return False
    if int(getattr(message, "id", 0) or 0) == int(source_topic):
        return False
    return bool(
        legacy.text_of(message)
        or legacy.real_media(message)
    )


async def run_alt_new_topics_last20_once(legacy, logger=None):
    log = logger or legacy.log
    path = _state_path(legacy)
    state = _load(path)

    while not legacy.client.is_connected():
        await asyncio.sleep(0.25)

    while not await legacy.client.is_user_authorized():
        await asyncio.sleep(0.5)

    await legacy.client.get_dialogs(limit=None)

    for contract in CONTRACTS:
        key = _key(contract)

        if (
            isinstance(state.get(key), dict)
            and state[key].get("status") == "done"
        ):
            log.warning(
                "[ALT NEWTOPICS LAST20 STATE SKIP] "
                "key=%s already_done=True",
                key,
            )
            continue

        matches = [
            route
            for route in legacy.ROUTES
            if _route_tuple(route) == contract
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "ALT new-topics last20 route invalid "
                f"key={key} matches={len(matches)}"
            )

        route = matches[0]
        source_topic = int(contract[1])

        source_root = await _call(
            lambda: legacy.client.get_messages(
                SOURCE_CHAT,
                ids=source_topic,
            ),
            log,
            f"source-root:{SOURCE_CHAT}_{source_topic}",
        )
        if not source_root:
            raise RuntimeError(
                f"ALT source topic missing: "
                f"{SOURCE_CHAT}_{source_topic}"
            )

        dest_root = await _call(
            lambda r=route: legacy.client.get_messages(
                r["dest_chat"],
                ids=int(r["dest_topic"]),
            ),
            log,
            f"dest-root:{contract[2]}_{contract[3]}",
        )
        if not dest_root:
            raise RuntimeError(
                f"ALT destination topic missing: "
                f"{contract[2]}_{contract[3]}"
            )

        raw = await _call(
            lambda r=route: legacy.fetch_route_messages(
                r,
                FETCH_LIMIT,
            ),
            log,
            f"source-history:{contract[0]}_{contract[1]}",
        )

        units = []
        for unit in legacy.build_units(list(raw or [])):
            filtered = [
                message
                for message in unit
                if _copyable(
                    legacy,
                    message,
                    source_topic,
                )
            ]
            if not filtered:
                continue
            filtered.sort(
                key=lambda message: int(message.id)
            )
            units.append(filtered)

        selected = units[-COUNT:]
        if not selected:
            raise RuntimeError(
                "ALT source returned no copyable posts: "
                f"{contract[0]}_{contract[1]}"
            )

        selected.sort(
            key=lambda unit: min(
                int(message.id)
                for message in unit
            )
        )

        state[key] = {
            "status": "running",
            "contract": list(contract),
            "selected_units": [
                [int(message.id) for message in unit]
                for unit in selected
            ],
            "started_at": time.time(),
            "sent": 0,
            "already_mapped": 0,
            "filtered": 0,
        }
        _save(path, state)

        log.warning(
            "[ALT NEWTOPICS LAST20 START] "
            "source=%s_%s dest=%s_%s selected=%s "
            "order=oldest_to_newest albums_one_post=True",
            contract[0],
            contract[1],
            contract[2],
            contract[3],
            len(selected),
        )

        sent = 0
        already = 0
        filtered_count = 0

        for index, unit in enumerate(
            selected,
            start=1,
        ):
            ids = [
                int(message.id)
                for message in unit
            ]

            if all(
                bool(
                    legacy.mapped_ids(
                        route,
                        message.id,
                    )
                )
                for message in unit
            ):
                already += 1
                log.info(
                    "[ALT NEWTOPICS LAST20 ALREADY MAPPED] "
                    "dest=%s_%s post=%s/%s ids=%s",
                    contract[2],
                    contract[3],
                    index,
                    len(selected),
                    ids,
                )
                continue

            ok = await legacy.copy_unit(
                route,
                unit,
                "new_topics_last20",
            )
            if not ok:
                raise RuntimeError(
                    f"ALT new-topics copy failed "
                    f"key={key} ids={ids}"
                )

            mapped_after = all(
                bool(
                    legacy.mapped_ids(
                        route,
                        message.id,
                    )
                )
                for message in unit
            )

            if mapped_after:
                sent += 1
                log.warning(
                    "[ALT NEWTOPICS LAST20 SENT] "
                    "source=%s_%s dest=%s_%s "
                    "post=%s/%s ids=%s",
                    contract[0],
                    contract[1],
                    contract[2],
                    contract[3],
                    index,
                    len(selected),
                    ids,
                )
            else:
                filtered_count += 1
                log.warning(
                    "[ALT NEWTOPICS LAST20 FILTERED] "
                    "source=%s_%s dest=%s_%s "
                    "post=%s/%s ids=%s",
                    contract[0],
                    contract[1],
                    contract[2],
                    contract[3],
                    index,
                    len(selected),
                    ids,
                )

            state[key].update(
                {
                    "sent": sent,
                    "already_mapped": already,
                    "filtered": filtered_count,
                    "updated_at": time.time(),
                }
            )
            _save(path, state)
            await asyncio.sleep(0.30)

        state[key] = {
            "status": "done",
            "contract": list(contract),
            "selected": len(selected),
            "sent": sent,
            "already_mapped": already,
            "filtered": filtered_count,
            "completed_at": time.time(),
        }
        _save(path, state)

        log.warning(
            "[ALT NEWTOPICS LAST20 DONE] "
            "source=%s_%s dest=%s_%s "
            "selected=%s sent=%s already_mapped=%s "
            "filtered=%s live_forwarding=True",
            contract[0],
            contract[1],
            contract[2],
            contract[3],
            len(selected),
            sent,
            already,
            filtered_count,
        )

    log.warning(
        "[ALT NEWTOPICS LAST20 ALL DONE] routes=%s",
        len(CONTRACTS),
    )
    return state

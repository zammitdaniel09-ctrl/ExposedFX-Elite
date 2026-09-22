"""One-time last-100 bootstrap for the split former pair106 providers.

Owner contract (ALT/new Telegram account):
  -1004347858259 (whole chat) -> -1004367822325 topic 2583
  -1003252087470 (whole chat) -> -1004367822325 topic 2585

Each route receives its own latest 100 logical posts. Albums count as one post.
The existing ALT copy_unit pipeline remains authoritative for filters, replies,
media, mappings, edits and duplicate prevention.
"""

import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

DEST_CHAT = -1004367822325
COUNT = 100
FETCH_LIMIT = 1200

CONTRACTS = (
    (-1004347858259, None, DEST_CHAT, 2583),
    (-1003252087470, None, DEST_CHAT, 2585),
)

STATE_FILE = "alt_split_pair2583_2585_last100_v1.json"


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
    return ":".join("None" if v is None else str(v) for v in contract)


async def _call(call, log, label, attempts=5):
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds)) + 1
            log.warning(
                "[ALT SPLIT LAST100 FLOODWAIT] label=%s attempt=%s/%s wait=%ss",
                label, attempt, attempts, wait_for,
            )
            await asyncio.sleep(wait_for)
    raise RuntimeError(f"flood-wait retry budget exhausted: {label}")


def _copyable(legacy, message):
    return bool(message and (legacy.text_of(message) or legacy.real_media(message)))


async def run_alt_split_pair_last100_once(legacy, logger=None):
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
        if isinstance(state.get(key), dict) and state[key].get("status") == "done":
            log.warning("[ALT SPLIT LAST100 STATE SKIP] key=%s already_done=True", key)
            continue

        matches = [route for route in legacy.ROUTES if _route_tuple(route) == contract]
        if len(matches) != 1:
            raise RuntimeError(f"ALT split last100 route invalid key={key} matches={len(matches)}")
        route = matches[0]

        # Destination root must exist before selecting/sending history.
        root = await _call(
            lambda r=route: legacy.client.get_messages(r["dest_chat"], ids=int(r["dest_topic"])),
            log,
            f"dest-root:{contract[2]}_{contract[3]}",
        )
        if not root:
            raise RuntimeError(f"ALT split destination missing: {contract[2]}_{contract[3]}")

        raw = await _call(
            lambda r=route: legacy.fetch_route_messages(r, FETCH_LIMIT),
            log,
            f"source-history:{contract[0]}",
        )
        units = [
            unit for unit in legacy.build_units(list(raw or []))
            if unit and any(_copyable(legacy, m) for m in unit)
        ]
        selected = units[-COUNT:]

        if not selected:
            raise RuntimeError(f"ALT split source returned no copyable posts: {contract[0]}")

        selected.sort(key=lambda unit: min(int(m.id) for m in unit))
        state[key] = {
            "status": "running",
            "selected": [[int(m.id) for m in unit] for unit in selected],
            "started_at": time.time(),
        }
        _save(path, state)

        log.warning(
            "[ALT SPLIT LAST100 START] source=%s dest=%s_%s selected=%s order=oldest_to_newest",
            contract[0], contract[2], contract[3], len(selected),
        )

        sent = 0
        already = 0
        filtered = 0

        for index, unit in enumerate(selected, start=1):
            ids = [int(m.id) for m in unit]

            if all(bool(legacy.mapped_ids(route, m.id)) for m in unit):
                already += 1
                log.info(
                    "[ALT SPLIT LAST100 ALREADY MAPPED] dest=%s_%s post=%s/%s ids=%s",
                    contract[2], contract[3], index, len(selected), ids,
                )
                continue

            ok = await legacy.copy_unit(route, unit, "split_pair_last100")
            if not ok:
                raise RuntimeError(f"ALT split copy failed key={key} ids={ids}")

            mapped_after = all(bool(legacy.mapped_ids(route, m.id)) for m in unit)
            if mapped_after:
                sent += 1
                log.warning(
                    "[ALT SPLIT LAST100 SENT] dest=%s_%s post=%s/%s ids=%s",
                    contract[2], contract[3], index, len(selected), ids,
                )
            else:
                filtered += 1
                log.warning(
                    "[ALT SPLIT LAST100 FILTERED] dest=%s_%s post=%s/%s ids=%s",
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

        log.warning(
            "[ALT SPLIT LAST100 DONE] source=%s dest=%s_%s selected=%s sent=%s already_mapped=%s filtered=%s live_forwarding=True",
            contract[0], contract[2], contract[3], len(selected), sent, already, filtered,
        )

    log.warning("[ALT SPLIT LAST100 ALL DONE] routes=%s", len(CONTRACTS))
    return state

"""Deployment-gap recovery for the FASTVIP2 routes.

FASTVIP2 only sees posts Telegram pushes while MAIN is running, and its
watchdog starts from the newest post after every start, so a post that
arrived while MAIN was restarting was never delivered (4 Nov 2026: relay 2
post 8522 was lost in a 41 s restart).

On start, before FASTVIP2 runs, each route is checked from its last
delivered post (the newest mapped source id) forwards. Missed posts up to
MAX_AGE_SECONDS old are delivered in order through the engine; older ones
are not sent, since a stale signal could mislead members, and are reported
("[GAP STALE NOT SENT]"), which the health monitor turns into a Saved
Messages notice.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Sequence

from .model import Route, Send
from .runtime import engine_route

log = logging.getLogger("imperium-worker")
MAX_AGE_SECONDS = 600
FETCH_LIMIT = 50


def last_delivered(message_map: Dict, route: Route) -> int:
    prefix, suffix = f"{route.source_chat}:", f":{route.dest_chat}:{route.dest_topic}"
    newest = 0
    for key in message_map:
        if key.startswith(prefix) and key.endswith(suffix):
            try:
                newest = max(newest, int(key.split(":")[1]))
            except (IndexError, ValueError):
                continue
    return newest


def units_of(messages: Sequence) -> List[List]:
    """Oldest first; an album is one unit."""
    ordered = sorted(messages, key=lambda m: int(m.id))
    units, used = [], set()
    for message in ordered:
        if message.id in used:
            continue
        grouped = getattr(message, "grouped_id", None)
        unit = [m for m in ordered if grouped and getattr(m, "grouped_id", None) == grouped] or [message]
        used.update(m.id for m in unit)
        units.append(unit)
    return units


async def missed_units(client, route: Route, after_id: int) -> List[List]:
    kwargs = {"limit": FETCH_LIMIT, "min_id": after_id}
    if route.source_topic is not None:
        kwargs["reply_to"] = route.source_topic
    messages = await client.get_messages(route.source_chat, **kwargs) or []
    fresh = [
        m
        for m in messages
        if m is not None
        and int(m.id) > after_id
        and int(m.id) != (route.source_topic or 0)
        and not getattr(m, "action", None)
    ]
    return units_of(fresh)


async def recover_gaps(worker, engine, legacy_routes: Sequence[Dict], clock=time.time) -> Dict[str, Dict]:
    report = {}
    for legacy_route in legacy_routes:
        route = Route.from_legacy(legacy_route)
        outcome = {"recovered": 0, "stale": 0, "skipped": 0}
        report[route.name] = outcome
        after_id = last_delivered(getattr(worker, "message_map", {}) or {}, route)
        if not after_id:
            continue  # never delivered: nothing to measure a gap from
        try:
            units = await missed_units(worker.client, route, after_id)
        except Exception as exc:
            log.warning("[GAP CHECK FAILED] route=%s %s: %s", route.name, type(exc).__name__, exc)
            continue
        for unit in units:
            ids = [int(m.id) for m in unit]
            engine.planner.context["emoji_registry"] = getattr(worker, "EMOJI_REGISTRY", None)
            if not isinstance(engine.planner.plan(engine_route(legacy_route), unit, legacy_route), Send):
                outcome["skipped"] += 1  # already copied, or the rules would skip it anyway
                continue
            age = clock() - unit[0].date.timestamp() if getattr(unit[0], "date", None) else None
            if age is None or age > MAX_AGE_SECONDS:
                outcome["stale"] += 1
                log.warning(
                    "[GAP STALE NOT SENT] route=%s ids=%s age=%ss max=%ss",
                    route.name,
                    ids,
                    int(age) if age is not None else "unknown",
                    MAX_AGE_SECONDS,
                )
                continue
            await engine.deliver(legacy_route, unit, live=True)
            if engine.planner.maps.all_mapped(route, unit):
                outcome["recovered"] += 1
                log.warning("[GAP RECOVERED] route=%s ids=%s age=%.0fs", route.name, ids, age)
            else:
                log.warning("[GAP NOT RECOVERED] route=%s ids=%s age=%.0fs", route.name, ids, age)
    log.warning(
        "[GAP CHECK DONE] routes=%s recovered=%s stale=%s",
        len(report),
        sum(r["recovered"] for r in report.values()),
        sum(r["stale"] for r in report.values()),
    )
    return report

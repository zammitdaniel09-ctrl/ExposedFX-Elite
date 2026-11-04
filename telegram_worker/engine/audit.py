"""Replay audit: check the engine against posts already delivered.

For the newest mapped posts on each live route, the source message and the
destination copy the legacy pipeline made are read back (two get_messages
calls per route), the engine plans the source post as if it were new, and the
plan is compared with the copy that is actually in the destination: text,
entities, media kind and reply target. Read-only; runs once per engine
version and logs one summary line per route.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .model import Plan, Route, Send, entity_keys
from .plan import Facts, Planner
from .rules import is_real_media

log = logging.getLogger("engine-audit")
STATE_FILE = "engine_replay_audit_v1.json"
ROUTE_PAUSE_SECONDS = 3.0  # 1 s still drew a 22 s GetMessages flood wait (4 Nov 2026)
AUDIT_VERSION = 2  # 2: first run with every MAIN route on the engine


def audit_key(version: int) -> str:
    """The audit runs once per deployment (and per audit version), so every deploy is checked."""
    return f"{version}:{os.environ.get('RAILWAY_DEPLOYMENT_ID', '')}"


PER_ROUTE = 25


def _media_kind(message: Any) -> Optional[str]:
    if not is_real_media(message):
        return None
    return "photo" if getattr(message.media, "photo", None) is not None else "document"


def _reply_to(message: Any) -> int:
    reply = getattr(message, "reply_to", None)
    return int(getattr(reply, "reply_to_msg_id", 0) or 0)


def media_changed_by_edit(fields: List[str], source: Any, dest: Any) -> bool:
    """Only the media differs, and the source was edited after it was copied.

    Edits are applied to copies in place, but an edit that adds, removes or
    swaps media never is (no delete, no resend), so such a post is expected
    to differ there. Every other difference on an edited post is still reported.
    """
    edited, sent = getattr(source, "edit_date", None), getattr(dest, "date", None)
    return fields == ["media"] and bool(edited and sent and edited > sent)


def compare(plan: Plan, source: Any, dest: Any) -> List[str]:
    """Fields where the engine's plan differs from the delivered copy."""
    if not isinstance(plan, Send):
        return [f"engine_skip:{plan.reason}"]
    fields = []
    if plan.text != (dest.message or ""):
        fields.append("text")
    if plan.entities != entity_keys(dest.entities if dest.message else None):
        fields.append("entities")
    if (plan.kind == "media") != (_media_kind(dest) is not None) or (
        plan.kind == "media" and _media_kind(source) != _media_kind(dest)
    ):
        fields.append("media")
    if plan.reply_to != _reply_to(dest):
        fields.append("reply_to")
    return fields


def newest_mapped(message_map: Mapping[str, Any], route: Route, limit: int) -> List[tuple]:
    prefix, suffix = f"{route.source_chat}:", f":{route.dest_chat}:{route.dest_topic}"
    pairs = []
    for key, value in message_map.items():
        if not (key.startswith(prefix) and key.endswith(suffix)):
            continue
        ids = value if isinstance(value, list) else [value]
        try:
            pairs.append((int(key.split(":")[1]), int(ids[0])))
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(pairs)[-limit:]


async def audit_route(
    client: Any, planner: Planner, legacy_route: Dict, formatter: str, limit: int = PER_ROUTE
) -> Dict:
    route = Route.from_legacy(legacy_route, formatter=formatter)
    pairs = newest_mapped(planner.maps.mapping, route, limit)
    if not pairs:
        return {"checked": 0}
    sources = await client.get_messages(route.source_chat, ids=[s for s, _ in pairs])
    dests = await client.get_messages(route.dest_chat, ids=[d for _, d in pairs])
    result: Counter = Counter()
    examples: List[Dict] = []
    for source, dest in zip(sources, dests):
        if source is None or dest is None or getattr(source, "grouped_id", None):
            result["unavailable_or_album"] += 1
            continue
        plan = planner.plan(route, [source], legacy_route, facts=Facts(all_mapped=False, any_mapped=False))
        fields = compare(plan, source, dest)
        if media_changed_by_edit(fields, source, dest):
            result["media_changed_by_edit"] += 1
            continue
        result["checked"] += 1
        if fields:
            result["mismatch"] += 1
            result.update(f"field:{f}" for f in fields)
            if len(examples) < 3:
                examples.append(
                    {
                        "source": source.id,
                        "dest": dest.id,
                        "fields": fields,
                        "engine": (plan.text[:50] if isinstance(plan, Send) else str(plan)),
                        "delivered": (dest.message or "")[:50],
                    }
                )
        else:
            result["match"] += 1
    return {**result, "examples": examples}


async def run_replay_audit(worker: Any, planner: Planner, delay_seconds: float = 90.0) -> Optional[Dict]:
    path = Path(worker.DATA_DIR) / STATE_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("version") == audit_key(AUDIT_VERSION):
            log.info("[ENGINE REPLAY AUDIT SKIPPED] version=%s already_done=True", audit_key(AUDIT_VERSION))
            return None
    except Exception:
        pass
    await asyncio.sleep(delay_seconds)  # let startup and live delivery settle first
    from .runtime import formatter_for

    planner.context["emoji_registry"] = getattr(worker, "EMOJI_REGISTRY", None)
    report = {}
    for legacy_route in list(worker.ROUTES):
        route = Route.from_legacy(legacy_route)
        try:
            outcome = await audit_route(worker.client, planner, legacy_route, formatter_for(route))
        except Exception as exc:
            outcome = {"error": f"{type(exc).__name__}: {exc}"}
        report[route.name] = outcome
        level = logging.WARNING if outcome.get("mismatch") or outcome.get("error") else logging.INFO
        log.log(
            level,
            "[ENGINE REPLAY AUDIT] route=%s checked=%s match=%s mismatch=%s detail=%s",
            route.name,
            outcome.get("checked", 0),
            outcome.get("match", 0),
            outcome.get("mismatch", 0),
            json.dumps(
                {k: v for k, v in outcome.items() if k not in ("checked", "match", "mismatch")},
                ensure_ascii=False,
            ),
        )
        await asyncio.sleep(ROUTE_PAUSE_SECONDS)  # stay under Telegram's read limit
    try:
        path.write_text(
            json.dumps({"version": audit_key(AUDIT_VERSION), "report": report}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    total = sum(r.get("checked", 0) for r in report.values())
    matched = sum(r.get("match", 0) for r in report.values())
    log.warning("[ENGINE REPLAY AUDIT DONE] routes=%s checked=%s match=%s", len(report), total, matched)
    return report

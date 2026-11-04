"""Shadow mode: prove the engine decides exactly what the legacy pipeline sends.

Nothing here sends, edits or deletes. Two legacy entry points are wrapped
transparently:

- worker.fast_vip_deliver_route(route, unit) - the FASTVIP2 routes;
- owner_requested_routes.copy_unit(worker, route, unit) - the owner routes.

While one of them runs, every send_message/send_file call it makes is recorded
(the arguments only; the call itself goes through untouched). When it returns,
the engine plans the same post on the same route and the two are compared:
skip vs send, destination, reply target, text, entities and media. Each post
logs [SHADOW MATCH] or [SHADOW MISMATCH] with the differing fields, and the
running totals are kept in engine_shadow_v1.json on the volume.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from telegram_worker import owner_forwarding_policy as policy

from .model import Route, Send, Skip, entity_keys, media_key
from .plan import Facts, Planner
from .replies import MessageMap
from .rules import Rules

log = logging.getLogger("engine-shadow")
STATE_FILE = "engine_shadow_v1.json"
IMPERIUM_ROUTE = (-1004367822325, 508, -1003726286301, 7)
_ACTIVE: contextvars.ContextVar[Optional["Capture"]] = contextvars.ContextVar("engine_shadow", default=None)


def formatter_for(route: Route) -> str:
    return "imperium_vip" if route.key == IMPERIUM_ROUTE else "copy"


@dataclass
class Capture:
    route: Route
    legacy_route: Dict
    unit: List
    facts: Facts
    calls: List[Send] = field(default_factory=list)


def _as_send(method: str, args, kwargs) -> Optional[Send]:
    """Normalise a legacy send_message/send_file call into a Send."""
    entity = args[0] if args else kwargs.get("entity")
    try:
        dest = int(entity)
    except (TypeError, ValueError):
        return None
    reply_to = kwargs.get("reply_to")
    reply_to = int(getattr(reply_to, "id", reply_to)) if reply_to is not None else 0
    if method == "send_message":
        text = args[1] if len(args) > 1 else kwargs.get("message", "")
        return Send("text", dest, reply_to, text or "", entity_keys(kwargs.get("formatting_entities")))
    file = args[1] if len(args) > 1 else kwargs.get("file")
    files = list(file) if isinstance(file, (list, tuple)) else [file]
    caption = kwargs.get("caption") or ""
    return Send("album" if isinstance(file, (list, tuple)) else "media", dest, reply_to, caption,
                entity_keys(kwargs.get("formatting_entities") if caption else None),
                tuple(media_key(f) for f in files))


def diff(engine: Send, legacy: Send) -> List[str]:
    return [name for name in ("kind", "dest_chat", "reply_to", "text", "entities", "media")
            if getattr(engine, name) != getattr(legacy, name)]


class Shadow:
    def __init__(self, worker, data_dir, clock=time.time):
        self.worker = worker
        self.path = Path(data_dir) / STATE_FILE
        self.clock = clock
        self.planner = Planner(
            rules=Rules.from_env(stopped=policy.is_stopped),
            maps=MessageMap(lambda: getattr(worker, "message_map", {}) or {}),
            permanently_failed=self._ledger,
        )
        self.totals = self._load()
        self.last_save = 0.0

    # -- state -------------------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _save(self, force=False) -> None:
        if not force and self.clock() - self.last_save < 30:
            return
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.totals, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            self.last_save = self.clock()
        except Exception as exc:
            log.warning("[SHADOW STATE NOT SAVED] %s", type(exc).__name__)

    def _count(self, route: Route, outcome: str) -> None:
        stats = self.totals.setdefault(route.name, {})
        stats[outcome] = int(stats.get(outcome, 0)) + 1
        self._save()

    def _ledger(self, legacy_route, ids) -> bool:
        ledger = getattr(self.worker, "FASTVIP_PERMANENT_FAILURES", None)
        return bool(ledger and ledger.contains_all(legacy_route, ids))

    # -- capture -----------------------------------------------------------
    def begin(self, legacy_route: Dict, unit, check_ledger: bool) -> Optional[Capture]:
        try:
            route = Route.from_legacy(legacy_route)
            route = Route(**{**route.__dict__, "formatter": formatter_for(route)})
            unit = sorted(unit, key=lambda m: int(m.id))
            return Capture(route, legacy_route, unit,
                           self.planner.facts(route, unit, legacy_route, check_ledger))
        except Exception as exc:
            log.warning("[SHADOW CAPTURE SKIPPED] %s: %s", type(exc).__name__, exc)
            return None

    def record(self, method: str, args, kwargs) -> None:
        capture = _ACTIVE.get()
        if capture is None:
            return
        try:
            send = _as_send(method, args, kwargs)
            if send is not None:
                capture.calls.append(send)
        except Exception as exc:
            log.warning("[SHADOW RECORD FAILED] %s: %s", type(exc).__name__, exc)

    def judge(self, capture: Capture, legacy_result: Any) -> str:
        try:
            self.planner.context["emoji_registry"] = getattr(self.worker, "EMOJI_REGISTRY", None)
            plan = self.planner.plan(capture.route, capture.unit, capture.legacy_route, facts=capture.facts)
            ids = [int(m.id) for m in capture.unit]
            # A delivery can make more than one send (a recovered reply parent
            # first, a retry, a re-upload): the post is the call that matches.
            legacy = None
            if capture.calls:
                legacy = capture.calls[-1]
                if isinstance(plan, Send):
                    legacy = next((c for c in capture.calls if not diff(plan, c)), legacy)
            if isinstance(plan, Skip) and legacy is None:
                outcome, detail = "match_skip", plan.reason
            elif isinstance(plan, Send) and legacy is not None:
                fields = diff(plan, legacy)
                outcome, detail = ("match_send", plan.kind) if not fields else ("mismatch", ",".join(fields))
            elif legacy is None and not _legacy_completed(legacy_result):
                outcome, detail = "legacy_failed", f"engine_would_send:{plan.kind}"
            elif legacy is None:
                outcome, detail = "mismatch", f"legacy_skipped engine_would_send:{plan.kind}"
            else:
                outcome, detail = "mismatch", f"engine_would_skip:{plan.reason}"
            self._count(capture.route, outcome)
            if outcome == "mismatch":
                log.warning("[SHADOW MISMATCH] route=%s ids=%s detail=%s engine=%s legacy=%s",
                            capture.route.name, ids, detail, _brief(plan), _brief(legacy))
            else:
                log.info("[SHADOW %s] route=%s ids=%s detail=%s", outcome.upper(), capture.route.name, ids, detail)
            return outcome
        except Exception as exc:
            log.warning("[SHADOW JUDGE FAILED] route=%s %s: %s", capture.route.name, type(exc).__name__, exc)
            return "error"

    def summary(self) -> str:
        parts = []
        for name, stats in sorted(self.totals.items()):
            seen = sum(stats.values())
            parts.append(f"{name}: {stats.get('match_send', 0) + stats.get('match_skip', 0)}/{seen}"
                         + (f" ({stats['mismatch']} mismatch)" if stats.get("mismatch") else ""))
        return "; ".join(parts) or "no posts yet"

    # -- wrappers ----------------------------------------------------------
    async def run_tracked(self, legacy_route, unit, check_ledger, call):
        capture = self.begin(legacy_route, unit, check_ledger)
        token = _ACTIVE.set(capture)
        result = None
        try:
            result = await call()
            return result
        finally:
            _ACTIVE.reset(token)
            if capture is not None:
                asyncio.get_running_loop().call_soon(self.judge, capture, result)


def _legacy_completed(result) -> bool:
    """FASTVIP returns True when a post is handled; owner routes return (status, ids)."""
    if result is True:
        return True
    return isinstance(result, tuple) and bool(result) and result[0] in ("sent", "existing", "filtered")


def _brief(value) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, Skip):
        return f"skip:{value.reason}"
    text = value.text.replace("\n", "\\n")
    return (f"{value.kind} dest={value.dest_chat} reply_to={value.reply_to} "
            f"text={text[:60]!r} entities={len(value.entities)} media={len(value.media)}")


def engine_routes_from_env() -> frozenset:
    """Route names the engine delivers (ENGINE_ROUTES, comma separated)."""
    raw = os.environ.get("ENGINE_ROUTES", "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def install_shadow(worker, owner_routes_module=None, data_dir=None, engine_routes=None) -> Shadow:
    """Wrap the legacy delivery entry points and the client's send methods.

    Routes named in engine_routes are delivered by the engine (deliver.py);
    every other route stays on the legacy pipeline under shadow comparison.
    """
    from .deliver import Deliverer

    shadow = Shadow(worker, data_dir or worker.DATA_DIR)
    engine_routes = engine_routes_from_env() if engine_routes is None else frozenset(engine_routes)
    shadow.deliverer = Deliverer(worker, shadow.planner,
                                 attempts=int(getattr(worker, "COPY_RETRY_ATTEMPTS", 3) or 3),
                                 sleep_cap=int(getattr(worker, "COPY_RETRY_SLEEP_CAP_SECONDS", 120) or 120))
    client = worker.client

    def engine_route(route):
        parsed = Route.from_legacy(route)
        return Route(**{**parsed.__dict__, "formatter": formatter_for(parsed)})

    for method in ("send_message", "send_file"):
        original = getattr(client, method)

        def recorder(*args, __original=original, __method=method, **kwargs):
            shadow.record(__method, args, kwargs)
            return __original(*args, **kwargs)

        setattr(client, method, recorder)

    original_deliver = worker.fast_vip_deliver_route

    async def fast_vip_deliver_route(route, unit, skip_permanent_errors=False):
        if route.get("name") in engine_routes:
            return await shadow.deliverer.deliver(route, engine_route(route), unit, live=skip_permanent_errors)
        return await shadow.run_tracked(
            route, unit, skip_permanent_errors,
            lambda: original_deliver(route, unit, skip_permanent_errors=skip_permanent_errors))

    worker.fast_vip_deliver_route = fast_vip_deliver_route

    if owner_routes_module is not None:
        original_copy_unit = owner_routes_module.copy_unit

        async def copy_unit(owner_worker, route, unit):
            if route.get("name") in engine_routes:
                return await shadow.deliverer.copy_unit(owner_routes_module, route, engine_route(route), unit)
            return await shadow.run_tracked(
                route, unit, False, lambda: original_copy_unit(owner_worker, route, unit))

        owner_routes_module.copy_unit = copy_unit

    log.warning("[ENGINE SHADOW READY] fastvip=True owner_routes=%s engine_routes=%s state=%s",
                owner_routes_module is not None, sorted(engine_routes), STATE_FILE)
    return shadow

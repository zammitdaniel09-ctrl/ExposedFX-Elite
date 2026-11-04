"""The ALT worker on the delivery engine: provider groups -> VIP RELAY GROUP.

ALT shares the engine's planner, rendering, sending, verification and message
map writes with MAIN and keeps what is ALT's own:

- no sender block or forward-loop guard (only the mention/link filter);
- the T4T route is translated by Telegram (translated_payload, fail-closed:
  nothing untranslated is sent) before rendering;
- reply targets come from ALT reply recovery (alt_reply_hardening);
- every handled post still advances the old per-route checkpoint
  (route_state.json), so a rollback to older code resumes where this left off;
- one attempt per call: the intake's watchdog (engine/intake.py) retries a
  failed post 30 s later; log markers stay "[ALT COPIED]" / "[ALT COPY FAILED]"
  / "[ALT FLOODWAIT]".
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from telethon.errors import FloodWaitError

from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.alt_reply_hardening import real_reply_source_ids, resolve_reply_target

from .deliver import Deliverer
from .model import Plan, Route, Send, Skip
from .plan import Facts, Planner
from .replies import MessageMap
from .rules import Rules, text_of

log = logging.getLogger("imperium-alt-worker")


class AltWorker:
    """What the engine needs from a worker, backed by alt_worker_legacy."""

    EMOJI_REGISTRY = None

    def __init__(self, legacy: Any) -> None:
        self.legacy = legacy

    @property
    def client(self) -> Any:
        return self.legacy.client

    @property
    def DATA_DIR(self) -> Any:  # noqa: N802 (matches the worker attribute)
        return self.legacy.DATA_DIR

    @property
    def message_map(self) -> Dict[str, Any]:
        return self.legacy.message_map

    def remember_message(self, source: Any, destination: Any, route: Dict, save: bool = True) -> None:
        self.legacy.remember(route, source, destination)  # writes the map and advances the checkpoint

    def save_map(self) -> None:
        pass  # remember() already saved


def alt_route(legacy: Any, legacy_route: Dict) -> Route:
    parsed = Route.from_legacy(legacy_route)
    formatter = "translate" if legacy.alt_arabic115_route(legacy_route) else "copy"
    # ALT has no forward-loop guard, whatever the route JSON carries.
    return Route(**{**parsed.__dict__, "formatter": formatter, "loop_guard_peers": frozenset()})


class AltEngine:
    """ALT's delivery: the shared planner and Deliverer, with ALT's own contract."""

    def __init__(self, legacy: Any, clock: Callable[[], float] = time.time) -> None:
        self.legacy = legacy
        self.clock = clock
        self.planner = Planner(
            rules=Rules(
                blocked_senders=frozenset(),
                filter_mentions=bool(legacy.FILTER_USERNAME_MENTIONS),
                filter_links=bool(legacy.FILTER_LINKS),
                stopped=policy.is_stopped,
            ),
            maps=MessageMap(lambda: legacy.message_map),
        )
        # One attempt per call: ALT's watchdog retries a failed post 30 s later.
        self.deliverer = Deliverer(AltWorker(legacy), self.planner, attempts=1, clock=clock)

    async def translations(self, legacy_route: Dict, unit: Sequence) -> Dict[int, Tuple[str, Any]]:
        """Translate exactly what legacy translated: the single post, or an album's caption."""
        targets = [m for m in unit if text_of(m)][:1]
        out: Dict[int, Tuple[str, Any]] = {}
        for message in targets:
            out[message.id] = await self.legacy.translated_payload(legacy_route, message)
        return out

    async def plan_unit(
        self, legacy_route: Dict, unit: Sequence, facts: Optional[Facts] = None
    ) -> Tuple[Route, Plan]:
        route = alt_route(self.legacy, legacy_route)
        plan = self.planner.plan(
            Route(**{**route.__dict__, "formatter": "copy"}), unit, legacy_route, facts=facts
        )
        if isinstance(plan, Skip) or route.formatter == "copy":
            return route, plan
        self.planner.context["translations"] = await self.translations(legacy_route, unit)
        try:
            return route, self.planner.plan(route, unit, legacy_route, facts=facts)
        finally:
            self.planner.context.pop("translations", None)

    async def deliver(self, legacy_route: Dict, unit: Sequence, reason: str) -> bool:
        """ALT contract: True when handled (sent or rightly skipped), False to let the watchdog retry."""
        unit = sorted(unit, key=lambda m: int(m.id))
        ids = [int(m.id) for m in unit]
        name = legacy_route.get("name")
        route, plan = await self.plan_unit(legacy_route, unit)
        if isinstance(plan, Skip):
            return self._skipped(legacy_route, unit, plan.reason)
        probe = next((m for m in unit if real_reply_source_ids(legacy_route, m)), unit[0])
        target = await resolve_reply_target(self.legacy, legacy_route, probe, logger=log)
        plan = Send(**{**plan.__dict__, "reply_to": int(target)})
        try:
            sent = await self.deliverer.send(legacy_route, route, unit, plan)
        except FloodWaitError as exc:
            log.warning("[ALT FLOODWAIT] route=%s seconds=%s engine=True", name, int(exc.seconds))
            return False
        except Exception as exc:
            log.exception(
                "[ALT COPY FAILED] route=%s ids=%s %s: %s engine=True", name, ids, type(exc).__name__, exc
            )
            return False
        lag = max(0.0, self.clock() - unit[0].date.timestamp()) if getattr(unit[0], "date", None) else 0.0
        log.warning(
            "[ALT COPIED] route=%s ids=%s kind=%s reason=%s engine=True lag=%.1fs",
            name,
            ids,
            "ALBUM" if plan.kind == "album" else "SINGLE",
            reason,
            lag,
        )
        if plan.reply_to != route.dest_topic:
            log.warning(
                "[ALT REPLY PRESERVED] route=%s source_child=%s dest_parent=%s", name, probe.id, plan.reply_to
            )
        return bool(sent)

    def _skipped(self, legacy_route: Dict, unit: Sequence, reason: str) -> bool:
        ids = [int(m.id) for m in unit]
        if reason == "partial_album_mapping":
            log.error("[ALT PARTIAL ALBUM MAP - FAIL CLOSED] ids=%s engine=True", ids)
            return False
        if reason == "mention_or_link":
            self.legacy.log_username_filter_alt(list(unit), "copy_unit", route=legacy_route)
        elif reason == "empty_unsupported":
            log.info("[ALT EMPTY MESSAGE SKIPPED] source_msg=%s engine=True", ids[0])
        elif reason != "already_copied":
            log.info("[ALT ENGINE SKIP] route=%s ids=%s reason=%s", legacy_route.get("name"), ids, reason)
        self.legacy.update_checkpoint(legacy_route, max(ids))
        return True


AUDIT_STATE_FILE = "engine_alt_replay_audit_v1.json"
AUDIT_VERSION = 1


async def run_alt_replay_audit(
    legacy: Any, engine: AltEngine, delay_seconds: float = 60.0, per_route: int = 25
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Read-only: re-plan recent ALT posts and compare them with the relay copies.

    Two get_messages calls per route; the translated route re-translates at
    most 10 posts (one Telegram translation call each).
    """
    import asyncio
    import json
    from pathlib import Path

    from .audit import audit_key, compare, media_changed_by_edit, newest_mapped, unavailable_reason
    from .plan import Facts

    path = Path(legacy.DATA_DIR) / AUDIT_STATE_FILE
    try:
        if json.loads(path.read_text(encoding="utf-8")).get("version") == audit_key(AUDIT_VERSION):
            return None
    except Exception:
        pass
    await asyncio.sleep(delay_seconds)
    report = {}
    for legacy_route in list(legacy.ROUTES):
        route = alt_route(legacy, legacy_route)
        outcome: Dict[str, Any] = {"checked": 0, "match": 0, "mismatch": 0, "examples": []}
        report[route.name] = outcome
        try:
            limit = 10 if route.formatter == "translate" else per_route
            pairs = newest_mapped(legacy.message_map, route, limit)
            if pairs:
                sources = await legacy.client.get_messages(route.source_chat, ids=[s for s, _ in pairs])
                dests = await legacy.client.get_messages(route.dest_chat, ids=[d for _, d in pairs])
                for source, dest in zip(sources, dests):
                    reason = unavailable_reason(source, dest)
                    if reason:
                        outcome[reason] = outcome.get(reason, 0) + 1
                        continue
                    _, plan = await engine.plan_unit(legacy_route, [source], facts=Facts(False, False))
                    fields = compare(plan, source, dest)
                    if media_changed_by_edit(fields, source, dest):
                        outcome["media_changed_by_edit"] = outcome.get("media_changed_by_edit", 0) + 1
                        continue
                    outcome["checked"] += 1
                    outcome["mismatch" if fields else "match"] += 1
                    if fields and len(outcome["examples"]) < 3:
                        outcome["examples"].append({"source": source.id, "dest": dest.id, "fields": fields})
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        level = logging.WARNING if outcome["mismatch"] or outcome.get("error") else logging.INFO
        log.log(
            level,
            "[ALT ENGINE REPLAY AUDIT] route=%s checked=%s match=%s mismatch=%s detail=%s",
            route.name,
            outcome["checked"],
            outcome["match"],
            outcome["mismatch"],
            json.dumps(
                {k: v for k, v in outcome.items() if k not in ("checked", "match", "mismatch")},
                ensure_ascii=False,
            ),
        )
        await asyncio.sleep(1.0)
    try:
        path.write_text(
            json.dumps({"version": audit_key(AUDIT_VERSION), "report": report}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    log.warning(
        "[ALT ENGINE REPLAY AUDIT DONE] routes=%s checked=%s match=%s",
        len(report),
        sum(r["checked"] for r in report.values()),
        sum(r["match"] for r in report.values()),
    )
    return report


def install_alt_engine(legacy: Any) -> AltEngine:
    engine = AltEngine(legacy)
    legacy.DELIVERY_ENGINE = engine
    log.warning(
        "[ALT DELIVERY ENGINE READY] routes=%s formatters=%s",
        len(legacy.ROUTES),
        sorted({alt_route(legacy, r).formatter for r in legacy.ROUTES}),
    )
    return engine

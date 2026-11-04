"""The MAIN worker's delivery engine: one planner, one deliverer, both entry points.

FASTVIP2 (worker_fixed.fast_vip_deliver_route) and the owner routes
(owner_requested_routes.copy_unit) both deliver through DeliveryEngine, so
every live MAIN route shares the same rules, rendering, reply resolution,
retries and message map.
"""

from __future__ import annotations

import logging
from typing import Dict, Sequence

from telegram_worker import owner_forwarding_policy as policy

from .deliver import Deliverer
from .model import Route
from .plan import Planner
from .render import FORMATTERS as FORMATTERS_BY_NAME
from .replies import MessageMap
from .rules import Rules

log = logging.getLogger("imperium-worker")

# Routes whose posts are restyled instead of copied as written.
FORMATTERS = {(-1004367822325, 508, -1003726286301, 7): "imperium_vip"}


def formatter_for(route: Route) -> str:
    return FORMATTERS.get(route.key, "copy")


def engine_route(legacy_route: Dict) -> Route:
    parsed = Route.from_legacy(legacy_route)
    return Route(**{**parsed.__dict__, "formatter": formatter_for(parsed)})


class DeliveryEngine:
    def __init__(self, worker):
        self.worker = worker
        self.planner = Planner(
            rules=Rules.from_env(stopped=policy.is_stopped),
            maps=MessageMap(lambda: getattr(worker, "message_map", {}) or {}),
            permanently_failed=self._permanently_failed,
        )
        self.deliverer = Deliverer(
            worker,
            self.planner,
            attempts=int(getattr(worker, "COPY_RETRY_ATTEMPTS", 3) or 3),
            sleep_cap=int(getattr(worker, "COPY_RETRY_SLEEP_CAP_SECONDS", 120) or 120),
        )

    def _permanently_failed(self, legacy_route, ids) -> bool:
        ledger = getattr(self.worker, "FASTVIP_PERMANENT_FAILURES", None)
        return bool(ledger and ledger.contains_all(legacy_route, ids))

    async def deliver(self, legacy_route: Dict, unit: Sequence, live: bool = True) -> bool:
        """FASTVIP2: True when handled (sent or rightly skipped), False to retry."""
        return await self.deliverer.deliver(legacy_route, engine_route(legacy_route), unit, live=live)

    async def copy_unit(self, owner_routes, legacy_route: Dict, unit: Sequence):
        """Owner routes: ("existing" | "sent" | "filtered", destination ids)."""
        return await self.deliverer.copy_unit(owner_routes, legacy_route, engine_route(legacy_route), unit)


def install_engine(worker) -> DeliveryEngine:
    engine = DeliveryEngine(worker)
    worker.DELIVERY_ENGINE = engine
    log.warning(
        "[DELIVERY ENGINE READY] routes=%s formatters=%s",
        len(getattr(worker, "ROUTES", []) or []),
        sorted({formatter_for(Route.from_legacy(r)) for r in getattr(worker, "ROUTES", []) or []}),
    )
    return engine


def restyle_for_route(message, legacy_route: Dict, registry):
    """The message as this route publishes it: restyled for VIP 7, unchanged elsewhere.

    Used for edits, so an edited VIP7 signal or update keeps the house style.
    """
    formatter = formatter_for(Route.from_legacy(legacy_route))
    if formatter == "copy":
        return message
    restyled = FORMATTERS_BY_NAME[formatter](message, {"emoji_registry": registry})
    if restyled is not message:
        log.warning("[VIP7 EDIT RESTYLED] route=%s source_msg=%s", legacy_route.get("name"), message.id)
    return restyled

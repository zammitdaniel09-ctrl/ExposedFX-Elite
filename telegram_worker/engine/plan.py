"""Decide what one source post becomes on one route: a Send or a Skip."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence

from .model import Plan, Route, Send, Skip, entity_keys, media_key
from .render import RECHECKED, render
from .replies import MessageMap, reply_target
from .rules import Rules


@dataclass(frozen=True)
class Facts:
    """Delivery state read before anything is sent for this post."""

    all_mapped: bool
    any_mapped: bool
    permanently_failed: bool = False


@dataclass
class Planner:
    rules: Rules
    maps: MessageMap
    # (route_dict, ids) -> True when Telegram already refused these posts for good.
    permanently_failed: Optional[Callable[[Dict, Sequence[int]], bool]] = None
    context: Dict[str, Any] = field(default_factory=dict)

    def facts(
        self, route: Route, unit: Sequence, legacy_route: Optional[Dict] = None, check_ledger: bool = False
    ) -> Facts:
        ids = [int(m.id) for m in unit]
        return Facts(
            all_mapped=self.maps.all_mapped(route, unit),
            any_mapped=self.maps.any_mapped(route, unit),
            permanently_failed=bool(
                check_ledger and self.permanently_failed and self.permanently_failed(legacy_route or {}, ids)
            ),
        )

    def plan(
        self,
        route: Route,
        unit: Sequence,
        legacy_route: Optional[Dict] = None,
        facts: Optional[Facts] = None,
        check_ledger: bool = False,
    ) -> Plan:
        unit = sorted(unit, key=lambda m: int(m.id))
        facts = facts or self.facts(route, unit, legacy_route, check_ledger)
        if facts.all_mapped:
            return Skip("already_copied")
        if facts.permanently_failed:
            return Skip("permanent_failure_recalled")
        if getattr(unit[0], "grouped_id", None) and len(unit) > 1 and facts.any_mapped:
            return Skip("partial_album_mapping")
        reason = self.rules.skip_reason(unit, route, legacy_route)
        if reason:
            return Skip(reason)
        rendered = render(unit, route.formatter, self.context)
        if route.formatter in RECHECKED and any(self.rules.blocked_content(m) for m in rendered.messages):
            return Skip("mention_or_link")
        if rendered.kind == "album" and not rendered.media:
            return Skip("empty_album")
        return Send(
            kind=rendered.kind,
            dest_chat=route.dest_chat,
            reply_to=reply_target(unit[0], route, self.maps),
            text=rendered.text,
            entities=entity_keys(rendered.entities if rendered.text else ()),
            media=tuple(media_key(m) for m in rendered.media),
            payload=rendered,
        )

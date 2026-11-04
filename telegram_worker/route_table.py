"""Every live forwarding route of both workers, in one typed table.

worker  the Telegram account that sends: MAIN or ALT
path    how new posts reach the route:
          intake     engine/intake.py, live VIP routes            (MAIN)
          owner      owner_routes.py contracts                    (MAIN)
          formatted  owner_formatted_route.py, library format     (MAIN)
          wholechat  samgtrades_relay.py, whole chat, house style (MAIN)
          relay      engine/intake.py, providers -> relay group   (ALT)

Retired and paused routes are not listed; their old definitions are in
archive/routes_before_table_20261104.py. validate() runs on import and refuses
a table that writes into a retired topic, so a mistake fails at start-up, not
in a destination.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

RELAY = -1004367822325  # VIP RELAY GROUP
VIP = -1003726286301  # IMPERIUM FX VIP
MARKET_MAKERS = -1003786932333
WORKERS = {"MAIN": ("intake", "owner", "formatted", "wholechat"), "ALT": ("relay",)}

# Never written to (or read from, for 2530) by any route.
RETIRED_DESTINATIONS = {(VIP, 5), (RELAY, 2530), (-1003885074241, 4663), (-1003885074241, 4665)}
RETIRED_SOURCES = {(RELAY, 2530)}
RELAY_7250_ONLY_DESTINATION = (VIP, 12161)


@dataclass(frozen=True)
class RouteSpec:
    name: str
    worker: str
    path: str
    source_chat: int
    source_topic: Optional[int]
    dest_chat: int
    dest_topic: Optional[int]
    options: Tuple[Tuple[str, Any], ...] = ()

    @property
    def identity(self) -> Tuple[int, Optional[int], int, Optional[int]]:
        return (self.source_chat, self.source_topic, self.dest_chat, self.dest_topic)

    def as_dict(self) -> Dict[str, Any]:
        """The route dict the workers have always used."""
        route: Dict[str, Any] = {
            "name": self.name,
            "source_chat": self.source_chat,
            "source_topic": self.source_topic,
            "dest_chat": self.dest_chat,
            "dest_topic": self.dest_topic,
        }
        route.update((key, list(value) if isinstance(value, tuple) else value) for key, value in self.options)
        return route


def _intake(
    name: str, source_chat: int, source_topic: Optional[int], dest_topic: int, loop_guard: Tuple[int, ...]
) -> RouteSpec:
    options: List[Tuple[str, Any]] = [
        ("verify_title", False),
        ("anonymous_send_as", False),
        ("live_only", True),
        ("copy_reply_parent", False),
    ]
    if source_topic is not None:
        options.append(("strict_source_topic", True))
    options += [("ordered_private", True), ("loop_guard_peers", loop_guard)]
    return RouteSpec(name, "MAIN", "intake", source_chat, source_topic, VIP, dest_topic, tuple(options))


def _owner(name: str, source_chat: int, source_topic: int, dest_chat: int, dest_topic: int) -> RouteSpec:
    options = (
        ("live_only", True),
        ("strict_source_topic", True),
        ("copy_reply_parent", False),
        ("anonymous_send_as", False),
        ("owner_managed", True),
    )
    return RouteSpec(name, "MAIN", "owner", source_chat, source_topic, dest_chat, dest_topic, options)


def _relay(name: str, source_chat: int, source_topic: Optional[int], relay_topic: int) -> RouteSpec:
    return RouteSpec(name, "ALT", "relay", source_chat, source_topic, RELAY, relay_topic)


TABLE: Tuple[RouteSpec, ...] = (
    # ALT: providers -> VIP RELAY GROUP topics
    _relay("ALT RELAY 1", -1003364661276, None, 2),  # Private Circle G Trades
    _relay("ARABIC VIP EN -> RELAY115", -1002211486103, None, 115),  # T4T AE Scalping VIP, translated
    _relay("NewAccount Topic5246 To Relay7248", -1004349952583, 5246, 7248),  # Blackline B+SCALPING
    _relay("NewAccount Topic5247 To Relay7250", -1004349952583, 5247, 7250),  # Blackline B+XAU
    _relay("NewAccount Select7194 To Relay8323", -1004410975295, 7194, 8323),  # SELECT SIGNALS
    # MAIN: relay topics and Market Slayers -> VIP
    _intake("Private Live 3838973021 To 9", -1003838973021, None, 9, (VIP,)),
    _intake("VIP RELAY Topic106 To VIP11", RELAY, 106, 11, (RELAY, VIP)),
    _intake("VIP RELAY Topic2 To VIP1364", RELAY, 2, 1364, (RELAY, VIP)),
    _intake("Relay508 To VIP7", RELAY, 508, 7, (RELAY, VIP)),  # restyled in the Imperium VIP format
    _intake("Relay7248 To VIP10877", RELAY, 7248, 10877, (RELAY, VIP)),
    # MAIN: owner contracts (relay 7250 and VIP partner topics -> Market Makers)
    _owner("Owner Relay7250 To VIP12161", RELAY, 7250, VIP, 12161),
    _owner("Owner VIP1364 To Partner28", VIP, 1364, MARKET_MAKERS, 28),
    _owner("Owner VIP10877 To Partner8", VIP, 10877, MARKET_MAKERS, 8),
    _owner("Owner VIP12161 To Partner24", VIP, 12161, MARKET_MAKERS, 24),
    # MAIN: formatted and whole-chat relays
    RouteSpec("Owner Relay8323 To VIP12289 Formatted", "MAIN", "formatted", RELAY, 8323, VIP, 12289),
    RouteSpec("SamGtrades To Exposed VIP", "MAIN", "wholechat", -1004281603170, None, -1003743381585, None),
)


def validate(table: Tuple[RouteSpec, ...] = TABLE) -> Tuple[RouteSpec, ...]:
    names, identities = set(), set()
    for spec in table:
        source, dest = (spec.source_chat, spec.source_topic), (spec.dest_chat, spec.dest_topic)
        problems = [
            (
                f"unknown worker/path {spec.worker}/{spec.path}"
                if spec.path not in WORKERS.get(spec.worker, ())
                else ""
            ),
            "duplicate name" if spec.name in names else "",
            "duplicate source/destination" if spec.identity in identities else "",
            "retired destination" if dest in RETIRED_DESTINATIONS else "",
            "retired source" if source in RETIRED_SOURCES else "",
            (
                "relay 7250 may only feed VIP12161"
                if source == (RELAY, 7250) and dest != RELAY_7250_ONLY_DESTINATION
                else ""
            ),
            (
                "ALT writes only into the relay group"
                if spec.worker == "ALT" and spec.dest_chat != RELAY
                else ""
            ),
        ]
        problems = [p for p in problems if p]
        if problems:
            raise ValueError(f"route table: {spec.name}: {', '.join(problems)}")
        names.add(spec.name)
        identities.add(spec.identity)
    return table


def routes(worker: str, *paths: str) -> List[Dict[str, Any]]:
    """Route dicts for one worker, in table order (all of its paths when none are given)."""
    return [spec.as_dict() for spec in TABLE if spec.worker == worker and (not paths or spec.path in paths)]


def one(name: str) -> RouteSpec:
    matches = [spec for spec in TABLE if spec.name == name]
    if len(matches) != 1:
        raise KeyError(f"route table has {len(matches)} routes named {name!r}")
    return matches[0]


validate()

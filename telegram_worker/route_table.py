"""Every live forwarding route of both workers, in one typed table.

ADD A ROUTE: add one line to TABLE and push to main; both workers pick it up
on their next start. REMOVE A ROUTE: delete (or comment out) its line.

    live("Name", (source_chat, source_topic), (dest_chat, dest_topic))
        MAIN copies new posts as they arrive (engine/intake.py). No history.
        source_topic None = the whole chat.
    history("Name", (source_chat, source_topic), (dest_chat, dest_topic), last=20)
        MAIN imports the last N posts once, then copies new ones (owner_routes.py).
    relay("Name", (source_chat, source_topic), relay_topic)
        ALT copies a provider into a VIP RELAY GROUP topic (engine/intake.py).

A new route starts at the newest post (nothing old is sent, except history's
last N); the account must already be a member of both chats. Edits, replies,
filters, formatting, recovery after restarts and the replay audit apply to every
route automatically. validate() runs on import and refuses a table that writes
into a retired topic or repeats a route, so a mistake fails at start-up, not in
a destination. Old paused definitions: archive/telegram_worker/routes_before_table_20261104.py.

worker  the Telegram account that sends: MAIN or ALT
path    intake (live), owner (history), wholechat (SamGtrades house style), relay (ALT)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

RELAY = -1004367822325  # VIP RELAY GROUP
VIP = -1003726286301  # IMPERIUM FX VIP
MARKET_MAKERS = -1003786932333
WORKERS = {"MAIN": ("intake", "owner", "wholechat"), "ALT": ("relay",)}

# Never written to (or read from, for 2530) by any route.
RETIRED_DESTINATIONS = {(VIP, 5), (RELAY, 2530), (-1003885074241, 4663), (-1003885074241, 4665)}
RETIRED_SOURCES = {(RELAY, 2530)}
RELAY_7250_ONLY_DESTINATION = (VIP, 12161)

Chat = Tuple[int, Optional[int]]


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

    def option(self, key: str, default: Any = None) -> Any:
        return dict(self.options).get(key, default)

    def as_dict(self) -> Dict[str, Any]:
        """The route dict the workers use."""
        route: Dict[str, Any] = {
            "name": self.name,
            "source_chat": self.source_chat,
            "source_topic": self.source_topic,
            "dest_chat": self.dest_chat,
            "dest_topic": self.dest_topic,
        }
        route.update((key, list(value) if isinstance(value, tuple) else value) for key, value in self.options)
        route["path"] = self.path
        return route


def live(name: str, source: Chat, dest: Chat, loop_guard: Optional[Tuple[int, ...]] = None) -> RouteSpec:
    """MAIN: copy new posts as they arrive. loop_guard: chats whose forwards are never copied back."""
    (source_chat, source_topic), (dest_chat, dest_topic) = source, dest
    options: List[Tuple[str, Any]] = [
        ("verify_title", False),
        ("anonymous_send_as", False),
        ("live_only", True),
        ("copy_reply_parent", False),
    ]
    if source_topic is not None:
        options.append(("strict_source_topic", True))
    guard = loop_guard if loop_guard is not None else tuple(dict.fromkeys((source_chat, dest_chat)))
    options += [("ordered_private", True), ("loop_guard_peers", guard)]
    return RouteSpec(name, "MAIN", "intake", source_chat, source_topic, dest_chat, dest_topic, tuple(options))


def history(name: str, source: Chat, dest: Chat, last: int = 20) -> RouteSpec:
    """MAIN: import the last `last` posts once, then copy new posts."""
    (source_chat, source_topic), (dest_chat, dest_topic) = source, dest
    options = (
        ("live_only", True),
        ("strict_source_topic", True),
        ("copy_reply_parent", False),
        ("anonymous_send_as", False),
        ("owner_managed", True),
        ("backfill", int(last)),
    )
    return RouteSpec(name, "MAIN", "owner", source_chat, source_topic, dest_chat, dest_topic, options)


def relay(name: str, source: Chat, relay_topic: int) -> RouteSpec:
    """ALT: copy a provider into a VIP RELAY GROUP topic."""
    return RouteSpec(name, "ALT", "relay", source[0], source[1], RELAY, relay_topic)


TABLE: Tuple[RouteSpec, ...] = (
    # ALT: providers -> VIP RELAY GROUP topics
    relay("ALT RELAY 1", (-1003364661276, None), 2),  # Private Circle G Trades
    relay("ARABIC VIP EN -> RELAY115", (-1002211486103, None), 115),  # T4T AE Scalping VIP, translated
    relay("NewAccount Topic5246 To Relay7248", (-1004349952583, 5246), 7248),  # Blackline B+SCALPING
    relay("NewAccount Topic5247 To Relay7250", (-1004349952583, 5247), 7250),  # Blackline B+XAU
    # MAIN: live copies (relay topics and Market Slayers -> VIP)
    live("Private Live 3838973021 To 9", (-1003838973021, None), (VIP, 9), loop_guard=(VIP,)),
    live("VIP RELAY Topic106 To VIP11", (RELAY, 106), (VIP, 11)),
    live("VIP RELAY Topic2 To VIP1364", (RELAY, 2), (VIP, 1364)),
    live("Relay508 To VIP7", (RELAY, 508), (VIP, 7)),  # restyled in the Imperium VIP format
    live("Relay7248 To VIP10877", (RELAY, 7248), (VIP, 10877)),
    # MAIN: live copies with a one-time history import
    history("Owner Relay7250 To VIP12161", (RELAY, 7250), (VIP, 12161), last=50),
    history("Owner VIP1364 To Partner28", (VIP, 1364), (MARKET_MAKERS, 28), last=20),
    history("Owner VIP10877 To Partner8", (VIP, 10877), (MARKET_MAKERS, 8), last=20),
    history("Owner VIP12161 To Partner24", (VIP, 12161), (MARKET_MAKERS, 24), last=20),
    # MAIN: whole-chat relay (its own module)
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
            "source and destination are the same" if source == dest else "",
            "destination topic required" if spec.dest_topic is None and spec.path != "wholechat" else "",
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

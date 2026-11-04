"""Which source posts must never be delivered.

Every rule here is a pure function of the message, the route and the static
configuration. The order of `skip_reason` matches the legacy pipeline so the
reason logged for a skipped post is the same one the old code would give.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set

from telethon.tl.types import MessageMediaWebPage

from .model import Route

RELAY_7250 = (-1004367822325, 7250)
RELAY_7250_ONLY_DESTINATION = (-1003726286301, 12161)

USERNAME_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])@[A-Za-z][A-Za-z0-9_]{4,31}(?![A-Za-z0-9_])")
URL_SCHEME_RE = re.compile(r"(?i)(?<![A-Za-z0-9._%+\-@])(?:https?://|tg://|www\.|t\.me/|telegram\.me/)\S+")
BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+\-@])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
    r"(?::\d{2,5})?"
    r"(?:/[^\s<>()]*)?"
)


def text_of(message: Any) -> str:
    return message.message or message.raw_text or message.text or ""


def entities_of(message: Any) -> List[Any]:
    return getattr(message, "entities", None) or []


def is_real_media(message: Any) -> bool:
    media = getattr(message, "media", None)
    return bool(media) and not isinstance(media, MessageMediaWebPage)


def marked_peer_id(peer: Any) -> Optional[int]:
    """PeerChannel/PeerChat -> the marked id used by routes (-100...)."""
    if peer is None:
        return None
    if isinstance(peer, int):
        return peer
    channel_id = getattr(peer, "channel_id", None)
    if channel_id:
        return int("-100" + str(abs(int(channel_id))))
    chat_id = getattr(peer, "chat_id", None)
    if chat_id:
        return -abs(int(chat_id))
    user_id = getattr(peer, "user_id", None)
    return int(user_id) if user_id else None


def forward_origins(message: Any) -> set:
    fwd = getattr(message, "fwd_from", None)
    if not fwd:
        return set()
    return {
        marked
        for marked in (marked_peer_id(getattr(fwd, attr, None)) for attr in ("from_id", "saved_from_peer"))
        if marked is not None
    }


def _peer_ids(obj: Any) -> Iterable[int]:
    if isinstance(obj, int):
        yield obj
        return
    for attr in ("user_id", "channel_id", "chat_id"):
        value = getattr(obj, attr, None)
        if value:
            yield int(value)


def sender_ids(message: Any) -> Set[int]:
    ids: Set[int] = set()
    for attr in ("sender_id", "from_id"):
        ids.update(_peer_ids(getattr(message, attr, None)))
    fwd = getattr(message, "fwd_from", None)
    if fwd:
        for attr in ("from_id", "saved_from_peer"):
            ids.update(_peer_ids(getattr(fwd, attr, None)))
    return ids


@dataclass(frozen=True)
class Rules:
    blocked_senders: FrozenSet[int] = frozenset()
    filter_mentions: bool = True
    filter_links: bool = True
    vip_chat: int = -1003726286301
    vip_blocked_source: int = -1002521699926
    stopped: Optional[Callable[[Dict[str, Any]], bool]] = field(default=None, compare=False)

    @classmethod
    def from_env(cls, stopped: Optional[Callable[[Dict[str, Any]], bool]] = None) -> "Rules":
        raw = os.environ.get("BLOCKED_SENDER_IDS", "7556281143").strip()
        return cls(
            blocked_senders=frozenset(int(x) for x in re.split(r"[,\s]+", raw) if x.strip()),
            filter_mentions=os.environ.get("FILTER_USERNAME_MENTIONS", "1").strip() == "1",
            filter_links=os.environ.get("FILTER_LINKS", "1").strip() == "1",
            stopped=stopped,
        )

    # -- content ---------------------------------------------------------
    def has_mention(self, message: Any) -> bool:
        if not self.filter_mentions:
            return False
        if any(type(e).__name__ == "MessageEntityMention" for e in entities_of(message)):
            return True
        text = text_of(message)
        return bool(text and USERNAME_RE.search(text))

    def has_link(self, message: Any) -> bool:
        if not self.filter_links:
            return False
        for entity in entities_of(message):
            name = type(entity).__name__
            if name == "MessageEntityUrl":
                return True
            if name == "MessageEntityTextUrl" and not str(
                getattr(entity, "url", "") or ""
            ).lower().startswith("mailto:"):
                return True
        text = text_of(message)
        return bool(text and (URL_SCHEME_RE.search(text) or BARE_DOMAIN_RE.search(text)))

    def blocked_content(self, message: Any) -> bool:
        return self.has_mention(message) or self.has_link(message)

    # -- origin ----------------------------------------------------------
    def blocked_sender(self, message: Any) -> bool:
        return bool(sender_ids(message) & self.blocked_senders)

    def vip_hard_block(self, message: Any, route: Route) -> bool:
        if route.dest_chat != self.vip_chat:
            return False
        return route.source_chat == self.vip_blocked_source or self.vip_blocked_source in forward_origins(
            message
        )

    def loop(self, message: Any, route: Route) -> bool:
        return bool(route.loop_guard_peers and forward_origins(message) & route.loop_guard_peers)

    # -- retired paths (hard stops kept from the legacy copy path) --------
    def retired(self, route: Route) -> Optional[str]:
        if (route.dest_chat, route.dest_topic) == (self.vip_chat, 5):
            return "vip5_retired"  # VIP topic 5 retired; outbound is always blocked
        if (route.source_chat, route.source_topic) == RELAY_7250 and (
            route.dest_chat,
            route.dest_topic,
        ) != RELAY_7250_ONLY_DESTINATION:
            return "relay7250_retired"  # relay 7250 may only feed VIP EXTRA PARTNER [ 4 ]
        return None

    # -- whole unit ------------------------------------------------------
    def skip_reason(
        self, unit: Sequence, route: Route, legacy_route: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """First rule that stops this post on this route, or None to deliver."""
        if self.stopped is not None and legacy_route is not None and self.stopped(legacy_route):
            return "owner_stopped"
        retired = self.retired(route)
        if retired:
            return retired
        if any(self.blocked_sender(m) for m in unit):
            return "blocked_sender"
        if any(self.vip_hard_block(m, route) for m in unit):
            return "vip_blocked_source"
        if any(self.loop(m, route) for m in unit):
            return "forward_loop"
        if any(self.blocked_content(m) for m in unit):
            return "mention_or_link"
        if not any(text_of(m) or is_real_media(m) for m in unit):
            return "empty_unsupported"
        return None

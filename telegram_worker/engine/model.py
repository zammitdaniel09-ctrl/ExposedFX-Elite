"""Typed values the delivery engine works with."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple, Union

# (type name, offset, length, extra) - comparable and hashable.
EntityKey = Tuple[str, int, int, Any]


@dataclass(frozen=True)
class Route:
    name: str
    source_chat: int
    source_topic: Optional[int]
    dest_chat: int
    dest_topic: int
    loop_guard_peers: frozenset = frozenset()
    owner_managed: bool = False
    formatter: str = "copy"

    @classmethod
    def from_legacy(cls, route: Mapping[str, Any], formatter: str = "copy") -> "Route":
        topic = route.get("source_topic")
        return cls(
            name=str(route.get("name") or ""),
            source_chat=int(route["source_chat"]),
            source_topic=int(topic) if topic is not None else None,
            dest_chat=int(route["dest_chat"]),
            dest_topic=int(route["dest_topic"]),
            loop_guard_peers=frozenset(int(p) for p in route.get("loop_guard_peers") or ()),
            owner_managed=bool(route.get("owner_managed")),
            formatter=formatter,
        )

    @property
    def key(self) -> Tuple[int, Optional[int], int, int]:
        return (self.source_chat, self.source_topic, self.dest_chat, self.dest_topic)

    def map_key(self, source_msg_id: int) -> str:
        return f"{self.source_chat}:{int(source_msg_id)}:{self.dest_chat}:{self.dest_topic}"


@dataclass(frozen=True)
class Skip:
    reason: str


@dataclass(frozen=True)
class Send:
    """One Telegram send: text, a single media post or an album."""
    kind: str                    # "text" | "media" | "album"
    dest_chat: int
    reply_to: int
    text: str
    entities: Tuple[EntityKey, ...]
    media: Tuple[Any, ...] = ()  # media identity keys, in send order


Plan = Union[Skip, Send]


def entity_key(entity) -> EntityKey:
    name = type(entity).__name__
    extra = None
    for attr in ("url", "document_id", "user_id", "language"):
        value = getattr(entity, attr, None)
        if value is not None:
            extra = value if not hasattr(value, "user_id") else getattr(value, "user_id")
            break
    return (name, int(getattr(entity, "offset", 0)), int(getattr(entity, "length", 0)), extra)


def entity_keys(entities) -> Tuple[EntityKey, ...]:
    return tuple(entity_key(e) for e in (entities or ()))


def media_key(media) -> Any:
    """Identity of a media object: the photo/document id it carries."""
    if media is None:
        return None
    for holder in ("photo", "document"):
        obj = getattr(media, holder, None)
        if obj is not None and getattr(obj, "id", None) is not None:
            return (holder, int(obj.id))
    return (type(media).__name__, None)

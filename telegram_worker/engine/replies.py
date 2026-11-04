"""Where a copied post goes in the destination: its mapped parent or the topic."""

from __future__ import annotations

from typing import Any, Callable, Iterable, List, Mapping, Union

from .model import Route


def _ids(value: Any) -> List[int]:
    """Message-map values are an int, a list of ints or (rarely) a dict of them."""
    if not value:
        return []
    items = (
        value.values() if isinstance(value, dict) else value if isinstance(value, (list, tuple)) else [value]
    )
    out = []
    for item in items:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if number and number not in out:
            out.append(number)
    return out


def keys_starting(mapping: Mapping[str, object], prefix: str) -> List[str]:
    """Map keys that start with `prefix`: an index range read on the SQLite map
    (no full scan on the event loop), a plain scan on a dict."""
    by_prefix = getattr(mapping, "keys_with_prefix", None)
    if callable(by_prefix):
        return list(by_prefix(prefix))
    return [key for key in list(mapping) if str(key).startswith(prefix)]


class MessageMap:
    """Read access to source -> destination message ids.

    Backed by the worker's message_map dict ("src_chat:src_id:dest_chat:dest_topic"
    -> id | [ids]); pass a callable to always read the worker's current dict.
    Delivery checks use the exact key only. Reply resolution may also scan
    key-by-parts, which maps written by older code needed.
    """

    def __init__(self, mapping: Union[Mapping[str, object], Callable[[], Mapping[str, object]]]):
        self._mapping = mapping

    @property
    def mapping(self) -> Mapping[str, object]:
        return self._mapping() if callable(self._mapping) else self._mapping

    def dest_ids(self, route: Route, source_msg_id: int, scan: bool = False) -> List[int]:
        mapping = self.mapping
        ids = _ids(mapping.get(route.map_key(source_msg_id)))
        if ids or not scan:
            return ids
        wanted = route.map_key(source_msg_id).split(":")
        out: List[int] = []
        for key in keys_starting(mapping, f"{route.source_chat}:{int(source_msg_id)}:"):
            if str(key).split(":") == wanted:
                out.extend(i for i in _ids(mapping.get(key)) if i not in out)
        return out

    def all_mapped(self, route: Route, unit: Iterable) -> bool:
        return all(self.dest_ids(route, m.id) for m in unit)

    def any_mapped(self, route: Route, unit: Iterable) -> bool:
        return any(self.dest_ids(route, m.id) for m in unit)


def reply_parent_ids(message: Any, route: Route) -> List[int]:
    """Real parents first, then thread ids; never the source topic root."""
    out: List[int] = []
    reply = getattr(message, "reply_to", None)
    candidates: List[Any] = [
        getattr(message, "reply_to_msg_id", None),
        getattr(reply, "reply_to_msg_id", None),
    ]
    for obj in (message, reply):
        candidates += [getattr(obj, "reply_to_top_id", None), getattr(obj, "top_msg_id", None)]
    for value in candidates:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number and number != route.source_topic and number not in out:
            out.append(number)
    return out


def reply_target(message: Any, route: Route, maps: MessageMap) -> int:
    for parent in reply_parent_ids(message, route):
        ids = maps.dest_ids(route, parent, scan=True)
        if ids:
            return ids[0]
    return route.dest_topic

"""Keeping copies in step with their source posts: edits and deletions (MAIN and ALT).

- An edit is applied to each mapped copy in place: same message id, rendered
  the way the route publishes it (restyled for VIP 7 on MAIN, translated on
  ALT's T4T route). An edit never creates, deletes or re-sends a copy, and a
  post copied as an album (several ids) is left alone.
- MAIN passes the edit on to copies of that copy (a VIP topic feeding a
  partner topic), at most `cascade_depth` levels; ALT does not, since MAIN
  watches ALT's relay copies itself.
- ALT never changes a copy's media structure (`same_media_only`): an edit that
  adds or removes media is reported and skipped.
- A deleted source post deletes every mapped copy and its map entries, and the
  copies of those copies like edits (MAIN).
- EditReconciler is a backup for missed edit events on one route: every
  interval it re-checks the newest posts and fixes copies that differ.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, MutableMapping, Optional, Sequence, Set, Tuple

from .model import entity_keys
from .rules import is_real_media, text_of

log = logging.getLogger("engine-mirror")
Route = Dict[str, Any]
Render = Callable[[Route, Any], Awaitable[Tuple[str, Optional[List[Any]]]]]
VisitKey = Tuple[int, int, int, int]


def mapped_ids(value: Any) -> List[int]:
    """Destination ids of a map entry: an int, or a list for albums."""
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


class Mirror:
    def __init__(
        self,
        name: str,
        client: Any,
        message_map: MutableMapping[str, Any],
        render: Render,
        blocked: Callable[[int, Any], bool] = lambda chat_id, message: False,
        routes_from: Callable[[int, Optional[int]], List[Route]] = lambda chat, topic: [],
        save: Callable[[], Any] = lambda: None,
        wait_for_map: float = 0.0,
        same_media_only: bool = False,
        cascade_depth: int = 0,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.client = client
        self.message_map = message_map
        self.render = render
        self.blocked = blocked
        self.routes_from = routes_from
        self.save = save
        self.wait_for_map = wait_for_map
        self.same_media_only = same_media_only
        self.cascade_depth = cascade_depth
        self.sleep = sleep
        self.monotonic = monotonic
        self.locks: Dict[VisitKey, asyncio.Lock] = {}

    # -- edits -----------------------------------------------------------------
    def dest_ids(self, route: Route, source_id: int) -> List[int]:
        key = f"{route['source_chat']}:{int(source_id)}:{route['dest_chat']}:{route['dest_topic']}"
        return mapped_ids(self.message_map.get(key))

    async def _wait_for_copy(self, route: Route, source_id: int) -> List[int]:
        # An edit can arrive right after the post; give its copy time to be recorded.
        deadline = self.monotonic() + self.wait_for_map
        ids = self.dest_ids(route, source_id)
        while not ids and self.monotonic() < deadline:
            await self.sleep(0.1)
            ids = self.dest_ids(route, source_id)
        return ids

    async def on_edit(self, chat_id: int, message: Any, routes: Sequence[Route]) -> None:
        """An edited source post: update its copies on every route that reads it."""
        if self.blocked(int(chat_id), message):
            return
        for route in routes:
            try:
                await self.edit(route, message)
            except Exception as exc:
                wait = getattr(exc, "seconds", None)
                if wait is not None:
                    log.warning("[EDIT FLOODWAIT] %s route=%s wait=%ss", self.name, route.get("name"), wait)
                    await self.sleep(min(float(wait) + 1.0, 60.0))
                else:
                    log.exception(
                        "[EDIT FAILED] %s route=%s msg=%s %s", self.name, route.get("name"), message.id, exc
                    )

    async def edit(
        self, route: Route, message: Any, depth: int = 0, visited: Optional[Set[VisitKey]] = None
    ) -> bool:
        """Edit the copy of `message` on `route` in place. Flood waits are raised to the caller."""
        visited = visited if visited is not None else set()
        key = (int(route["source_chat"]), int(message.id), int(route["dest_chat"]), int(route["dest_topic"]))
        if key in visited:
            return True
        visited.add(key)
        name = route.get("name")
        async with self.locks.setdefault(key, asyncio.Lock()):
            ids = await self._wait_for_copy(route, message.id)
            if not ids:
                log.info("[EDIT SKIPPED - NO COPY] %s route=%s source_msg=%s", self.name, name, message.id)
                return False
            if len(ids) != 1:
                log.warning(
                    "[EDIT SKIPPED - ALBUM COPY] %s route=%s source_msg=%s dest_ids=%s",
                    self.name,
                    name,
                    message.id,
                    ids,
                )
                return False
            dest_id = ids[0]
            if self.same_media_only and not await self._same_media(route, message, dest_id):
                return False
            text, entities = await self.render(route, message)
            if self.same_media_only and not text and not is_real_media(message):
                return False
            try:
                await self.client.edit_message(
                    route["dest_chat"],
                    dest_id,
                    text or "",
                    formatting_entities=(entities if text else None),
                    parse_mode=None,
                    link_preview=True,
                )
                log.warning(
                    "[EDIT PROPAGATED IN PLACE] %s depth=%s route=%s source_msg=%s dest_msg=%s dest=%s_%s",
                    self.name,
                    depth,
                    name,
                    message.id,
                    dest_id,
                    route["dest_chat"],
                    route["dest_topic"],
                )
            except Exception as exc:
                if getattr(exc, "seconds", None) is not None:
                    raise  # flood wait: the caller backs off
                if "not modified" not in str(exc).lower() and type(exc).__name__ != "MessageNotModifiedError":
                    log.warning(
                        "[EDIT PROPAGATION FAILED SAFE] %s route=%s source_msg=%s dest_msg=%s %s: %s",
                        self.name,
                        name,
                        message.id,
                        dest_id,
                        type(exc).__name__,
                        exc,
                    )
                    return False
                log.info("[EDIT ALREADY CURRENT] %s route=%s source_msg=%s", self.name, name, message.id)
        if depth < self.cascade_depth:
            await self._cascade(route, dest_id, depth, visited)
        return True

    async def _same_media(self, route: Route, message: Any, dest_id: int) -> bool:
        copy = await self.client.get_messages(route["dest_chat"], ids=dest_id)
        if not copy:
            log.warning(
                "[EDIT DESTINATION MISSING] %s route=%s dest_msg=%s", self.name, route.get("name"), dest_id
            )
            return False
        if is_real_media(message) != is_real_media(copy):
            log.warning(
                "[EDIT MEDIA STRUCTURE CHANGE SKIPPED] %s source_msg=%s NO_DELETE=True NO_RESEND=True",
                self.name,
                message.id,
            )
            return False
        return True

    async def _cascade(self, route: Route, dest_id: int, depth: int, visited: Set[VisitKey]) -> None:
        downstream = self.routes_from(int(route["dest_chat"]), route["dest_topic"])
        if not downstream:
            return
        try:
            copy = await self.client.get_messages(route["dest_chat"], ids=dest_id)
        except Exception as exc:
            log.warning(
                "[EDIT CASCADE SOURCE FETCH FAILED] chat=%s msg=%s %s", route["dest_chat"], dest_id, exc
            )
            return
        if not copy:
            return
        for next_route in downstream:
            if self.dest_ids(next_route, dest_id):
                await self.edit(next_route, copy, depth=depth + 1, visited=visited)

    # -- deletions ---------------------------------------------------------------
    async def delete(self, source_chat: int, source_id: int, depth: int = 0) -> bool:
        """Delete every mapped copy of a deleted source post (and, like edits, the copies
        of those copies up to `cascade_depth` levels), then their map entries."""
        prefix = f"{int(source_chat)}:{int(source_id)}:"
        keys_with_prefix = getattr(self.message_map, "keys_with_prefix", None)
        keys = (
            list(keys_with_prefix(prefix))
            if keys_with_prefix
            else [k for k in self.message_map if k.startswith(prefix)]
        )
        targets: Dict[int, List[int]] = {}
        found = []
        for key in keys:
            ids = mapped_ids(self.message_map.get(key))
            parts = key.split(":")
            if len(parts) != 4 or not ids:
                continue
            chat_ids = targets.setdefault(int(parts[2]), [])
            chat_ids.extend(i for i in ids if i not in chat_ids)
            found.append(key)
        if not targets:
            if depth == 0:
                log.info("[delete mirror] no mapped destination for source=%s:%s", source_chat, source_id)
            return False
        deleted = False
        removed: List[Tuple[int, int]] = []
        for dest_chat, ids in targets.items():
            try:
                await self.client.delete_messages(dest_chat, ids)
                log.info(
                    "[delete mirror] deleted dest=%s msgs=%s for source=%s:%s",
                    dest_chat,
                    ids,
                    source_chat,
                    source_id,
                )
                deleted = True
                removed.extend((dest_chat, i) for i in ids)
            except Exception as exc:
                log.warning(
                    "[delete mirror failed] dest=%s ids=%s source=%s:%s: %s",
                    dest_chat,
                    ids,
                    source_chat,
                    source_id,
                    exc,
                )
        if deleted:
            for key in found:
                self.message_map.pop(key, None)
            self.save()
        if depth < self.cascade_depth:
            for chat, copy_id in removed:
                await self.delete(chat, copy_id, depth + 1)
        return deleted

    async def on_delete(self, chat_id: Optional[int], deleted_ids: Sequence[int]) -> None:
        if chat_id is None:
            log.warning("[delete mirror skipped] missing chat_id deleted_ids=%s", list(deleted_ids))
            return
        for source_id in deleted_ids:
            try:
                await self.delete(chat_id, source_id)
            except Exception as exc:
                log.warning("[delete mirror item failed] chat=%s msg=%s: %s", chat_id, source_id, exc)


def edit_fingerprint(message: Any) -> Optional[str]:
    """Identity of one version of an edited post (None if it was never edited)."""
    edit_date = getattr(message, "edit_date", None)
    if edit_date is None:
        return None
    stamp = edit_date.isoformat() if hasattr(edit_date, "isoformat") else str(edit_date)
    payload = "|".join(
        [str(int(message.id)), stamp, text_of(message), repr(entity_keys(getattr(message, "entities", None)))]
    )
    return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()


class EditReconciler:
    """Backup for missed edit events on one route: the newest posts are re-checked every interval."""

    def __init__(
        self,
        mirror: Mirror,
        route: Route,
        fetch: Callable[[int], Awaitable[Sequence[Any]]],
        state_path: Path,
        interval: float = 30.0,
        limit: int = 40,
    ) -> None:
        self.mirror = mirror
        self.route = route
        self.fetch = fetch
        self.state_path = Path(state_path)
        self.interval = interval
        self.limit = limit
        self.state = self._load()

    def _load(self) -> Dict[str, str]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def _remember(self, message: Any, fingerprint: str) -> None:
        self.state[str(int(message.id))] = fingerprint
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, separators=(",", ":")), encoding="utf-8")
        tmp.replace(self.state_path)

    async def once(self) -> int:
        route, mirror = self.route, self.mirror
        updated = 0
        for message in await self.fetch(self.limit) or []:
            fingerprint = edit_fingerprint(message)
            if fingerprint is None or self.state.get(str(int(message.id))) == fingerprint:
                continue
            try:
                if mirror.blocked(int(route["source_chat"]), message):
                    self._remember(message, fingerprint)
                    continue
                ids = mirror.dest_ids(route, message.id)
                if len(ids) != 1:
                    continue
                copy = await mirror.client.get_messages(route["dest_chat"], ids=ids[0])
                if not copy:
                    log.warning("[EDIT RECONCILE DEST MISSING] source_msg=%s dest_msg=%s", message.id, ids[0])
                    self._remember(message, fingerprint)
                    continue
                text, entities = await mirror.render(route, message)
                differs = text != text_of(copy) or entity_keys(entities) != entity_keys(
                    getattr(copy, "entities", None)
                )
                if differs:
                    if not await mirror.edit(route, message):
                        continue
                    updated += 1
                    log.warning(
                        "[EDIT RECONCILED] route=%s source_msg=%s dest_msg=%s",
                        route.get("name"),
                        message.id,
                        ids[0],
                    )
                self._remember(message, fingerprint)
            except Exception as exc:
                if getattr(exc, "seconds", None) is not None:
                    raise
                log.warning(
                    "[EDIT RECONCILE ITEM FAILED SAFE] source_msg=%s %s: %s",
                    message.id,
                    type(exc).__name__,
                    exc,
                )
        return updated

    async def run(self) -> None:
        log.warning(
            "[MAPPED EDIT RECONCILER READY] route=%s interval=%ss fetch_limit=%s",
            self.route.get("name"),
            self.interval,
            self.limit,
        )
        while True:
            try:
                await self.once()
            except Exception as exc:
                wait = getattr(exc, "seconds", None)
                if wait is not None:
                    log.warning("[MAPPED EDIT RECONCILER FLOODWAIT] wait=%ss", int(wait) + 1)
                    await self.mirror.sleep(float(wait) + 1.0)
                else:
                    log.exception("[MAPPED EDIT RECONCILER FAILED SAFE] %s: %s", type(exc).__name__, exc)
            await self.mirror.sleep(self.interval)

"""Engine intake: how new source posts reach delivery, shared by MAIN and ALT.

- Telegram pushes (NewMessage) are the fast path. Album items are gathered
  here until no new item has arrived for `album_gather` seconds, then queued as
  one post. Each source (chat + topic) has one writer and an oldest-first
  queue, so a source's posts are delivered in order and never twice at once;
  the writer holds a newer post while an older album is still being gathered.
- A watchdog per source reads only the posts after its checkpoint, oldest
  first, every `watchdog_seconds`. It queues anything not handled yet: a
  missed push, a failed delivery, or posts that arrived during a restart.
- The checkpoint ("every post up to here is handled") is moved only by the
  watchdog, over posts it has seen, so a later success can never skip an
  earlier failure. Checkpoints live in the SQLite state store.
- MAIN sets `stale_after`: a post older than that is not sent (a late signal
  misleads members) but reported as "[GAP STALE NOT SENT]". A post that keeps
  failing for `give_up_after` seconds is skipped and reported as "[INTAKE GAVE UP]".
  The health monitor turns both into a Saved Messages notice.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .replies import keys_starting

SourceKey = Tuple[int, Optional[int]]
Routes = List[Dict[str, Any]]
Deliver = Callable[[Dict[str, Any], List[Any], str], Awaitable[bool]]
log = logging.getLogger("engine-intake")
PAGE = 50
MAX_PAGES = 10
ALBUM_SETTLE_SECONDS = 2.0
ALBUM_GATHER_SECONDS = 0.6  # Telethon's own Album event waits 0.5 s for more items
GIVE_UP_SECONDS = 3600.0


class Checkpoints:
    """Per-source "handled up to" ids, in the state store's SQLite file."""

    def __init__(self, db: sqlite3.Connection, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self.clock = clock
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS intake_checkpoints "
            "(source TEXT PRIMARY KEY, last_id INTEGER NOT NULL, updated REAL NOT NULL)"
        )

    @staticmethod
    def key(source: SourceKey) -> str:
        return f"{source[0]}:{source[1]}"

    def get(self, source: SourceKey) -> Optional[int]:
        row = self.db.execute(
            "SELECT last_id FROM intake_checkpoints WHERE source = ?", (self.key(source),)
        ).fetchone()
        return None if row is None else int(row[0])

    def set(self, source: SourceKey, last_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO intake_checkpoints (source, last_id, updated) VALUES (?, ?, ?)",
            (self.key(source), int(last_id), self.clock()),
        )


@dataclass
class _Album:
    items: Dict[int, Any]
    due: float  # loop time after which no more items are expected


@dataclass
class _Source:
    checkpoint: int
    handled: Set[int] = field(default_factory=set)  # message ids above the checkpoint
    queued: Set[int] = field(default_factory=set)
    attempts: Dict[int, int] = field(default_factory=dict)
    first_failure: Dict[int, float] = field(default_factory=dict)
    albums: Dict[int, _Album] = field(default_factory=dict)  # grouped_id -> items being gathered
    album_queued: asyncio.Event = field(default_factory=asyncio.Event)
    queue: "asyncio.PriorityQueue[Tuple[int, int, List[Any], str, float]]" = field(
        default_factory=asyncio.PriorityQueue
    )


def units_of(messages: Sequence[Any]) -> List[List[Any]]:
    """Oldest first; an album is one unit."""
    ordered = sorted(messages, key=lambda m: int(m.id))
    units: List[List[Any]] = []
    used: Set[int] = set()
    for message in ordered:
        if int(message.id) in used:
            continue
        grouped = getattr(message, "grouped_id", None)
        unit = [m for m in ordered if grouped and getattr(m, "grouped_id", None) == grouped] or [message]
        used.update(int(m.id) for m in unit)
        units.append(unit)
    return units


def _ids(unit: Sequence[Any]) -> List[int]:
    return [int(m.id) for m in unit]


def _age(unit: Sequence[Any], now: float) -> Optional[float]:
    dates = [m.date.timestamp() for m in unit if getattr(m, "date", None)]
    return now - max(dates) if dates else None


class Intake:
    def __init__(
        self,
        name: str,
        client: Any,
        groups: Dict[SourceKey, Routes],
        deliver: Deliver,
        checkpoints: Checkpoints,
        topic_of: Callable[[Any, int], Optional[int]],
        initial_checkpoint: Callable[[SourceKey, Routes], Awaitable[int]],
        pending: Optional[Callable[[Dict[str, Any], List[Any]], bool]] = None,
        watchdog_seconds: float = 30.0,
        stale_after: Optional[float] = None,
        give_up_after: float = GIVE_UP_SECONDS,
        album_gather: float = ALBUM_GATHER_SECONDS,
        fetch_concurrency: int = 3,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.name = name
        self.client = client
        self.groups = groups
        self.deliver = deliver
        self.checkpoints = checkpoints
        self.topic_of = topic_of
        self.initial_checkpoint = initial_checkpoint
        self.pending = pending
        self.watchdog_seconds = watchdog_seconds
        self.stale_after = stale_after
        self.give_up_after = give_up_after
        self.album_gather = album_gather
        self.fetch_limit = asyncio.Semaphore(max(1, fetch_concurrency))
        self.clock = clock
        self.sleep = sleep
        self.sources: Dict[SourceKey, _Source] = {}
        self.order = itertools.count()
        self.tasks: List["asyncio.Task[None]"] = []
        self.album_tasks: Set["asyncio.Task[None]"] = set()
        self.closing = False  # set on shutdown: no new deliveries start
        self.inflight = 0

    # -- routing -------------------------------------------------------------
    def sources_for(self, chat_id: int, message: Any) -> List[SourceKey]:
        try:
            topic = self.topic_of(message, int(chat_id))
        except Exception:
            topic = None
        return [
            source
            for source in self.groups
            if source[0] == int(chat_id) and (source[1] is None or source[1] == topic)
        ]

    # -- queue -----------------------------------------------------------------
    async def submit(self, source: SourceKey, unit: Sequence[Any], reason: str) -> bool:
        """Queue a post unless it is handled, queued or not ready to be read yet."""
        state = self.sources.get(source)
        unit = sorted(unit, key=lambda m: int(m.id))
        if state is None or not unit or self.closing:
            return False  # not started (or stopping): the next sweep reads it from the checkpoint
        if max(_ids(unit)) <= state.checkpoint:
            return False
        remainder = [m for m in unit if int(m.id) not in state.handled]
        if not remainder:
            return False
        if len(remainder) < len(unit):
            # Items Telegram added to an album after it was delivered: send them on their own.
            log.warning(
                "[INTAKE ALBUM REMAINDER] %s source=%s_%s ids=%s delivered=%s",
                self.name,
                source[0],
                source[1],
                _ids(remainder),
                [i for i in _ids(unit) if i in state.handled],
            )
            unit = remainder
        first = int(unit[0].id)
        if first in state.queued:
            return False
        state.queued.add(first)
        await state.queue.put((first, next(self.order), unit, reason, time.monotonic()))
        return True

    async def on_message(self, event: Any) -> None:
        message = event.message
        for source in self.sources_for(event.chat_id, message):
            if getattr(message, "grouped_id", None):
                self.gather(source, message)
            else:
                await self.submit(source, [message], "telegram_push")

    # -- albums ------------------------------------------------------------------
    def gather(self, source: SourceKey, message: Any) -> None:
        """Collect an album item; the album is queued once no item came for `album_gather` s.

        Telethon's Album event is not used: it hands albums over late enough for a
        newer single post to overtake them, and it keeps every album it ever saw."""
        state = self.sources.get(source)
        if state is None or self.closing:
            return  # the next sweep reads it from the checkpoint
        loop = asyncio.get_running_loop()
        grouped = int(message.grouped_id)
        album = state.albums.get(grouped)
        if album is None:
            album = state.albums[grouped] = _Album(items={}, due=0.0)
            task = loop.create_task(self._queue_album(source, grouped, album))
            self.album_tasks.add(task)  # the loop holds tasks weakly
            task.add_done_callback(self.album_tasks.discard)
        album.items[int(message.id)] = message
        album.due = loop.time() + self.album_gather

    async def _queue_album(self, source: SourceKey, grouped: int, album: _Album) -> None:
        state = self.sources[source]
        loop = asyncio.get_running_loop()
        try:
            while album.due > loop.time():
                await asyncio.sleep(album.due - loop.time())
            state.albums.pop(grouped, None)
            await self.submit(source, list(album.items.values()), "telegram_album")
        except Exception as exc:
            log.warning("[INTAKE ALBUM GATHER FAILED] %s source=%s %s", self.name, source, exc)
        finally:
            state.albums.pop(grouped, None)
            state.album_queued.set()

    async def _older_album_gathering(self, state: _Source, first: int) -> bool:
        """An album older than `first` is still being gathered: wait for it (bounded)."""
        if not any(album.items and min(album.items) < first for album in state.albums.values()):
            return False
        state.album_queued.clear()
        try:
            await asyncio.wait_for(state.album_queued.wait(), self.album_gather * 4 + 1.0)
        except asyncio.TimeoutError:
            pass
        return True

    async def worker(self, source: SourceKey) -> None:
        state = self.sources[source]
        while True:
            first, order, unit, reason, queued_at = await state.queue.get()
            requeued = False
            try:
                if self.closing:
                    continue  # left for the next start's first sweep
                if await self._older_album_gathering(state, first):
                    await state.queue.put((first, order, unit, reason, queued_at))  # after the album
                    requeued = True
                    continue
                if self._stale(source, unit, _age(unit, self.clock())):
                    state.handled.update(_ids(unit))  # waited too long in the queue (flood wait, outage)
                    state.attempts.pop(first, None)
                    continue
                self.inflight += 1
                try:
                    ok = await self.deliver_unit(source, unit, reason)
                finally:
                    self.inflight -= 1
                self.settle(source, unit, ok)
                if ok:
                    age = _age(unit, self.clock())
                    log.info(
                        "[INTAKE COMMITTED] %s source=%s_%s ids=%s reason=%s queue_ms=%.1f telegram_age_ms=%s",
                        self.name,
                        source[0],
                        source[1],
                        _ids(unit),
                        reason,
                        (time.monotonic() - queued_at) * 1000.0,
                        "unknown" if age is None else f"{max(0.0, age) * 1000.0:.0f}",
                    )
            except Exception as exc:  # never let one post stop the source
                log.exception("[INTAKE ERROR] %s source=%s ids=%s %s", self.name, source, _ids(unit), exc)
            finally:
                if not requeued:
                    state.queued.discard(first)
                state.queue.task_done()

    async def deliver_unit(self, source: SourceKey, unit: List[Any], reason: str) -> bool:
        """Every route of the source at once; handled only when all of them are."""
        results = await asyncio.gather(
            *(self.deliver(route, unit, reason) for route in self.groups[source]), return_exceptions=True
        )
        for route, result in zip(self.groups[source], results):
            if isinstance(result, BaseException):
                log.error(
                    "[INTAKE DELIVERY ERROR] %s route=%s ids=%s %s: %s",
                    self.name,
                    route.get("name"),
                    _ids(unit),
                    type(result).__name__,
                    result,
                )
        return all(result is True for result in results)

    def settle(self, source: SourceKey, unit: Sequence[Any], ok: bool) -> None:
        state = self.sources[source]
        first = int(unit[0].id)
        if ok:
            state.attempts.pop(first, None)
            state.first_failure.pop(first, None)
            state.handled.update(_ids(unit))
            return
        # Time-based: independent of the watchdog interval, and of flood waits
        # that fail every retry at once.
        state.attempts[first] = state.attempts.get(first, 0) + 1
        since = state.first_failure.setdefault(first, self.clock())
        if self.clock() - since >= self.give_up_after:
            log.error(
                "[INTAKE GAVE UP] route=%s ids=%s attempts=%s",
                self._names(source),
                _ids(unit),
                state.attempts.pop(first),
            )
            state.first_failure.pop(first, None)
            state.handled.update(_ids(unit))

    # -- watchdog ----------------------------------------------------------------
    async def fetch_after(self, source: SourceKey, after_id: int) -> List[Any]:
        """Posts newer than after_id, oldest first, a page at a time."""
        chat, topic = source
        found: List[Any] = []
        cursor = after_id
        for _ in range(MAX_PAGES):
            kwargs: Dict[str, Any] = {"limit": PAGE, "min_id": cursor, "reverse": True}
            if topic is not None:
                kwargs["reply_to"] = topic
            async with self.fetch_limit:
                raw = await self.client.get_messages(chat, **kwargs)
            page = [m for m in (raw or []) if m is not None and int(m.id) > cursor]
            if not page:
                break
            found.extend(m for m in page if int(m.id) != (topic or 0) and not getattr(m, "action", None))
            cursor = max(int(m.id) for m in page)
            if len(page) < PAGE:
                break
        return found

    async def sweep(self, source: SourceKey, reason: str = "watchdog") -> int:
        """Queue every unhandled post after the checkpoint, then move the checkpoint."""
        state = self.sources[source]
        now = self.clock()
        queued = 0
        advance = state.checkpoint
        contiguous = True
        for unit in units_of(await self.fetch_after(source, state.checkpoint)):
            first, ids = int(unit[0].id), _ids(unit)
            age = _age(unit, now)
            if getattr(unit[0], "grouped_id", None) and age is not None and age < ALBUM_SETTLE_SECONDS:
                break  # Telegram may still be adding items to this album
            done = all(i in state.handled for i in ids)
            if not done and first not in state.queued and self._stale(source, unit, age):
                state.handled.update(ids)
                done = True
            if done:
                if contiguous:
                    advance = max(advance, max(ids))
                continue
            contiguous = False
            if await self.submit(source, unit, reason):
                queued += 1
        if advance > state.checkpoint:
            state.checkpoint = advance
            state.handled = {i for i in state.handled if i > advance}
            for failures in (state.attempts, state.first_failure):  # e.g. deleted before success
                for old in [i for i in failures if i <= advance]:
                    del failures[old]
            self.checkpoints.set(source, advance)
        return queued

    def _stale(self, source: SourceKey, unit: List[Any], age: Optional[float]) -> bool:
        """Too old to send (MAIN). Reported when a route would still have sent it."""
        if self.stale_after is None or age is None or age <= self.stale_after:
            return False
        routes = [r for r in self.groups[source] if self._would_send(r, unit)]
        if routes:
            log.warning(
                "[GAP STALE NOT SENT] route=%s ids=%s age=%ss max=%ss",
                ", ".join(str(r.get("name", "?")) for r in routes),
                _ids(unit),
                int(age),
                int(self.stale_after),
            )
        return True

    def _would_send(self, route: Dict[str, Any], unit: List[Any]) -> bool:
        if self.pending is None:
            return True
        try:
            return bool(self.pending(route, unit))
        except Exception:
            return True  # cannot tell: report it rather than stall the source

    async def drain(self, timeout: float = 2.0) -> None:
        """Shutdown: start no new delivery and let the ones in flight record their copies."""
        self.closing = True
        deadline = time.monotonic() + timeout
        while self.inflight and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        log.warning("[INTAKE DRAINED] %s in_flight=%s", self.name, self.inflight)

    async def run_source(self, source: SourceKey) -> None:
        stored = self.checkpoints.get(source)
        if stored is None and any(route.get("from_start") for route in self.groups[source]):
            stored = 0  # route_table from_start=True: a new route copies its source from the first post
            self.checkpoints.set(source, stored)
        while stored is None:
            try:
                stored = int(await self.initial_checkpoint(source, self.groups[source]))
                self.checkpoints.set(source, stored)
            except Exception as exc:
                log.warning(
                    "[INTAKE START RETRY] %s source=%s %s: %s", self.name, source, type(exc).__name__, exc
                )
                await self.sleep(5)
        self.sources[source] = _Source(checkpoint=stored)
        log.warning(
            "[INTAKE READY] %s source=%s_%s routes=%s checkpoint=%s watchdog=%ss stale_after=%s",
            self.name,
            source[0],
            source[1],
            self._names(source),
            stored,
            self.watchdog_seconds,
            self.stale_after,
        )
        writer_started = False
        reason = "deployment_gap"
        while True:
            try:
                queued = await self.sweep(source, reason)
                if queued:
                    log.warning(
                        "[INTAKE RECOVERED] %s source=%s_%s queued=%s reason=%s",
                        self.name,
                        source[0],
                        source[1],
                        queued,
                        reason,
                    )
            except Exception as exc:
                log.warning(
                    "[INTAKE WATCHDOG RETRY] %s source=%s %s: %s", self.name, source, type(exc).__name__, exc
                )
                wait = getattr(exc, "seconds", None)
                if wait:
                    await self.sleep(float(wait))
            if not writer_started:  # after the first sweep, so restart gaps go out oldest first
                self.tasks.append(asyncio.get_running_loop().create_task(self.worker(source)))
                writer_started = True
            reason = "watchdog"
            await self.sleep(self.watchdog_seconds)

    # -- start -----------------------------------------------------------------
    def attach(self) -> None:
        from telethon import events

        chats = sorted({source[0] for source in self.groups})
        self.client.add_event_handler(self._safe(self.on_message), events.NewMessage(chats=chats))

    def _safe(self, handler: Callable[[Any], Awaitable[None]]) -> Callable[[Any], Awaitable[None]]:
        async def run(event: Any) -> None:
            try:
                await handler(event)
            except Exception as exc:
                log.exception(
                    "[INTAKE HANDLER ERROR] %s chat=%s %s", self.name, getattr(event, "chat_id", None), exc
                )

        return run

    def start(self) -> List["asyncio.Task[None]"]:
        self.attach()
        loop = asyncio.get_running_loop()
        self.tasks.extend(loop.create_task(self.run_source(source)) for source in self.groups)
        return self.tasks

    def _names(self, source: SourceKey) -> str:
        return ", ".join(str(r.get("name", "?")) for r in self.groups[source])


def group_by_source(routes: Sequence[Dict[str, Any]]) -> Dict[SourceKey, Routes]:
    groups: Dict[SourceKey, Routes] = {}
    for route in routes:
        topic = route.get("source_topic")
        key = (int(route["source_chat"]), int(topic) if topic is not None else None)
        groups.setdefault(key, []).append(route)
    return groups


def last_delivered(message_map: Any, route: Dict[str, Any]) -> int:
    """Newest source id this route has a copy of (0 if none)."""
    prefix = f"{int(route['source_chat'])}:"
    suffix = f":{int(route['dest_chat'])}:{int(route['dest_topic'])}"
    newest = 0
    for key in keys_starting(message_map, prefix):
        if key.endswith(suffix):
            try:
                newest = max(newest, int(key.split(":")[1]))
            except (IndexError, ValueError):
                continue
    return newest


async def newest_ids(client: Any, source: SourceKey, limit: int = PAGE) -> List[int]:
    kwargs: Dict[str, Any] = {"limit": limit}
    if source[1] is not None:
        kwargs["reply_to"] = source[1]
    messages = await client.get_messages(source[0], **kwargs) or []
    return sorted(int(m.id) for m in messages if m is not None and int(m.id) != (source[1] or 0))


async def first_start_checkpoint(client: Any, source: SourceKey, known: Sequence[int]) -> int:
    """Where a source with no stored checkpoint starts.

    `known` are the routes' last handled ids from the old intake (0 = none).
    With none, start at the newest post: no history is copied. Otherwise start
    after the oldest of them, but never more than PAGE posts back.
    """
    newest = await newest_ids(client, source)
    handled = [int(k) for k in known if int(k) > 0]
    if not handled:
        return newest[-1] if newest else 0
    start = min(handled)
    if len(newest) >= PAGE:
        start = max(start, newest[0] - 1)
    return start

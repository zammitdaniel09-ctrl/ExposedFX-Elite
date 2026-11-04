"""Durable source -> destination message map for both workers (SQLite on the volume).

Replaces message_map.json as the source of truth. It behaves like the dict
the workers always used (get, [], in, items, keys, len, del, pop), so every
reader keeps working, but:

- each write is one indexed row (WAL journal), instead of rewriting the whole
  multi-megabyte JSON file on every delivery;
- a crash can never leave a half-written map;
- message_map.json is still written, as a snapshot at most every
  SNAPSHOT_SECONDS from a background thread, so a rollback to older code
  finds a current map; on start, entries only the JSON has (written by such a
  rollback) are merged in, so rolling forward again loses nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger("state-store")
SNAPSHOT_SECONDS = 300
DRAIN_SECONDS = 2.0
SHUTDOWN_HOOKS: List[Callable[[], Awaitable[Any]]] = []  # awaited (bounded) before the shutdown snapshot


def register_shutdown_hook(hook: Callable[[], Awaitable[Any]]) -> None:
    SHUTDOWN_HOOKS.append(hook)


_MISSING = object()


class SQLiteMap(MutableMapping):
    def __init__(
        self,
        path: Path,
        legacy_json: Optional[Path] = None,
        load_legacy: Optional[Callable[[], Dict[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.legacy_json = Path(legacy_json) if legacy_json else None
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._connect()
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS message_map (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)"
        )
        self.last_snapshot = 0.0
        self._snapshot_thread: Optional[threading.Thread] = None
        self.imported = self._merge_legacy(load_legacy)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        return db

    def _merge_legacy(self, load_legacy: Optional[Callable[[], Dict[str, Any]]]) -> int:
        """Add JSON entries the database does not have (first start, or after a rollback)."""
        source = self.legacy_json
        if load_legacy is None and (source is None or not source.exists()):
            return 0
        try:
            if load_legacy is not None:
                legacy = load_legacy()
            else:
                assert source is not None
                legacy = json.loads(source.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("[STATE STORE LEGACY MAP UNREADABLE] %s: %s", type(exc).__name__, exc)
            return 0
        if not isinstance(legacy, dict) or not legacy:
            return 0
        now = self.clock()
        before = len(self)
        with self.db:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT OR IGNORE INTO message_map (key, value, updated) VALUES (?, ?, ?)",
                ((str(k), json.dumps(v), now) for k, v in legacy.items()),
            )
        added = len(self) - before
        if added:
            log.warning(
                "[STATE STORE MERGED LEGACY MAP] added=%s total=%s path=%s", added, len(self), self.path
            )
        return added

    # -- mapping protocol --------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM message_map WHERE key = ?", (str(key),)).fetchone()
        if row is None:
            raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO message_map (key, value, updated) VALUES (?, ?, ?)",
            (str(key), json.dumps(value), self.clock()),
        )

    def __delitem__(self, key: str) -> None:
        if self.db.execute("DELETE FROM message_map WHERE key = ?", (str(key),)).rowcount == 0:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter([row[0] for row in self.db.execute("SELECT key FROM message_map")])

    def __len__(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM message_map").fetchone()[0])

    def __contains__(self, key: object) -> bool:
        return self.db.execute("SELECT 1 FROM message_map WHERE key = ?", (str(key),)).fetchone() is not None

    def items(self) -> List[Tuple[str, Any]]:  # type: ignore[override]
        """A snapshot list (callers iterate it while deleting entries)."""
        return [(k, json.loads(v)) for k, v in self.db.execute("SELECT key, value FROM message_map")]

    def keys_with_prefix(self, prefix: str) -> List[str]:
        """Keys starting with `prefix`, read through the primary-key index."""
        if not prefix:
            return list(self)
        upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        rows = self.db.execute("SELECT key FROM message_map WHERE key >= ? AND key < ?", (prefix, upper))
        return [row[0] for row in rows]

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM message_map WHERE key = ?", (str(key),)).fetchone()
        return default if row is None else json.loads(row[0])

    def pop(self, key: str, default: Any = _MISSING) -> Any:
        value = self.get(key, _MISSING)
        if value is _MISSING:
            if default is _MISSING:
                raise KeyError(key)
            return default
        del self[key]
        return value

    # -- JSON snapshot for rollbacks ------------------------------------------
    def snapshot_to(self, path: Optional[Path] = None, force: bool = False) -> bool:
        """Write the JSON copy in the background, at most every SNAPSHOT_SECONDS."""
        target = Path(path) if path else self.legacy_json
        if target is None:
            return False
        if not force and self.clock() - self.last_snapshot < SNAPSHOT_SECONDS:
            return False
        if self._snapshot_thread is not None and self._snapshot_thread.is_alive():
            return False
        self.last_snapshot = self.clock()
        self._snapshot_thread = threading.Thread(target=self._write_snapshot, args=(target,), daemon=True)
        self._snapshot_thread.start()
        return True

    def _write_snapshot(self, target: Path) -> None:
        try:
            reader = sqlite3.connect(str(self.path), timeout=30)
            try:
                data = {k: json.loads(v) for k, v in reader.execute("SELECT key, value FROM message_map")}
            finally:
                reader.close()
            tmp = target.with_name(f"{target.name}.{threading.get_ident()}.snapshot.tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:
            log.warning("[STATE STORE SNAPSHOT FAILED] %s: %s", type(exc).__name__, exc)

    def snapshot_on_exit(self, path: Optional[Path] = None) -> bool:
        """Write the JSON copy when Railway stops the process (SIGTERM).

        A rollback to older code then starts from the current map, not one up
        to SNAPSHOT_SECONDS old. The process still ends exactly as before.
        """
        target = Path(path) if path else self.legacy_json
        if target is None:
            return False
        previous = signal.getsignal(signal.SIGTERM)
        draining: List[bool] = []

        def finish() -> None:
            self.wait_snapshot(5.0)
            self._write_snapshot(target)
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

        async def drain_then_finish() -> None:
            # Let deliveries already sent to Telegram record their copy, so the
            # next start does not send them again; Railway kills after 3 s anyway.
            hooks = [hook() for hook in SHUTDOWN_HOOKS]
            try:
                await asyncio.wait_for(asyncio.gather(*hooks, return_exceptions=True), DRAIN_SECONDS)
            except Exception:
                pass
            finish()

        def handler(signum: int, frame: Any) -> None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is None or not SHUTDOWN_HOOKS or draining:
                finish()  # no event loop, nothing to drain, or a second SIGTERM
                return
            draining.append(True)
            loop.call_soon_threadsafe(lambda: loop.create_task(drain_then_finish()))

        try:
            signal.signal(signal.SIGTERM, handler)
        except ValueError:  # only the main thread may install handlers
            return False
        return True

    def wait_snapshot(self, timeout: float = 30.0) -> None:
        if self._snapshot_thread is not None:
            self._snapshot_thread.join(timeout)

    def close(self) -> None:
        self.wait_snapshot()
        self.db.close()


def open_message_map(
    data_dir: Path, legacy_json: Path, load_legacy: Optional[Callable[[], Dict[str, Any]]] = None
) -> SQLiteMap:
    store = SQLiteMap(
        Path(data_dir) / "message_map.sqlite3", legacy_json=legacy_json, load_legacy=load_legacy
    )
    store.snapshot_on_exit()
    log.warning(
        "[STATE STORE READY] entries=%s merged_from_json=%s path=%s", len(store), store.imported, store.path
    )
    return store

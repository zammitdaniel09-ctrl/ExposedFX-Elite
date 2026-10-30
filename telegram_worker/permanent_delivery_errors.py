"""Destination errors that retrying cannot fix, and a durable record of them.

A destination that refuses this account (no text/media permission, banned,
private, admin required, or not resolvable because the account is not a
member) fails identically on every retry. Live FASTVIP delivery records such
a failure once per message and route, so the watchdog stops re-sending that
message every few seconds.

Nothing else is skipped: every NEW message still gets one real attempt, so a
route resumes on its own as soon as the destination allows the account again.
Skips are logged as NOT DELIVERED and kept in the ledger for review.
"""

import json
import os
import re
import time
from pathlib import Path


PERMANENT_ERROR_NAMES = frozenset({
    "ChatSendPlainForbiddenError",
    "ChatWriteForbiddenError",
    "ChatSendMediaForbiddenError",
    "ChatSendPhotosForbiddenError",
    "ChatSendVideosForbiddenError",
    "ChatSendDocsForbiddenError",
    "ChatSendAudiosForbiddenError",
    "ChatSendVoicesForbiddenError",
    "ChatSendRoundvideosForbiddenError",
    "ChatSendStickersForbiddenError",
    "ChatSendGifsForbiddenError",
    "ChatSendPollForbiddenError",
    "ChatSendInlineForbiddenError",
    "ChatAdminRequiredError",
    "ChatRestrictedError",
    "UserBannedInChannelError",
    "ChannelPrivateError",
})

DESTINATION_NOT_RESOLVABLE = "DestinationNotResolvable"

_MISSING_ENTITY = re.compile(
    r"Could not find the input entity for PeerChannel\(channel_id=(\d+)\)"
)


def _channel_id(chat_id):
    text = str(int(chat_id))
    if text.startswith("-100"):
        return int(text[4:])
    return abs(int(chat_id))


def permanent_error_name(exc, dest_chat):
    """Return a stable name if `exc` is a permanent destination error, else None."""
    for cls in type(exc).__mro__:
        if cls.__name__ in PERMANENT_ERROR_NAMES:
            return cls.__name__

    # Telethon raises a plain ValueError when the account cannot resolve a
    # chat. Only treat it as permanent when it names THIS route's destination,
    # never a source or reply-parent lookup.
    if isinstance(exc, ValueError) and dest_chat is not None:
        match = _MISSING_ENTITY.search(str(exc))
        try:
            if match and int(match.group(1)) == _channel_id(dest_chat):
                return DESTINATION_NOT_RESOLVABLE
        except (TypeError, ValueError):
            return None

    return None


def route_key(route):
    return (
        f"{route.get('source_chat')}_{route.get('source_topic')}"
        f"->{route.get('dest_chat')}_{route.get('dest_topic')}"
    )


class PermanentFailureLedger:
    """Per-route record of message IDs that hit a permanent destination error.

    Stored as JSON on the worker volume, written atomically, capped per route.
    It never touches message_map.json or any other worker state file.
    """

    def __init__(self, path, max_per_route=500):
        self.path = Path(path)
        self.max_per_route = max(1, int(max_per_route))
        self._data = self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            # Keep an unreadable ledger for inspection instead of overwriting it.
            try:
                self.path.replace(
                    self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
                )
            except OSError:
                pass
            return {}

    def contains_all(self, route, ids):
        entries = self._data.get(route_key(route), {})
        ids = [int(value) for value in ids if value]
        return bool(ids) and all(str(value) in entries for value in ids)

    def record(self, route, ids, error_name, now=None):
        """Remember the failure; returns False only if the file could not be saved."""
        stamp = int(now if now is not None else time.time())
        entries = self._data.setdefault(route_key(route), {})
        for value in ids:
            if value:
                entries[str(int(value))] = {"error": str(error_name), "ts": stamp}

        if len(entries) > self.max_per_route:
            newest = sorted(
                entries.items(),
                key=lambda item: (item[1].get("ts", 0), int(item[0])),
            )[-self.max_per_route:]
            self._data[route_key(route)] = dict(newest)

        return self._save()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
            return True
        except OSError:
            return False


class Throttle:
    """Allow one event per key per interval (used to rate-limit owner alerts)."""

    def __init__(self, interval_seconds):
        self.interval = float(interval_seconds)
        self._last = {}

    def ready(self, key, now=None):
        now = time.monotonic() if now is None else float(now)
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True

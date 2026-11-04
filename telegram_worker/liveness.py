"""Process liveness: restart instead of idling dead.

A long-running Telethon worker can stop working without crashing:

1. Telegram gives up reconnecting and run_until_disconnected() returns. The
   old entry points then exited with status 0, which Railway treats as a
   clean stop, so the worker stayed down.
2. The event loop stops turning (a blocking call or deadlock). Nothing is
   logged and nothing is delivered.

Both now end the process with a non-zero status so Railway restarts it, after
leaving a marker that the health monitor turns into a Saved Messages notice on
the next start ("restarted itself because ...").
"""
import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

MARKER_FILE = "liveness_exit_v1.json"
STALL_SECONDS = max(30, int(os.environ.get("LIVENESS_STALL_SECONDS", "120")))
TICK_SECONDS = 5
EXIT_DISCONNECTED = 75
EXIT_STALLED = 70


def write_marker(data_dir, service, reason):
    try:
        path = Path(data_dir) / MARKER_FILE
        path.write_text(json.dumps({"service": service, "reason": reason, "ts": time.time()}), encoding="utf-8")
    except Exception:
        pass


def take_marker(data_dir):
    """Return and clear the previous run's exit marker, if any."""
    path = Path(data_dir) / MARKER_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    try:
        path.unlink()
    except Exception:
        pass
    return value if isinstance(value, dict) else None


class LoopWatchdog:
    """A thread outside the event loop that exits the process if the loop stalls."""

    def __init__(self, service, data_dir, logger, stall_seconds=STALL_SECONDS,
                 clock=time.monotonic, exit_process=os._exit):
        self.service = service
        self.data_dir = data_dir
        self.log = logger
        self.stall_seconds = stall_seconds
        self.clock = clock
        self.exit_process = exit_process
        self.last_tick = clock()

    async def _tick(self):
        while True:
            self.last_tick = self.clock()
            await asyncio.sleep(TICK_SECONDS)

    def check(self):
        stalled = self.clock() - self.last_tick
        if stalled <= self.stall_seconds:
            return False
        self.log.critical("[LIVENESS STALL] service=%s loop_stalled=%.0fs action=exit_for_restart",
                          self.service, stalled)
        write_marker(self.data_dir, self.service, f"event loop stalled for {stalled:.0f}s")
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass
        self.exit_process(EXIT_STALLED)
        return True

    def _watch(self):
        while not self.check():
            time.sleep(TICK_SECONDS)

    def start(self):
        self.task = asyncio.get_running_loop().create_task(self._tick())
        threading.Thread(target=self._watch, name="liveness-watchdog", daemon=True).start()
        return self


def exit_disconnected(service, data_dir, logger):
    logger.critical("[LIVENESS DISCONNECTED] service=%s action=exit_for_restart", service)
    write_marker(data_dir, service, "Telegram connection was lost and did not come back")
    raise SystemExit(EXIT_DISCONNECTED)

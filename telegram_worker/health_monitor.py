"""Delivery health monitor (owner request 4 Nov 2026).

Watches this worker's own log lines - no change to any delivery path - and
tells the owner in Saved Messages when something needs attention: a route
stopped, repeated delivery failures, a long flood wait or an unexpected
traceback. Once a day it sends a short digest of deliveries per route.
Alerts are rate limited and sent from a background task, never inline.

The same file runs on MAIN and ALT; each reports to its own account.
"""

import asyncio
import json
import logging
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("health-monitor")

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Malta")
except Exception:  # slim images may ship without tz data; never fail startup
    TZ = timezone(timedelta(hours=1))
DIGEST_HOUR = 8
FAILURE_WINDOW_SECONDS = 600
FAILURE_THRESHOLD = 3
FLOOD_ALERT_SECONDS = 60
ALERT_COOLDOWN_SECONDS = 1800
SAVE_EVERY_SECONDS = 60
STATE_FILE = "health_monitor_v1.json"

SUCCESS = (
    re.compile(r"\[FASTVIP COPIED\] route=(?P<route>.+?) source=\S+ .*?(?:lag=(?P<lag>[\d.]+)s|$)"),
    re.compile(r"\[OWNER LIVE POST\] contract=(?P<route>\S+) outcome=sent"),
    re.compile(
        r"\[OWNER FORMATTED POST\] contract=(?P<route>\S+) source_id=\S+ "
        r"outcome=(?:signal|update|recap) lag=(?P<lag>[\d.]+)s"
    ),
    re.compile(r"\[WHOLECHAT SENT\] reason=\S+ source=\S+ dest=(?P<route>\S+)"),
    re.compile(r"\[ALT COPIED\] route=(?P<route>.+?) (?:source_msg|ids)=.*?(?:lag=(?P<lag>[\d.]+)s|$)"),
)
FAILURE = (
    re.compile(r"\[FASTVIP DEST FAILED\] route=(?P<route>.+?) ids=.*? dest=\S+ (?P<error>\w+)"),
    re.compile(r"\[OWNER LIVE WAIT\] contract=(?P<route>\S+) error=(?P<error>\S+)"),
    re.compile(r"\[OWNER FORMATTED LIVE WAIT\] contract=(?P<route>\S+) error=(?P<error>\S+)"),
    re.compile(r"\[(?P<route>WHOLECHAT) LIVE(?: ALBUM)? FAILED\] (?P<error>\w+)"),
    re.compile(r"\[ALT COPY FAILED\] route=(?P<route>.+?) ids=\[.*?\] (?P<error>\w+)"),
)
STOPPED = (
    re.compile(r"\[OWNER ROUTE STOPPED\] route=(?P<name>.*?) contract=(?P<route>\S+) reason=(?P<error>\S+)"),
    re.compile(r"\[OWNER FORMATTED STOPPED\] contract=(?P<route>\S+)"),
)
FLOOD = (
    re.compile(r"\[copy retry floodwait\] attempt=\S+ wait=(?P<seconds>\d+)s route=(?P<route>.+?) msg="),
    re.compile(r"Sleeping for (?P<seconds>\d+)s \(.*?\) on (?P<route>\w+) flood wait"),
    re.compile(r"\[ALT FLOODWAIT\] route=(?P<route>.+?) seconds=(?P<seconds>\d+)"),
    re.compile(r"FLOODWAIT\] label=(?P<route>\S+) attempt=\S+ wait=(?P<seconds>\d+)s"),
)
GAP_STALE = re.compile(
    r"\[GAP STALE NOT SENT\] route=(?P<route>.+?) ids=(?P<ids>\[[^\]]*\]) age=(?P<age>\w+)s"
)


class HealthMonitor:
    def __init__(self, service, data_dir, clock=time.time):
        self.service = service
        self.path = Path(data_dir) / STATE_FILE
        self.clock = clock
        self.state = self._load()
        self.state["restarts"] = int(self.state.get("restarts", 0)) + 1
        self.state.setdefault("routes", {})
        self.state.setdefault("since", self.clock())
        # First install: start counting today; the first digest is tomorrow.
        self.state.setdefault("last_digest_day", datetime.fromtimestamp(self.clock(), TZ).date().isoformat())
        self.failures = defaultdict(deque)
        self.last_alert = {}
        self.outbox = deque(maxlen=50)
        self.last_save = 0.0
        self.extras = []  # callables adding a line to the daily digest

    def _load(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
            self.last_save = self.clock()
        except Exception as exc:
            log.warning("[HEALTH MONITOR SAVE FAILED] %s", type(exc).__name__)

    def _route(self, route):
        return self.state["routes"].setdefault(route, {"ok": 0, "fail": 0, "lag_sum": 0.0, "lag_n": 0})

    def _alert(self, key, text):
        now = self.clock()
        if now - self.last_alert.get(key, 0.0) < ALERT_COOLDOWN_SECONDS:
            return
        self.last_alert[key] = now
        self.outbox.append(text)

    def observe(self, level, text, has_exc=False):
        if "[" not in text and "flood wait" not in text:
            return
        for pattern in SUCCESS:
            match = pattern.search(text)
            if match:
                route = match.group("route").strip()
                stats = self._route(route)
                stats["ok"] += 1
                if "lag" in match.groupdict() and match.group("lag"):
                    stats["lag_sum"] += float(match.group("lag"))
                    stats["lag_n"] += 1
                self.failures.pop(route, None)
                return
        for pattern in STOPPED:
            match = pattern.search(text)
            if match:
                route = match.group("route").strip()
                name = (match.groupdict().get("name") or "").strip() or route
                reason = match.groupdict().get("error") or "stopped"
                self._alert(("stop", route), f"⛔ {self.service}: route stopped\n{name}\nReason: {reason}")
                return
        for pattern in FAILURE:
            match = pattern.search(text)
            if match:
                route = match.group("route").strip()
                self._route(route)["fail"] += 1
                now = self.clock()
                recent = self.failures[route]
                recent.append(now)
                while recent and now - recent[0] > FAILURE_WINDOW_SECONDS:
                    recent.popleft()
                if len(recent) >= FAILURE_THRESHOLD:
                    self._alert(
                        ("fail", route),
                        f"⚠️ {self.service}: {route}\n"
                        f"{len(recent)} failed deliveries in 10 min ({match.group('error')})",
                    )
                return
        for pattern in FLOOD:
            match = pattern.search(text)
            if match:
                seconds = int(match.group("seconds"))
                if seconds >= FLOOD_ALERT_SECONDS:
                    route = match.group("route").strip()
                    self._alert(
                        ("flood", route), f"⏳ {self.service}: Telegram flood wait {seconds}s ({route})"
                    )
                return
        match = GAP_STALE.search(text)
        if match:
            route = match.group("route").strip()
            self._alert(
                ("gap", route, match.group("ids")),
                f"⏭️ {self.service}: {route}\nPost(s) {match.group('ids')} arrived during a restart and are "
                f"{match.group('age')}s old, so they were not sent (stale signals can mislead). "
                "Forward manually if still relevant.",
            )
            return
        if level >= logging.ERROR and has_exc:
            first = text.splitlines()[0][:200] if text else "error"
            self._alert(("error", first[:60]), f"\U0001f6a8 {self.service}: unexpected error\n{first}")

    def digest_due(self, now=None):
        local = datetime.fromtimestamp(now if now is not None else self.clock(), TZ)
        return local.hour >= DIGEST_HOUR and self.state.get("last_digest_day") != local.date().isoformat()

    def digest(self):
        routes = self.state["routes"]
        ok = sum(r["ok"] for r in routes.values())
        fail = sum(r["fail"] for r in routes.values())
        lines = [
            f"\U0001f4ca {self.service} · daily health",
            f"Delivered {ok} · Failed {fail} · Restarts {self.state.get('restarts', 0)}",
        ]
        for name, r in sorted(routes.items(), key=lambda item: -item[1]["ok"]):
            lag = f" · {r['lag_sum'] / r['lag_n']:.1f}s" if r["lag_n"] else ""
            failed = f" · {r['fail']} failed" if r["fail"] else ""
            lines.append(f"• {name}: {r['ok']}{lag}{failed}")
        if not routes:
            lines.append("No deliveries recorded.")
        for extra in self.extras:
            try:
                lines.append(str(extra()))
            except Exception:
                pass
        return "\n".join(lines)

    def reset_after_digest(self, now=None):
        local = datetime.fromtimestamp(now if now is not None else self.clock(), TZ)
        self.state.update(routes={}, restarts=0, since=self.clock(), last_digest_day=local.date().isoformat())

    async def _send(self, client, text):
        await client.send_message("me", text, parse_mode=None, link_preview=False)

    async def run(self, client):
        while True:
            await asyncio.sleep(5)
            try:
                while self.outbox:
                    text = self.outbox[0]
                    try:
                        await self._send(client, text)
                    except Exception as exc:
                        log.warning("[HEALTH ALERT NOT SENT] %s: %s", type(exc).__name__, exc)
                        break
                    self.outbox.popleft()
                if self.digest_due():
                    try:
                        await self._send(client, self.digest())
                        self.reset_after_digest()
                        self.save()
                    except Exception as exc:
                        log.warning("[HEALTH DIGEST NOT SENT] %s: %s", type(exc).__name__, exc)
                if self.clock() - self.last_save >= SAVE_EVERY_SECONDS:
                    self.save()
            except asyncio.CancelledError:
                self.save()
                raise


class _LogHandler(logging.Handler):
    def __init__(self, monitor):
        super().__init__(logging.INFO)
        self.monitor = monitor

    def emit(self, record):
        if record.name == log.name:
            return
        try:
            self.monitor.observe(record.levelno, record.getMessage(), bool(record.exc_info))
        except Exception:
            pass


def install_health_monitor(service, client, data_dir):
    from telegram_worker.liveness import take_marker

    monitor = HealthMonitor(service, data_dir)
    previous = take_marker(data_dir)
    if previous:
        monitor.outbox.append(f"♻️ {service} restarted itself\nReason: {previous.get('reason')}")
    logging.getLogger().addHandler(_LogHandler(monitor))
    monitor.task = asyncio.create_task(monitor.run(client))
    log.warning(
        "[HEALTH MONITOR READY] service=%s alerts=saved_messages digest=%02d:00 Europe/Malta",
        service,
        DIGEST_HOUR,
    )
    return monitor

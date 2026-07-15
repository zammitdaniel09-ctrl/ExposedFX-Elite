import asyncio
import json
import os
import time
from pathlib import Path

import requests


DATA_DIR = Path(os.environ.get("DATA_DIR") or "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

ALERT_BOT_TOKEN = os.environ.get("TELEGRAM_ALERT_BOT_TOKEN", "").strip()
ALERT_CHAT_ID = os.environ.get("TELEGRAM_ALERT_CHAT_ID", "").strip()

HEARTBEAT_SECONDS = int(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))
STALE_SECONDS = int(os.environ.get("WORKER_STALE_SECONDS", "180"))


def _alert_enabled():
    return bool(ALERT_BOT_TOKEN and ALERT_CHAT_ID)


def send_alert(text: str):
    if not _alert_enabled():
        return False

    try:
        requests.post(
            f"https://api.telegram.org/bot{ALERT_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": ALERT_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return True
    except Exception:
        return False


async def start_runtime_guard(service_name: str, log=None):
    """
    Local singleton/heartbeat guard.

    This prevents duplicate loops inside one container and creates heartbeat files.
    Railway can briefly overlap old and new containers during deployment, so the raw
    Telegram worker waits before connecting. This gives the old container time to stop
    before the new container uses the same Telegram authorization key.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    heartbeat_file = DATA_DIR / f"{service_name}.heartbeat.json"
    lock_file = DATA_DIR / f"{service_name}.lock"

    now = time.time()

    if lock_file.exists():
        try:
            age = now - lock_file.stat().st_mtime
            if age < STALE_SECONDS:
                msg = f"⚠️ <b>{service_name}</b> duplicate local worker detected. Exiting duplicate instance."
                if log:
                    log.error(msg)
                send_alert(msg)
                raise SystemExit(12)
        except SystemExit:
            raise
        except Exception:
            pass

    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    default_delay = "90" if service_name == "imperium-telegram-worker" else "0"
    connect_delay = max(0, int(os.environ.get("TELEGRAM_CONNECT_DELAY_SECONDS", default_delay)))
    if connect_delay:
        if log:
            log.info(
                f"Telegram deployment overlap protection active: waiting {connect_delay}s before connect"
            )
        await asyncio.sleep(connect_delay)

    async def heartbeat_loop():
        while True:
            try:
                heartbeat_file.write_text(
                    json.dumps({
                        "service": service_name,
                        "pid": os.getpid(),
                        "ts": time.time(),
                        "owner": os.environ.get("SESSION_OWNER", service_name),
                    }),
                    encoding="utf-8",
                )
                lock_file.touch()
            except Exception:
                pass

            await asyncio.sleep(HEARTBEAT_SECONDS)

    if log:
        log.info(f"Runtime guard active: service={service_name} heartbeat={HEARTBEAT_SECONDS}s")

    send_alert(f"✅ <b>{service_name}</b> started")

    return asyncio.create_task(heartbeat_loop())


def alert_crash(service_name: str, exc: Exception):
    send_alert(f"🚨 <b>{service_name}</b> crashed\n<code>{type(exc).__name__}: {exc}</code>")

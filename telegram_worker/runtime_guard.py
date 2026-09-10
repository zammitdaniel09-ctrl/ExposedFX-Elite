import asyncio
import atexit
import json
import os
import sys
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


def _lock_pid(lock_file: Path):
    try:
        return int(lock_file.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _remove_own_lock(lock_file: Path, pid: int):
    """Remove the lock only when this process still owns it."""
    try:
        if lock_file.exists() and _lock_pid(lock_file) == int(pid):
            lock_file.unlink(missing_ok=True)
    except Exception:
        pass


async def _auto_install_vip_emoji_registry(log=None):
    """
    Attach the Saved Messages custom-emoji collector to the already-running
    worker_fixed Telegram client without changing Railway's start command.

    This is intentionally restricted to the main VIP service and can be
    disabled with VIP_EMOJI_REGISTRY_ENABLED=0.
    """
    if os.environ.get("VIP_EMOJI_REGISTRY_ENABLED", "1").strip() != "1":
        if log:
            log.info("[VIP EMOJI REGISTRY] disabled")
        return

    try:
        from telegram_worker.community_emoji_registry import (
            CommunityEmojiRegistry,
            install_saved_messages_emoji_collector,
        )
    except Exception as exc:
        if log:
            log.exception(
                "[VIP EMOJI REGISTRY IMPORT FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )
        return

    while True:
        try:
            main_module = sys.modules.get("__main__")
            client = getattr(main_module, "client", None) if main_module else None

            if client is None:
                await asyncio.sleep(0.25)
                continue

            if not client.is_connected():
                await asyncio.sleep(0.25)
                continue

            if not await client.is_user_authorized():
                await asyncio.sleep(0.5)
                continue

            scan_limit = max(
                1,
                int(os.environ.get("EMOJI_REGISTRY_SCAN_LIMIT", "500")),
            )

            registry = CommunityEmojiRegistry(
                DATA_DIR,
                logger=log,
            )

            await install_saved_messages_emoji_collector(
                client,
                registry,
                logger=log,
                scan_limit=scan_limit,
            )

            if main_module is not None:
                setattr(main_module, "EMOJI_REGISTRY", registry)

            if log:
                log.warning(
                    "[VIP EMOJI REGISTRY ACTIVE] "
                    f"aliases={len(registry.entries)} "
                    f"scan_limit={scan_limit} "
                    "SOURCE=SavedMessages "
                    "START_COMMAND_CHANGE_REQUIRED=False"
                )

            return

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if log:
                log.warning(
                    "[VIP EMOJI REGISTRY WAIT/RETRY] %s: %s",
                    type(exc).__name__,
                    exc,
                )
            await asyncio.sleep(2)


async def start_runtime_guard(service_name: str, log=None):
    """
    Persistent-volume singleton/heartbeat guard.

    Railway can briefly overlap old and new containers during a deployment and
    the lock lives on the mounted DATA_DIR volume. Previous behaviour exited
    immediately while a fresh lock existed, which could put Railway into a
    crash/restart loop after an unclean shutdown.

    New behaviour waits for the existing lock to become stale instead. An
    actually-running worker refreshes its lock every HEARTBEAT_SECONDS, so the
    new container keeps waiting. A dead worker stops refreshing the lock, so
    this process automatically takes over after STALE_SECONDS.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    heartbeat_file = DATA_DIR / f"{service_name}.heartbeat.json"
    lock_file = DATA_DIR / f"{service_name}.lock"
    pid = os.getpid()

    wait_log_at = 0.0

    while lock_file.exists():
        try:
            now = time.time()
            age = max(0.0, now - lock_file.stat().st_mtime)

            if age >= STALE_SECONDS:
                if log:
                    log.warning(
                        f"Stale runtime lock recovered: service={service_name} "
                        f"age={age:.1f}s stale_after={STALE_SECONDS}s"
                    )
                break

            if log and (now - wait_log_at >= 15.0):
                remaining = max(1, int(STALE_SECONDS - age) + 1)
                log.warning(
                    f"Existing {service_name} runtime lock detected; "
                    f"waiting instead of crashing. age={age:.1f}s "
                    f"stale_after={STALE_SECONDS}s remaining~{remaining}s"
                )
                wait_log_at = now

            await asyncio.sleep(2)

        except FileNotFoundError:
            break
        except Exception as exc:
            if log:
                log.warning(
                    f"Runtime lock check failed for {service_name}: "
                    f"{type(exc).__name__}: {exc}; retrying"
                )
            await asyncio.sleep(2)

    lock_file.write_text(str(pid), encoding="utf-8")
    lock_file.touch()

    atexit.register(_remove_own_lock, lock_file, pid)

    default_delay = "90" if service_name == "imperium-telegram-worker" else "0"
    connect_delay = max(0, int(os.environ.get("TELEGRAM_CONNECT_DELAY_SECONDS", default_delay)))
    if connect_delay:
        if log:
            log.info(
                f"Telegram deployment overlap protection active: waiting {connect_delay}s before connect"
            )
        await asyncio.sleep(connect_delay)

    async def heartbeat_loop():
        try:
            while True:
                try:
                    heartbeat_file.write_text(
                        json.dumps({
                            "service": service_name,
                            "pid": pid,
                            "ts": time.time(),
                            "owner": os.environ.get("SESSION_OWNER", service_name),
                        }),
                        encoding="utf-8",
                    )
                    if _lock_pid(lock_file) == pid:
                        lock_file.touch()
                except Exception:
                    pass

                await asyncio.sleep(HEARTBEAT_SECONDS)
        finally:
            _remove_own_lock(lock_file, pid)

    if log:
        log.info(f"Runtime guard active: service={service_name} heartbeat={HEARTBEAT_SECONDS}s")

    send_alert(f"✅ <b>{service_name}</b> started")

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    # Main VIP service: install the emoji collector automatically on the
    # existing worker_fixed Telegram client. No Railway start-command change.
    if service_name == "imperium-telegram-worker":
        asyncio.create_task(_auto_install_vip_emoji_registry(log))

    return heartbeat_task


def alert_crash(service_name: str, exc: Exception):
    send_alert(f"🚨 <b>{service_name}</b> crashed\n<code>{type(exc).__name__}: {exc}</code>")

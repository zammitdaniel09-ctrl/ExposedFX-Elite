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
    """Install global reply preservation first, then VIP emoji/formatter layers.

    Reply preservation must not depend on the emoji registry. As soon as the
    Telegram user session is authorised, every main-worker route gets the
    hardened reply mapper/copy wrappers. The Imperium 508 -> VIP7 formatter is
    installed afterwards when its Saved Messages emoji registry is available.
    """
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

            # ----------------------------------------------------------
            # GLOBAL REPLY LAYER - independent of all VIP formatting.
            # ----------------------------------------------------------
            try:
                from telegram_worker.reply_hardening import install_reply_hardening

                reply_state = install_reply_hardening(
                    main_module,
                    logger=log,
                )
                if main_module is not None:
                    setattr(main_module, "GLOBAL_REPLY_HARDENING", reply_state)
                if log:
                    log.warning(
                        "[GLOBAL REPLY HARDENING READY] all_routes=True "
                        "dependency=telegram_session only emoji_registry_required=False"
                    )
            except Exception as exc:
                if log:
                    log.exception(
                        "[GLOBAL REPLY HARDENING INSTALL FAILED] %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                await asyncio.sleep(2)
                continue

            # Reply preservation stays active even if VIP formatting is disabled.
            if os.environ.get("VIP_EMOJI_REGISTRY_ENABLED", "1").strip() != "1":
                if log:
                    log.info("[VIP EMOJI REGISTRY] disabled; global reply hardening remains active")
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
                # Reply hardening is already installed. Keep retrying only the
                # optional formatter dependency.
                await asyncio.sleep(2)
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

            inline_state = None

            try:
                from telegram_worker.imperium_vip_inline_hook import (
                    install_imperium_vip_inline_hook,
                )

                inline_state = install_imperium_vip_inline_hook(
                    main_module,
                    registry,
                    logger=log,
                )

                if main_module is not None:
                    setattr(main_module, "IMPERIUM_VIP_FORMATTER", inline_state)
                    setattr(main_module, "IMPERIUM_VIP_DESTINATION_POLLER", None)

                if log:
                    log.warning(
                        "[IMPERIUM VIP PRIMARY FORMATTER ACTIVE] "
                        "source=-1004367822325_508 dest=-1003726286301_7 "
                        "writer=inline_forward_path destination_polling=False"
                    )

            except Exception as exc:
                if log:
                    log.exception(
                        "[IMPERIUM VIP INLINE HOOK INSTALL FAILED] %s: %s",
                        type(exc).__name__,
                        exc,
                    )

            # Emergency formatter fallback only. Reply hardening does not depend
            # on this path and remains installed regardless.
            if inline_state is None:
                try:
                    from telegram_worker.imperium_vip_destination_poller import (
                        install_imperium_vip_destination_poller,
                    )

                    destination_poller_state = await install_imperium_vip_destination_poller(
                        client,
                        registry,
                        logger=log,
                    )

                    if main_module is not None:
                        setattr(
                            main_module,
                            "IMPERIUM_VIP_DESTINATION_POLLER",
                            destination_poller_state,
                        )

                    if log:
                        log.warning(
                            "[IMPERIUM VIP FORMATTER FALLBACK ACTIVE] "
                            "source=-1004367822325_508 dest=-1003726286301_7 "
                            "writer=destination_poller reason=inline_hook_unavailable"
                        )

                except Exception as exc:
                    if log:
                        log.exception(
                            "[IMPERIUM VIP FORMATTER FALLBACK FAILED] %s: %s",
                            type(exc).__name__,
                            exc,
                        )

            return

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if log:
                log.warning(
                    "[VIP/REPLY INSTALL WAIT/RETRY] %s: %s",
                    type(exc).__name__,
                    exc,
                )
            await asyncio.sleep(2)


async def start_runtime_guard(service_name: str, log=None):
    """Persistent-volume singleton/heartbeat guard.

    For the main VIP worker we deliberately use a fast 5s heartbeat and a 45s
    stale threshold. 45s is long enough to coexist safely with an older
    deployment that may only heartbeat every 30s, while reducing dead-worker
    takeover from several minutes to at most ~45s.

    The heartbeat starts immediately after this process claims the lock. This
    is important: a startup/connect delay must never leave a claimed lock
    ageing without being refreshed.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    heartbeat_file = DATA_DIR / f"{service_name}.heartbeat.json"
    lock_file = DATA_DIR / f"{service_name}.lock"
    pid = os.getpid()

    is_main_vip = service_name == "imperium-telegram-worker"
    heartbeat_seconds = 5 if is_main_vip else max(2, HEARTBEAT_SECONDS)
    stale_seconds = 45 if is_main_vip else max(heartbeat_seconds * 2, STALE_SECONDS)

    wait_log_at = 0.0

    while lock_file.exists():
        try:
            now = time.time()
            age = max(0.0, now - lock_file.stat().st_mtime)

            if age >= stale_seconds:
                if log:
                    log.warning(
                        f"Stale runtime lock recovered: service={service_name} "
                        f"age={age:.1f}s stale_after={stale_seconds}s"
                    )
                break

            if log and (now - wait_log_at >= 10.0):
                remaining = max(1, int(stale_seconds - age) + 1)
                log.warning(
                    f"Existing {service_name} runtime lock detected; "
                    f"waiting instead of crashing. age={age:.1f}s "
                    f"stale_after={stale_seconds}s remaining~{remaining}s"
                )
                wait_log_at = now

            await asyncio.sleep(1)

        except FileNotFoundError:
            break
        except Exception as exc:
            if log:
                log.warning(
                    f"Runtime lock check failed for {service_name}: "
                    f"{type(exc).__name__}: {exc}; retrying"
                )
            await asyncio.sleep(1)

    lock_file.write_text(str(pid), encoding="utf-8")
    lock_file.touch()
    atexit.register(_remove_own_lock, lock_file, pid)

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

                await asyncio.sleep(heartbeat_seconds)
        finally:
            _remove_own_lock(lock_file, pid)

    heartbeat_task = asyncio.create_task(heartbeat_loop())

    if is_main_vip:
        connect_delay = 0
    else:
        connect_delay = max(
            0,
            int(os.environ.get("TELEGRAM_CONNECT_DELAY_SECONDS", "0")),
        )

    if connect_delay:
        if log:
            log.info(
                f"Telegram deployment overlap protection active: waiting {connect_delay}s before connect"
            )
        await asyncio.sleep(connect_delay)

    if log:
        log.info(
            f"Runtime guard active: service={service_name} "
            f"heartbeat={heartbeat_seconds}s stale_after={stale_seconds}s "
            f"connect_delay={connect_delay}s"
        )

    send_alert(f"✅ <b>{service_name}</b> started")

    if is_main_vip:
        asyncio.create_task(_auto_install_vip_emoji_registry(log))

    return heartbeat_task


def alert_crash(service_name: str, exc: Exception):
    send_alert(f"🚨 <b>{service_name}</b> crashed\n<code>{type(exc).__name__}: {exc}</code>")

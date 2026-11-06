"""Stable entrypoint for the ALT worker with reply preservation installed.

The historical ALT worker is kept byte-for-byte as alt_worker_legacy.py. This
wrapper lets Railway keep its existing `python -m telegram_worker.alt_worker`
start command while installing reply hardening before legacy main() begins.

Routes come from route_table.py (ALT_ROUTES_JSON is no longer read).
"""

import asyncio

from telegram_worker import alt_worker_legacy as _legacy
from telegram_worker.alt_reply_hardening import install_alt_reply_hardening
from telegram_worker.alt_new_topics7248_7250_last20 import run_alt_new_topics_last20_once

install_alt_reply_hardening(_legacy, logger=_legacy.log)


# Keep useful module attributes available for diagnostics/importers.
client = _legacy.client
ROUTES = _legacy.ROUTES
SOURCE_CHATS = _legacy.SOURCE_CHATS


async def _run_new_topics_last20_guarded():
    try:
        await run_alt_new_topics_last20_once(_legacy, logger=_legacy.log)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _legacy.log.exception(
            "[ALT NEWTOPICS LAST20 FAILED SAFE] "
            f"{type(exc).__name__}: {exc} "
            "LIVE_FORWARDING_CONTINUES=True"
        )


async def main():
    from telegram_worker import liveness
    from telegram_worker.health_monitor import install_health_monitor

    liveness.LoopWatchdog("ExposedFX ALT", _legacy.DATA_DIR, _legacy.log).start()
    liveness.start_heartbeat("ExposedFX ALT", _legacy.client, _legacy.log)
    _legacy.log.warning(
        "[ALT RUNTIME] telethon=%s python=%s liveness=True json_logs=True",
        __import__("telethon").__version__,
        __import__("sys").version.split()[0],
    )
    install_health_monitor("ExposedFX ALT", _legacy.client, _legacy.DATA_DIR)
    from telegram_worker.engine.alt import install_alt_engine, run_alt_replay_audit

    engine = install_alt_engine(_legacy)
    engine.audit_task = asyncio.create_task(run_alt_replay_audit(_legacy, engine))
    new_topics_last20_task = asyncio.create_task(_run_new_topics_last20_guarded())
    from telegram_worker.engine.topic_reset import reset_when_ready

    # Owner request 6 Nov 2026: relay topic 8323 starts on a fresh slate (runs once).
    reset_task = asyncio.create_task(reset_when_ready(_legacy.client, -1004367822325, 8323, _legacy.DATA_DIR))
    try:
        await _legacy.main()
        liveness.exit_disconnected("ExposedFX ALT", _legacy.DATA_DIR, _legacy.log)
    finally:
        for task in (new_topics_last20_task, reset_task):
            if not task.done():
                task.cancel()


if __name__ == "__main__":
    asyncio.run(main())

"""Telegram worker package bootstrap.

The package keeps normal behaviour for every service. For the main
imperium-telegram-worker only, it schedules the owner's one-time 82617
last-50 backfill after global reply hardening is live. The backfill itself has
persistent progress/state and therefore cannot repeat after completion.
"""

import asyncio
import sys

from . import runtime_guard as _runtime_guard


if not getattr(_runtime_guard, "_FORCE_82617_WRAPPED", False):
    _original_start_runtime_guard = _runtime_guard.start_runtime_guard

    async def _wait_and_run_82617_last50(log=None):
        while True:
            try:
                main_module = sys.modules.get("__main__")
                client = getattr(main_module, "client", None) if main_module else None
                reply_ready = getattr(main_module, "GLOBAL_REPLY_HARDENING", None) if main_module else None

                if (
                    main_module is not None
                    and client is not None
                    and reply_ready is not None
                    and client.is_connected()
                    and await client.is_user_authorized()
                ):
                    if getattr(main_module, "FORCE_82617_LAST50_TASK", None) is None:
                        from .force_82617_last50 import run_force_82617_last50

                        task = asyncio.create_task(
                            run_force_82617_last50(main_module, logger=log)
                        )
                        setattr(main_module, "FORCE_82617_LAST50_TASK", task)
                        if log:
                            log.warning(
                                "[82617 LAST50 TASK READY] "
                                "source=-1002385852838_62159 "
                                "dest=-1003918958200_82617 "
                                "count=50 force_nonempty=True "
                                "skip_already_mapped=True preserve_copy_pipeline=True"
                            )
                    return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if log:
                    log.warning(
                        "[82617 LAST50 WAIT/RETRY] %s: %s",
                        type(exc).__name__,
                        exc,
                    )

            await asyncio.sleep(0.5)

    async def _wrapped_start_runtime_guard(service_name: str, log=None):
        task = await _original_start_runtime_guard(service_name, log)
        if service_name == "imperium-telegram-worker":
            asyncio.create_task(_wait_and_run_82617_last50(log))
        return task

    _runtime_guard.start_runtime_guard = _wrapped_start_runtime_guard
    _runtime_guard._FORCE_82617_WRAPPED = True

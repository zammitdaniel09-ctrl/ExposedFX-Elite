"""Telegram worker package bootstrap.

For the main imperium-telegram-worker this schedules owner-requested one-time
bootstrap jobs and live sidecars after the Telegram session and global reply
hardening are ready.
"""

import asyncio
import sys

from . import runtime_guard as _runtime_guard


if not getattr(_runtime_guard, "_FORCE_82617_WRAPPED", False):
    _original_start_runtime_guard = _runtime_guard.start_runtime_guard

    async def _run_wholechat_relay_with_retry(main_module, log=None):
        while True:
            try:
                from .wholechat_relay_4281603170_to_3743381585 import (
                    run_wholechat_4281603170_to_3743381585,
                )

                await run_wholechat_4281603170_to_3743381585(
                    main_module,
                    logger=log,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if log:
                    log.exception(
                        "[WHOLECHAT RELAY WAIT/RETRY] %s: %s",
                        type(exc).__name__,
                        exc,
                    )
                await asyncio.sleep(10)

    async def _wait_and_run_owner_tasks(log=None):
        while True:
            try:
                main_module = sys.modules.get("__main__")
                client = (
                    getattr(main_module, "client", None)
                    if main_module
                    else None
                )
                reply_ready = (
                    getattr(main_module, "GLOBAL_REPLY_HARDENING", None)
                    if main_module
                    else None
                )

                if (
                    main_module is not None
                    and client is not None
                    and reply_ready is not None
                    and client.is_connected()
                    and await client.is_user_authorized()
                ):
                    if getattr(
                        main_module,
                        "FORCE_82617_LAST50_TASK",
                        None,
                    ) is None:
                        from .force_82617_last50 import run_force_82617_last50

                        task = asyncio.create_task(
                            run_force_82617_last50(
                                main_module,
                                logger=log,
                            )
                        )
                        setattr(
                            main_module,
                            "FORCE_82617_LAST50_TASK",
                            task,
                        )
                        if log:
                            log.warning(
                                "[82617 LAST50 TASK READY] "
                                "source=-1002385852838_62159 "
                                "dest=-1003918958200_82617 "
                                "count=50 force_nonempty=True "
                                "skip_already_mapped=True "
                                "preserve_copy_pipeline=True"
                            )

                    if getattr(
                        main_module,
                        "WHOLECHAT_4281603170_TO_3743381585_TASK",
                        None,
                    ) is None:
                        relay_task = asyncio.create_task(
                            _run_wholechat_relay_with_retry(
                                main_module,
                                log,
                            )
                        )
                        setattr(
                            main_module,
                            "WHOLECHAT_4281603170_TO_3743381585_TASK",
                            relay_task,
                        )
                        if log:
                            log.warning(
                                "[WHOLECHAT RELAY TASK READY] "
                                "source=-1004281603170 "
                                "dest=-1003743381585 "
                                "last50=True live=True "
                                "whole_source=True whole_destination=True"
                            )

                    return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if log:
                    log.warning(
                        "[OWNER TASK WAIT/RETRY] %s: %s",
                        type(exc).__name__,
                        exc,
                    )

            await asyncio.sleep(0.5)

    async def _wrapped_start_runtime_guard(service_name: str, log=None):
        task = await _original_start_runtime_guard(service_name, log)
        if service_name == "imperium-telegram-worker":
            asyncio.create_task(_wait_and_run_owner_tasks(log))
        return task

    _runtime_guard.start_runtime_guard = _wrapped_start_runtime_guard
    _runtime_guard._FORCE_82617_WRAPPED = True

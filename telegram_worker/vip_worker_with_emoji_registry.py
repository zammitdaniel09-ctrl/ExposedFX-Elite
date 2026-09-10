import asyncio
import os

from telegram_worker import worker_fixed
from telegram_worker.community_emoji_registry import (
    CommunityEmojiRegistry,
    install_saved_messages_emoji_collector,
)


async def _install_emoji_registry_when_ready():
    """Attach the Saved Messages emoji collector to the existing VIP client.

    This deliberately reuses worker_fixed.client so there is only one Telegram
    user session/connection responsible for the VIP forwarder and the emoji
    registry.
    """
    while True:
        try:
            if worker_fixed.client.is_connected():
                if await worker_fixed.client.is_user_authorized():
                    break
        except Exception:
            pass

        await asyncio.sleep(0.25)

    scan_limit = max(
        1,
        int(os.environ.get("EMOJI_REGISTRY_SCAN_LIMIT", "500")),
    )

    registry = CommunityEmojiRegistry(
        worker_fixed.DATA_DIR,
        logger=worker_fixed.log,
    )

    await install_saved_messages_emoji_collector(
        worker_fixed.client,
        registry,
        logger=worker_fixed.log,
        scan_limit=scan_limit,
    )

    # Expose it on worker_fixed for later VIP/community formatter code.
    worker_fixed.EMOJI_REGISTRY = registry

    worker_fixed.log.warning(
        "[VIP EMOJI REGISTRY ACTIVE] "
        f"aliases={len(registry.entries)} "
        f"scan_limit={scan_limit} "
        "SOURCE=SavedMessages"
    )

    # Keep this task alive for the lifetime of the main worker. The event
    # handler itself is registered on worker_fixed.client.
    await asyncio.Event().wait()


async def main():
    collector_task = asyncio.create_task(
        _install_emoji_registry_when_ready()
    )

    try:
        await worker_fixed.main()
    finally:
        collector_task.cancel()
        try:
            await collector_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        worker_fixed.log.exception(
            "[vip worker with emoji registry fatal] "
            f"{type(exc).__name__}: {exc}"
        )
        worker_fixed.alert_crash(
            "imperium-telegram-worker-with-emoji-registry:fatal",
            exc,
        )
        raise

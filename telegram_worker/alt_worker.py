"""Stable entrypoint for the ALT worker with reply preservation installed.

The historical ALT worker is kept byte-for-byte as alt_worker_legacy.py.  This
wrapper lets Railway keep its existing `python -m telegram_worker.alt_worker`
start command while installing reply hardening before legacy main() begins.
"""

import asyncio

from telegram_worker import alt_worker_legacy as _legacy
from telegram_worker.alt_reply_hardening import install_alt_reply_hardening


install_alt_reply_hardening(_legacy, logger=_legacy.log)

# Keep useful module attributes available for diagnostics/importers.
client = _legacy.client
ROUTES = _legacy.ROUTES
SOURCE_CHATS = _legacy.SOURCE_CHATS


async def main():
    return await _legacy.main()


if __name__ == "__main__":
    asyncio.run(main())

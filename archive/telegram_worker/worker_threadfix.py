# Backward-compatible Railway start command wrapper.
# If Railway still starts `python -m telegram_worker.worker_threadfix`,
# run the stable provider forwarder instead.

import os

os.environ["DISABLE_PROVIDER_ROUTES"] = "0"

from telegram_worker.worker_fixed import main


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

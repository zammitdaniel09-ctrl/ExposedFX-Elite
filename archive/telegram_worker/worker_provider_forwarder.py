import os

# This service must always load provider routes, even if the AI formatter service
# uses DISABLE_PROVIDER_ROUTES=1. Set this before importing worker_fixed/routes.
os.environ["DISABLE_PROVIDER_ROUTES"] = "0"

from telegram_worker.worker_fixed import main


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

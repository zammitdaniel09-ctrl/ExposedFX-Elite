import asyncio
import logging

from telethon import events

from telegram_worker.worker_fixed import client, stats, SOURCE_CHATS
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command

log = logging.getLogger("imperium-worker-admin")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats")
    log.info("Imperium worker with Saved Messages admin controls running...")

    await admin_startup(client)
    asyncio.create_task(stats.loop(client))
    asyncio.create_task(admin_loop(client, stats))

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

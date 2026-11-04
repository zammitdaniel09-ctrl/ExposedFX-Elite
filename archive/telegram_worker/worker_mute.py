import asyncio
import logging
import os

from telethon import events

from telegram_worker.worker_fixed import client, stats, SOURCE_CHATS
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command

log = logging.getLogger("imperium-worker-mute")

MUTED_SENDER_IDS = {
    int(x.strip())
    for x in os.environ.get("MUTED_SENDER_IDS", "7556281143").split(",")
    if x.strip()
}


def get_sender_id(event):
    sender_id = getattr(event, "sender_id", None)
    if sender_id:
        return int(sender_id)
    msg = getattr(event, "message", None)
    if msg is None:
        messages = getattr(event, "messages", None) or []
        msg = messages[0] if messages else None
    sender_id = getattr(msg, "sender_id", None) if msg is not None else None
    return int(sender_id) if sender_id else None


def install_sender_mute():
    items = []
    for item in list(getattr(client, "_event_builders", [])):
        if not isinstance(item, tuple) or len(item) != 2:
            items.append(item)
            continue
        builder, callback = item
        async def wrapper(event, _callback=callback):
            sender_id = get_sender_id(event)
            if sender_id in MUTED_SENDER_IDS:
                log.info(f"[muted sender] sender_id={sender_id} skipped")
                return
            return await _callback(event)
        items.append((builder, wrapper))
    client._event_builders = items
    log.info(f"Sender mute active: {sorted(MUTED_SENDER_IDS)}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    install_sender_mute()
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Telegram login file loaded but account is not authorised. Regenerate the local session and Railway chunks.")
    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Watching {len(SOURCE_CHATS)} source chats")
    await admin_startup(client)
    asyncio.create_task(stats.loop(client))
    asyncio.create_task(admin_loop(client, stats))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

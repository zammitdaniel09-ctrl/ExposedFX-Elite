import asyncio
import logging
import os
import re

from telethon import events

from telegram_worker.worker_fixed import client, stats, SOURCE_CHATS
from telegram_worker.admin_features import ADMIN_CHAT, admin_startup, admin_loop, handle_admin_command

log = logging.getLogger("imperium-worker-clean")

BLOCKED_IDS = {
    int(x.strip())
    for x in os.environ.get("BLOCKED_IDS", "7556281143").split(",")
    if x.strip()
}

ONLY_LINK_PATTERN = re.compile(r"^(?:https?://|t\.me/|www\.)\S+$", re.IGNORECASE)


def peer_ids(peer):
    found = set()
    if peer is None:
        return found
    for attr in ("user_id", "channel_id", "chat_id"):
        value = getattr(peer, attr, None)
        if value:
            found.add(int(value))
    return found


def source_ids(event):
    found = set()
    direct = getattr(event, "sender_id", None)
    if direct:
        found.add(int(direct))

    msg = getattr(event, "message", None)
    if msg is None:
        msgs = getattr(event, "messages", None) or []
        msg = msgs[0] if msgs else None

    if msg is not None:
        direct = getattr(msg, "sender_id", None)
        if direct:
            found.add(int(direct))
        found |= peer_ids(getattr(msg, "from_id", None))
        fwd = getattr(msg, "fwd_from", None)
        if fwd is not None:
            found |= peer_ids(getattr(fwd, "from_id", None))
            found |= peer_ids(getattr(fwd, "saved_from_peer", None))
    return found


def raw_text(event):
    msg = getattr(event, "message", None)
    if msg is None:
        msgs = getattr(event, "messages", None) or []
        msg = msgs[0] if msgs else None
    if msg is None:
        return ""
    return getattr(msg, "message", None) or getattr(msg, "raw_text", None) or getattr(msg, "text", None) or ""


def blocked(event):
    ids = source_ids(event)
    if ids & BLOCKED_IDS:
        log.info(f"[blocked account] ids={sorted(ids)}")
        return True

    text = raw_text(event).strip()
    if ONLY_LINK_PATTERN.match(text):
        log.info("[plain link removed]")
        return True

    return False


def install_guard():
    output = []
    for item in list(getattr(client, "_event_builders", [])):
        if not isinstance(item, tuple) or len(item) != 2:
            output.append(item)
            continue
        builder, callback = item
        async def guarded(event, _callback=callback):
            if blocked(event):
                return
            return await _callback(event)
        output.append((builder, guarded))
    client._event_builders = output
    log.info(f"Clean guard active. Blocked IDs: {sorted(BLOCKED_IDS)}")


@client.on(events.NewMessage(chats=ADMIN_CHAT))
async def on_admin_message(event):
    try:
        await handle_admin_command(event, client, stats)
    except Exception as exc:
        log.error(f"[admin command failed] {exc}")


async def main():
    install_guard()
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

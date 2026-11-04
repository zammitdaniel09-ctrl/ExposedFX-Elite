# telegram_bridge/qr_login.py
# Optional helper if you need to create a new Telegram session via QR.

import os
import asyncio
import getpass
from pathlib import Path

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_PATH = str(DATA_DIR / "session")


async def main():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already logged in as {me.first_name} | id={me.id}")
        await client.disconnect()
        return

    qr_login = await client.qr_login()

    print("=" * 60)
    print("SCAN THIS QR WITH TELEGRAM:")
    print("Telegram iPhone -> Settings -> Devices -> Link Desktop Device")
    print("=" * 60)

    qr = qrcode.QRCode()
    qr.add_data(qr_login.url)
    qr.print_ascii(invert=True)

    print("Waiting for QR scan...")

    try:
        await qr_login.wait(timeout=120)
    except SessionPasswordNeededError:
        password = getpass.getpass("Telegram 2FA password: ")
        await client.sign_in(password=password)

    me = await client.get_me()
    print("LOGIN SUCCESSFUL")
    print(f"Logged in as: {me.first_name}")
    print(f"User ID: {me.id}")
    print(f"Session saved to: {DATA_DIR / 'session.session'}")

    await client.disconnect()


asyncio.run(main())

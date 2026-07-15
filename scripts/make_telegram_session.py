import asyncio
import base64
import getpass
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError


async def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python scripts/make_telegram_session.py <name> [output_dir]")

    name = sys.argv[1].strip().replace(" ", "_")
    out_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path("telegram_sessions")
    out_dir.mkdir(parents=True, exist_ok=True)

    api_id_raw = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()

    if not api_id_raw or not api_hash:
        raise SystemExit("API_ID/API_HASH missing. Run this through `railway run --service ... -- python ...`.")

    api_id = int(api_id_raw)
    session_base = out_dir / f"fresh_{name}"
    session_file = Path(str(session_base) + ".session")

    for p in [session_file, Path(str(session_base) + ".session-journal")]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    print("=" * 70)
    print(f"Creating NEW Telegram session for: {name}")
    print("Use the SAME Telegram account that has access to the groups.")
    print("Phone format example: +35699123456")
    print("=" * 70)

    client = TelegramClient(str(session_base), api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        phone = input("Telegram phone number: ").strip()
        if not phone:
            raise SystemExit("No phone entered.")

        await client.send_code_request(phone)
        code = input("Telegram login code: ").strip().replace(" ", "")

        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)

    me = await client.get_me()
    print(f"Logged in as: {getattr(me, 'first_name', '')} | id={getattr(me, 'id', '')}")

    await client.disconnect()

    if not session_file.exists():
        raise SystemExit(f"Session file not created: {session_file}")

    raw = session_file.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    b64_file = out_dir / f"{name}.b64"
    b64_file.write_text(b64, encoding="ascii")

    print(f"SESSION_FILE={session_file}")
    print(f"SESSION_B64_FILE={b64_file}")
    print(f"SESSION_B64_LENGTH={len(b64)}")
    print("SESSION_CREATED_OK")


if __name__ == "__main__":
    asyncio.run(main())

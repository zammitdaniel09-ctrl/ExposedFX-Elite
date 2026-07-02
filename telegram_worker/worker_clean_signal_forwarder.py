from telegram_worker.runtime_guard import start_runtime_guard, alert_crash
import asyncio
import base64
import json
import logging
import os
import re
from pathlib import Path

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clean-signal-forwarder")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SOURCE_CHAT = int(os.environ.get("CLEAN_FORWARD_SOURCE_CHAT", "-5252460120"))
DEST_CHAT = int(os.environ.get("CLEAN_FORWARD_DEST_CHAT", "-5144279180"))

# 1 = send as clean copied message, 0 = Telegram forward
# I recommend 1 so the new group only sees clean signals, no forwarded/source mess.
COPY_MODE = True  # forced anonymous copy mode; never Telegram-forward to final group
STRICT_ONLY_AI_FORMAT = os.environ.get("STRICT_ONLY_AI_FORMAT", "1").strip() == "1"
FORWARD_CLEAN_UPDATES = os.environ.get("FORWARD_CLEAN_UPDATES", "0").strip() == "1"
CLEAN_SEND_RETRY_ATTEMPTS = int(os.environ.get("CLEAN_SEND_RETRY_ATTEMPTS", "2"))
CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS = int(os.environ.get("CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS", "60"))

DATA_DIR = Path(os.environ.get("DATA_DIR") or "./data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SESSION_BASE = DATA_DIR / "clean_forwarder_session"
SESSION_FILE = DATA_DIR / "clean_forwarder_session.session"
CLEAN_MESSAGE_MAP_FILE = DATA_DIR / "clean_forwarder_message_map.json"



def load_message_map():
    if not CLEAN_MESSAGE_MAP_FILE.exists():
        return {}
    try:
        return json.loads(CLEAN_MESSAGE_MAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_message_map():
    try:
        tmp = CLEAN_MESSAGE_MAP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean_message_map), encoding="utf-8")
        tmp.replace(CLEAN_MESSAGE_MAP_FILE)
    except Exception as exc:
        log.warning(f"[map save failed] {exc}")


def map_key(source_msg_id):
    return str(int(source_msg_id))


def remember_clean_copy(source_msg, dest_msg):
    if not source_msg or not dest_msg:
        return

    src_id = getattr(source_msg, "id", None)
    dst_id = getattr(dest_msg, "id", None)

    if src_id and dst_id:
        clean_message_map[map_key(src_id)] = int(dst_id)
        save_message_map()
        log.info(f"[mapped clean copy] source_msg={src_id} -> dest_msg={dst_id}")


async def delete_clean_copy_by_source_id(source_msg_id):
    key = map_key(source_msg_id)
    dst_id = clean_message_map.get(key)

    if not dst_id:
        return False

    try:
        await client.delete_messages(DEST_CHAT, int(dst_id))
        clean_message_map.pop(key, None)
        save_message_map()
        log.info(f"[deleted clean copy] source_msg={source_msg_id} dest_msg={dst_id}")
        return True
    except Exception as exc:
        log.warning(f"[delete clean copy failed] source_msg={source_msg_id} dest_msg={dst_id}: {exc}")
        return False



def clean_b64(value: str) -> str:
    return "".join((value or "").split()).strip()


def combined_session_blob() -> str:
    direct = clean_b64(os.environ.get("SESSION_B64", ""))
    if direct:
        return direct

    count_raw = os.environ.get("SESSION_B64_CHUNKS", "").strip()
    if count_raw:
        count = int(count_raw)
        chunks = []
        for i in range(1, count + 1):
            chunk = clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
            if not chunk:
                raise RuntimeError(f"SESSION_B64_CHUNKS={count}, but SESSION_B64_{i} is missing")
            chunks.append(chunk)
        return "".join(chunks)

    chunks = []
    i = 1
    while True:
        chunk = clean_b64(os.environ.get(f"SESSION_B64_{i}", ""))
        if not chunk:
            break
        chunks.append(chunk)
        i += 1

    if chunks:
        return "".join(chunks)

    raise RuntimeError("No SESSION_B64 / SESSION_B64_1 variables found")


def write_session_file():
    raw = base64.b64decode(combined_session_blob())
    SESSION_FILE.write_bytes(raw)
    log.info(f"Session written: {SESSION_FILE} bytes={len(raw)}")


write_session_file()
client = TelegramClient(str(SESSION_BASE), API_ID, API_HASH)
clean_message_map = load_message_map()


HEADER_RE = re.compile(
    r"^(?:\s|[^\w])*"
    r"(BUY|SELL)\s+([A-Z0-9/ ]{3,30})\s+(?:INTRADAY\s+)?ZONE\b",
    re.IGNORECASE,
)


def text_of(message) -> str:
    return message.message or message.raw_text or message.text or ""


def is_my_formatted_signal(text: str) -> bool:
    """
    FINAL GROUP RULE:
    Forward only completed AI formatted signal templates.

    Supports:
    - BUY/SELL normal zone templates
    - BUY STOP / SELL STOP breakout templates
    """
    t = (text or "").strip()
    low = t.lower()

    if not t:
        return False

    blocked = [
        "source:",
        "exposedfx |",
        "forwarded from",
        "partial",
        "take further",
        "take a partial",
        "running trade",
        "remainder should run",
        "set stop loss",
        "move stop loss",
        "move sl",
        "click close",
        "edit the lot size",
        "ended with",
        "weekly recap",
        "daily recap",
        "results",
        "profit today",
        "instagram",
        "free life-time vip",
        "daily overview",
        "market overview",
    ]

    if any(x in low for x in blocked):
        return False

    is_breakout = bool(re.search(
        r"\b(BUY|SELL)\s+STOP\s+[A-Z0-9/ ]{3,30}\s+BREAKOUT\s+ZONE\b",
        t,
        re.IGNORECASE,
    ))

    is_normal = bool(HEADER_RE.search(t))

    if not is_breakout and not is_normal:
        return False

    common_required = [
        r"stop\s+loss\s*:\s*[-+]?\d",
        r"(?:📌\s*)?tp1\s*-\s*(?:[-+]?\d|open)",
        r"risk\s*:\s*(?:low|medium|high)",
        r"tips\s*:",
        r"this\s+is\s+not\s+financial\s+advice",
    ]

    if not all(re.search(pattern, t, re.IGNORECASE) for pattern in common_required):
        return False

    if is_breakout:
        breakout_required = [
            r"trigger\s+(?:above|below)\s*:\s*[-+]?\d",
            r"(?:buy|sell)\s+stop\s+entry\s*:\s*[-+]?\d",
        ]
        return all(re.search(pattern, t, re.IGNORECASE) for pattern in breakout_required)

    normal_required = [
        r"(?:buy|sell)\s+point\s*:\s*[-+]?\d",
        r"layer\s+point\s*:\s*[-+]?\d",
    ]

    return all(re.search(pattern, t, re.IGNORECASE) for pattern in normal_required)



def is_clean_signal_update(text: str) -> bool:
    t = (text or "").strip()
    u = t.upper()

    if not t:
        return False

    if "SOURCE:" in u or "EXPOSEDFX |" in u:
        return False

    has_pips = bool(re.search(r"\+\s*\d{1,5}(?:\.\d+)?\s*PIPS\b", u))
    has_tp_hit = bool(re.search(r"\bTP\s*#?\s*\d{1,2}\s+HIT\b", u))
    has_be = bool(re.search(r"\b(?:BE|BREAK\s*EVEN|BREAKEVEN)\s+HIT\b", u))
    has_sl_move = bool(re.search(r"\b(?:SL|STOP\s*LOSS)\s+(?:TO|MOVED\s+TO)\s+(?:BE|BREAK\s*EVEN|[-+]?\d)", u))

    return has_pips or has_tp_hit or has_be or has_sl_move



def clean_reply_target(message):
    ids = []

    direct = getattr(message, "reply_to_msg_id", None)
    if direct:
        ids.append(direct)

    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_msg_id", "reply_to_top_id", "top_msg_id"):
            value = getattr(reply, attr, None)
            if value and value not in ids:
                ids.append(value)

    for source_id in ids:
        key = map_key(source_id)
        mapped = clean_message_map.get(key)
        if mapped:
            try:
                return int(mapped)
            except Exception:
                pass

    return None


def is_transient_clean_error(exc):
    text = str(exc).lower()
    patterns = (
        "timeout",
        "timed out",
        "connection",
        "server disconnected",
        "temporarily",
        "transport",
        "network",
        "request failed",
    )
    return any(p in text for p in patterns)


async def clean_send_message_with_retry(*args, **kwargs):
    attempts = max(1, CLEAN_SEND_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await client.send_message(*args, **kwargs)
        except FloodWaitError as exc:
            last_exc = exc
            wait = min(int(exc.seconds) + 1, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
            log.warning(f"[clean send retry floodwait] attempt={attempt}/{attempts} wait={wait}s")
            if attempt < attempts:
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and is_transient_clean_error(exc):
                wait = min(2 * attempt, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
                log.warning(f"[clean send retry transient] attempt={attempt}/{attempts} wait={wait}s error={exc}")
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc
    return None


async def clean_send_file_with_retry(*args, **kwargs):
    attempts = max(1, CLEAN_SEND_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await client.send_file(*args, **kwargs)
        except FloodWaitError as exc:
            last_exc = exc
            wait = min(int(exc.seconds) + 1, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
            log.warning(f"[clean media retry floodwait] attempt={attempt}/{attempts} wait={wait}s")
            if attempt < attempts:
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and is_transient_clean_error(exc):
                wait = min(2 * attempt, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
                log.warning(f"[clean media retry transient] attempt={attempt}/{attempts} wait={wait}s error={exc}")
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc
    return None


async def clean_forward_with_retry(dest_chat, message):
    attempts = max(1, CLEAN_SEND_RETRY_ATTEMPTS)
    last_exc = None

    for attempt in range(1, attempts + 1):
        try:
            return await client.forward_messages(dest_chat, message)
        except FloodWaitError as exc:
            last_exc = exc
            wait = min(int(exc.seconds) + 1, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
            log.warning(f"[clean forward retry floodwait] attempt={attempt}/{attempts} wait={wait}s msg={getattr(message, 'id', None)}")
            if attempt < attempts:
                await asyncio.sleep(wait)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < attempts and is_transient_clean_error(exc):
                wait = min(2 * attempt, CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS)
                log.warning(f"[clean forward retry transient] attempt={attempt}/{attempts} wait={wait}s msg={getattr(message, 'id', None)} error={exc}")
                await asyncio.sleep(wait)
                continue
            raise

    if last_exc:
        raise last_exc
    return None


async def delete_existing_clean_copy_for_edit(source_msg_id):
    key = map_key(source_msg_id)
    dst_id = clean_message_map.get(key)

    if not dst_id:
        return False

    try:
        await client.delete_messages(DEST_CHAT, int(dst_id))
        clean_message_map.pop(key, None)
        save_message_map()
        log.info(f"[clean edit cleanup] source_msg={source_msg_id} deleted_old_dest={dst_id}")
        return True
    except Exception as exc:
        log.warning(f"[clean edit cleanup failed] source_msg={source_msg_id} dest_msg={dst_id}: {exc}")
        return False


async def copy_clean_signal_message(message, edited=False):
    text = text_of(message)

    sent = None

    if COPY_MODE:
        if getattr(message, "media", None):
            sent = await clean_send_file_with_retry(
                DEST_CHAT,
                message.media,
                caption=text,
                formatting_entities=getattr(message, "entities", None),
                parse_mode=None,
            )
            log.info(f"[COPIED MEDIA SIGNAL{' EDIT' if edited else ''}] msg={message.id} -> {DEST_CHAT}")
        else:
            sent = await clean_send_message_with_retry(
                DEST_CHAT,
                text,
                formatting_entities=getattr(message, "entities", None),
                parse_mode=None,
                link_preview=False,
            )
            log.info(f"[COPIED SIGNAL{' EDIT' if edited else ''}] msg={message.id} -> {DEST_CHAT}")
    else:
        sent = await clean_forward_with_retry(DEST_CHAT, message)
        log.info(f"[FORWARDED SIGNAL{' EDIT' if edited else ''}] msg={message.id} -> {DEST_CHAT}")

    if not sent:
        log.warning(f"[CLEAN SIGNAL SEND RETURNED NONE] msg={message.id}")
        return None

    remember_clean_copy(message, sent)
    return sent


async def copy_clean_update_message(message, edited=False):
    text = text_of(message)
    reply_to = clean_reply_target(message)

    if not reply_to:
        log.info(f"[SKIP UPDATE NO MAP{' EDIT' if edited else ''}] msg={message.id} text={text[:80]!r}")
        return None

    sent = await clean_send_message_with_retry(
        DEST_CHAT,
        text,
        formatting_entities=getattr(message, "entities", None),
        parse_mode=None,
        link_preview=False,
        reply_to=reply_to,
    )

    if not sent:
        log.warning(f"[CLEAN UPDATE SEND RETURNED NONE] msg={message.id}")
        return None

    remember_clean_copy(message, sent)
    log.info(f"[COPIED SIGNAL UPDATE{' EDIT' if edited else ''}] msg={message.id} -> {DEST_CHAT} reply_to={reply_to}")
    return sent


@client.on(events.NewMessage(chats=SOURCE_CHAT))
async def on_message(event):
    try:
        message = event.message
        text = text_of(message)
        key = map_key(message.id)

        if is_clean_signal_update(text):
            if not FORWARD_CLEAN_UPDATES:
                log.info(f"[SKIP UPDATE DISABLED] msg={message.id} text={text[:80]!r}")
                return

            reply_to = clean_reply_target(message)
            if not reply_to:
                log.info(f"[SKIP UPDATE NO MAP] msg={message.id} text={text[:80]!r}")
                return

            if clean_message_map.get(key):
                log.info(f"[SKIP UPDATE DUPLICATE] msg={message.id} mapped={clean_message_map.get(key)}")
                return

            sent = await clean_send_message_with_retry(
                DEST_CHAT,
                text,
                formatting_entities=getattr(message, "entities", None),
                parse_mode=None,
                link_preview=False,
                reply_to=reply_to,
            )

            if not sent:
                log.warning(f"[CLEAN UPDATE SEND RETURNED NONE] msg={message.id}")
                return

            remember_clean_copy(message, sent)
            log.info(f"[COPIED SIGNAL UPDATE] msg={message.id} -> {DEST_CHAT} reply_to={reply_to}")
            return

        if not is_my_formatted_signal(text):
            log.info(f"[SKIP] msg={message.id} text={text[:80]!r}")
            return

        if clean_message_map.get(key):
            log.info(f"[SKIP DUPLICATE SIGNAL] msg={message.id} mapped={clean_message_map.get(key)}")
            return

        sent = None

        if COPY_MODE:
            if getattr(message, "media", None):
                sent = await clean_send_file_with_retry(
                    DEST_CHAT,
                    message.media,
                    caption=text,
                    formatting_entities=getattr(message, "entities", None),
                    parse_mode=None,
                )
                log.info(f"[COPIED MEDIA SIGNAL] msg={message.id} -> {DEST_CHAT}")
            else:
                sent = await clean_send_message_with_retry(
                    DEST_CHAT,
                    text,
                    formatting_entities=getattr(message, "entities", None),
                    parse_mode=None,
                    link_preview=False,
                )
                log.info(f"[COPIED SIGNAL] msg={message.id} -> {DEST_CHAT}")
        else:
            sent = await clean_forward_with_retry(DEST_CHAT, message)
            log.info(f"[FORWARDED SIGNAL] msg={message.id} -> {DEST_CHAT}")

        if not sent:
            log.warning(f"[CLEAN SIGNAL SEND RETURNED NONE] msg={message.id}")
            return

        remember_clean_copy(message, sent)

    except Exception as exc:
        log.exception(f"[CLEAN HANDLER FAILED] msg={getattr(getattr(event, 'message', None), 'id', None)}: {exc}")
        alert_crash("exposedfx-clean-signal-forwarder:on_message", exc)


@client.on(events.MessageEdited(chats=SOURCE_CHAT))
async def on_message_edited(event):
    try:
        message = event.message
        text = text_of(message)
        key = map_key(message.id)
        had_existing = bool(clean_message_map.get(key))

        # If this edited message already exists in final group, remove old copy first.
        if had_existing:
            await delete_existing_clean_copy_for_edit(message.id)

        if is_clean_signal_update(text):
            if not FORWARD_CLEAN_UPDATES:
                log.info(f"[SKIP UPDATE EDIT DISABLED] msg={message.id} text={text[:80]!r}")
                return

            sent = await copy_clean_update_message(message, edited=True)
            if sent:
                log.info(f"[MIRRORED CLEAN UPDATE EDIT] msg={message.id}")
            return

        if is_my_formatted_signal(text):
            sent = await copy_clean_signal_message(message, edited=True)
            if sent:
                log.info(f"[MIRRORED CLEAN SIGNAL EDIT] msg={message.id}")
            return

        # Edited into invalid/non-final content. If old final copy existed, it was already removed.
        if had_existing:
            log.info(f"[CLEAN EDIT REMOVED INVALID COPY] msg={message.id} text={text[:80]!r}")
        else:
            log.info(f"[SKIP EDIT NON SIGNAL] msg={message.id} text={text[:80]!r}")

    except Exception as exc:
        log.exception(f"[CLEAN EDIT HANDLER FAILED] msg={getattr(getattr(event, 'message', None), 'id', None)}: {exc}")
        alert_crash("exposedfx-clean-signal-forwarder:on_message_edited", exc)


@client.on(events.MessageDeleted(chats=SOURCE_CHAT))
async def on_deleted(event):
    try:
        for source_msg_id in getattr(event, "deleted_ids", []) or []:
            await delete_clean_copy_by_source_id(source_msg_id)
    except Exception as exc:
        log.exception(f"[CLEAN DELETE HANDLER FAILED]: {exc}")
        alert_crash("exposedfx-clean-signal-forwarder:on_deleted", exc)



async def main():
    await start_runtime_guard("exposedfx-clean-signal-forwarder", log)
    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("Telegram session loaded but account is not authorised. Generate a fresh session for this service.")

    me = await client.get_me()
    log.info(f"Logged in as {me.first_name} | id={me.id}")
    log.info(f"Source={SOURCE_CHAT} Destination={DEST_CHAT} CopyMode={COPY_MODE}")
    log.info("Anonymous clean copy mode active: True")
    log.info(f"STRICT_ONLY_AI_FORMAT={STRICT_ONLY_AI_FORMAT}")
    log.info(f"CLEAN_SEND_RETRY_ATTEMPTS={CLEAN_SEND_RETRY_ATTEMPTS}")
    log.info(f"CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS={CLEAN_SEND_RETRY_SLEEP_CAP_SECONDS}")
    log.info("Clean formatted signal forwarder running...")
    log.info("Delete sync from incoming group to final group: True")
    log.info(f"Clean signal update mirror active: {FORWARD_CLEAN_UPDATES}")
    log.info("Clean signal edit mirror active: True")
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        log.exception(f"[clean forwarder fatal crash] {type(exc).__name__}: {exc}")
        alert_crash("exposedfx-clean-signal-forwarder:fatal", exc)
        raise

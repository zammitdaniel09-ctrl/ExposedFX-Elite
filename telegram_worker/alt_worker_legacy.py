import asyncio
import json
import hashlib
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError, ChannelPrivateError
from telethon.sessions import StringSession
from telegram_worker import owner_forwarding_policy as owner_policy
from telethon.tl.types import MessageMediaWebPage

# ==============================================================
# LOGGING
# ==============================================================

from telegram_worker.log_setup import configure_logging

configure_logging("imperium-telegram-worker-alt")

log = logging.getLogger("imperium-alt-worker")


# ==============================================================
# ENV
# ==============================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SESSION_STRING = os.environ["ALT_SESSION_STRING"].strip()

EXPECTED_USER_ID = int(os.environ.get("ALT_EXPECTED_USER_ID", "0") or 0)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data-alt"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
owner_policy.configure(DATA_DIR)

MAP_FILE = DATA_DIR / "message_map.json"
STATE_FILE = DATA_DIR / "route_state.json"
MEDIA_DIR = DATA_DIR / "media_cache"

MEDIA_DIR.mkdir(parents=True, exist_ok=True)

CONNECT_DELAY = max(0, int(os.environ.get("TELEGRAM_CONNECT_DELAY_SECONDS", "5")))

WATCHDOG_SECONDS = max(2.0, float(os.environ.get("ALT_WATCHDOG_SECONDS", "5")))

WATCHDOG_LIMIT = max(50, int(os.environ.get("ALT_WATCHDOG_LIMIT", "500")))


# BEGIN ALT_LAST50_ONCE_CONFIG_V1


# END ALT_LAST50_ONCE_CONFIG_V1


# BEGIN ALT_PREMIUM_HISTORY_ONCE_CONFIG_V1


# END ALT_PREMIUM_HISTORY_ONCE_CONFIG_V1


# ==============================================================
# ROUTES
# ==============================================================


def load_routes():
    raw = os.environ.get("ALT_ROUTES_JSON", "").strip()
    if not raw:
        raise RuntimeError("ALT_ROUTES_JSON is empty")
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise RuntimeError("ALT_ROUTES_JSON must be a JSON list")
    routes = []
    for index, item in enumerate(parsed, start=1):
        source_topic = item.get("source_topic")
        if source_topic not in (None, ""):
            source_topic = int(source_topic)
        else:
            source_topic = None
        routes.append(
            {
                "name": str(item.get("name") or f"ALT RELAY {index}"),
                "source_chat": int(item["source_chat"]),
                "source_topic": source_topic,
                "dest_chat": int(item["dest_chat"]),
                "dest_topic": int(item["dest_topic"]),
            }
        )
    if not routes:
        raise RuntimeError("No ALT routes configured")
    return routes


# BEGIN ALT VIP5 DESTINATION PERMANENT STOP V1
# This topic is retired for ALL providers, even if ALT_ROUTES_JSON is changed.
ALT_RETIRED_DEST_CHAT = -1003726286301
ALT_RETIRED_DEST_TOPIC = 5


def alt_vip5_block(route):
    try:
        return (
            int(route.get("dest_chat") or 0) == ALT_RETIRED_DEST_CHAT
            and int(route.get("dest_topic") or 0) == ALT_RETIRED_DEST_TOPIC
        )
    except (TypeError, ValueError, AttributeError):
        return False


# END ALT VIP5 DESTINATION PERMANENT STOP V1


ROUTES = [
    route for route in load_routes() if not alt_vip5_block(route) and not owner_policy.is_stopped(route)
]
log.warning(
    "[OWNER ALT STOP POLICY] stopped_source_chats=%s remaining_routes=%s",
    sorted(owner_policy.STOPPED_SOURCE_CHATS),
    len(ROUTES),
)

SOURCE_CHATS = sorted({route["source_chat"] for route in ROUTES})


# ==============================================================
# CLIENT
# ==============================================================

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


# ==============================================================
# JSON STORAGE
# ==============================================================


def load_json(path):
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except Exception as exc:
        log.warning(f"[ALT STORAGE LOAD FAILED] path={path} {type(exc).__name__}: {exc}")
    return {}


def save_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")

    temp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")

    temp.replace(path)


message_map = load_json(MAP_FILE)

route_state = load_json(STATE_FILE)


# ==============================================================
# ROUTE / MAP KEYS
# ==============================================================


def route_key(route):
    return (
        f"{route['source_chat']}:"
        f"{route['source_topic']}:"
        f"{route['dest_chat']}:"
        f"{route['dest_topic']}"
    )


def map_key(route, source_message_id):
    return (
        f"{route['source_chat']}:"
        f"{int(source_message_id)}:"
        f"{route['dest_chat']}:"
        f"{route['dest_topic']}"
    )


def mapped_ids(route, source_message_id):
    value = message_map.get(map_key(route, source_message_id))
    if value is None:
        return []
    if isinstance(value, list):
        result = []
        for item in value:
            try:
                result.append(int(item))
            except Exception:
                pass
        return result
    try:
        return [int(value)]
    except Exception:
        return []


def checkpoint(route):
    try:
        return int(route_state.get(route_key(route), 0) or 0)

    except Exception:
        return 0


def update_checkpoint(route, source_message_id):
    current = checkpoint(route)
    new_value = max(current, int(source_message_id))
    if new_value == current:
        return
    route_state[route_key(route)] = new_value
    save_json(STATE_FILE, route_state)


def remember(route, source_message, destination_message):
    if source_message is None or destination_message is None:
        return
    if isinstance(destination_message, list):
        ids = [int(item.id) for item in destination_message if getattr(item, "id", None)]
        if not ids:
            return
        value = ids[0] if len(ids) == 1 else ids
    else:
        destination_id = getattr(destination_message, "id", None)
        if not destination_id:
            return
        value = int(destination_id)
    message_map[map_key(route, source_message.id)] = value
    save_json(MAP_FILE, message_map)
    update_checkpoint(route, source_message.id)


# ==============================================================
# TELEGRAM MESSAGE HELPERS
# ==============================================================


def text_of(message):
    return getattr(message, "message", None) or ""


def entities_of(message):
    return getattr(message, "entities", None)


# BEGIN USERNAME_MENTION_FILTER_V2

FILTER_USERNAME_MENTIONS = os.environ.get("FILTER_USERNAME_MENTIONS", "1").strip() == "1"


USERNAME_HANDLE_RE = re.compile(r"(?<![A-Za-z0-9._%+\-])" r"@[A-Za-z][A-Za-z0-9_]{4,31}" r"(?![A-Za-z0-9_])")


def has_username_mention(message):
    """
    Block an actual Telegram-style @username.

    Detection has two layers:

    1. Telegram MessageEntityMention when Telegram provides it.

    2. Safe text fallback for standalone Telegram-style handles
       when Telegram sends the handle as plain text.

    The fallback deliberately requires the @ not to be preceded
    by an email/local-part character, so addresses such as
    test@example.com remain allowed.
    """
    if not FILTER_USERNAME_MENTIONS:
        return False
    entities = getattr(message, "entities", None) or []
    for entity in entities:
        if type(entity).__name__ == "MessageEntityMention":
            return True
    text = text_of(message)
    if not text:
        return False
    return bool(USERNAME_HANDLE_RE.search(text))


def log_username_filter_alt(messages, context, route=None):
    messages = list(messages or [])
    ids = [int(getattr(message, "id", 0) or 0) for message in messages]
    route_name = route.get("name") if isinstance(route, dict) else None
    log.warning(
        f"[USERNAME MENTION FILTERED] account=ALT context={context} ids={ids} route={route_name} SENT=False FILTER=EntityOrStandaloneUsername"
    )


# END USERNAME_MENTION_FILTER_V2

# BEGIN GLOBAL_CONTENT_FILTER_V1

FILTER_LINKS = os.environ.get("FILTER_LINKS", "1").strip() == "1"

URL_SCHEME_RE = re.compile(r"(?i)(?<![A-Za-z0-9._%+\-@])(?:https?://|tg://|www\.|t\.me/|telegram\.me/)\S+")
BARE_DOMAIN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9._%+\-@])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
    r"(?::\d{2,5})?"
    r"(?:/[^\s<>()]*)?"
)

_original_has_username_mention_v2 = has_username_mention


def has_link(message):
    if not FILTER_LINKS:
        return False

    for entity in getattr(message, "entities", None) or []:
        entity_name = type(entity).__name__

        if entity_name == "MessageEntityUrl":
            return True

        if entity_name == "MessageEntityTextUrl":
            target = str(getattr(entity, "url", "") or "")
            if target.lower().startswith("mailto:"):
                continue
            return True

    text = text_of(message)
    if not text:
        return False

    return bool(URL_SCHEME_RE.search(text) or BARE_DOMAIN_RE.search(text))


def has_username_mention(message):
    return _original_has_username_mention_v2(message) or has_link(message)


def unit_has_username_mention(messages):
    return any(has_username_mention(message) for message in (messages or []))


# END GLOBAL_CONTENT_FILTER_V1


# BEGIN ALT_ARABIC115_TRANSLATION_V1

ALT_ARABIC115_SOURCE_CHAT = -1002211486103
ALT_ARABIC115_SOURCE_TOPIC = None

ALT_ARABIC115_DEST_CHAT = -1004367822325
ALT_ARABIC115_DEST_TOPIC = 115

ALT_ARABIC115_TARGET_LANGUAGE = "en"

ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS = max(
    3.0, float(os.environ.get("ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS", "10"))
)

ALT_ARABIC115_TRANSLATION_CONCURRENCY = max(
    1, int(os.environ.get("ALT_ARABIC115_TRANSLATION_CONCURRENCY", "3"))
)

ALT_ARABIC115_TRANSLATION_SEM = asyncio.Semaphore(ALT_ARABIC115_TRANSLATION_CONCURRENCY)

ALT_ARABIC_CHAR_RE = re.compile(
    r"[\u0600-\u06FF" r"\u0750-\u077F" r"\u08A0-\u08FF" r"\uFB50-\uFDFF" r"\uFE70-\uFEFF]"
)


def alt_arabic115_route(route):
    try:
        return (
            int(route["source_chat"]) == ALT_ARABIC115_SOURCE_CHAT
            and route["source_topic"] is ALT_ARABIC115_SOURCE_TOPIC
            and (int(route["dest_chat"]) == ALT_ARABIC115_DEST_CHAT)
            and (int(route["dest_topic"]) == ALT_ARABIC115_DEST_TOPIC)
        )
    except Exception:
        return False


def contains_arabic(text):

    return bool(text and ALT_ARABIC_CHAR_RE.search(text))


async def translated_payload(route, message):
    """
    The ONLY translation path used by both:
      - live Telegram pushes
      - watchdog recovery
      - the dedicated last50 history import
      - edits

    Non-Arabic text is returned byte-for-byte unchanged.

    Arabic is translated through Telegram's own
    messages.translateText API.

    Translation failure is FAIL-CLOSED:
      nothing is sent untranslated.
      The normal retry/watchdog path may retry later.
    """
    original_text = text_of(message)
    original_entities = entities_of(message)
    if not alt_arabic115_route(route):
        return (original_text, original_entities)
    if not original_text:
        return (original_text, original_entities)
    if not contains_arabic(original_text):
        return (original_text, original_entities)
    try:
        source_peer = await client.get_input_entity(ALT_ARABIC115_SOURCE_CHAT)
        request = functions.messages.TranslateTextRequest(
            peer=source_peer, id=[int(message.id)], text=None, to_lang=ALT_ARABIC115_TARGET_LANGUAGE
        )
        async with ALT_ARABIC115_TRANSLATION_SEM:
            response = await asyncio.wait_for(
                client(request), timeout=ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS
            )
        results = list(getattr(response, "result", None) or [])
        if len(results) != 1:
            raise RuntimeError(f"Telegram translation returned {len(results)} results")
        translated = results[0]
        translated_text = getattr(translated, "text", None) or ""
        translated_entities = getattr(translated, "entities", None) or []
        if not translated_text:
            raise RuntimeError("Telegram translation returned empty text")
        log.warning(
            f"[ALT TRANSLATED TO EN] route={route['name']} source_msg={int(message.id)} before_chars={len(original_text)} after_chars={len(translated_text)} ARABIC_TO_ENGLISH=True"
        )
        return (translated_text, translated_entities)
    except Exception as exc:
        log.warning(
            f"[ALT TRANSLATION FAILED CLOSED] route={route.get('name')} source_msg={int(message.id)} {type(exc).__name__}: {exc} ORIGINAL_ARABIC_SENT=False"
        )
        raise


async def preflight_arabic115_translation():
    """
    Runs BEFORE the route is activated.

    It performs one translation request against a recent Arabic
    source message, but sends NOTHING.

    This protects all existing ALT routes from an untested
    translation integration.
    """
    source = await client.get_entity(ALT_ARABIC115_SOURCE_CHAT)
    me = await client.get_me()
    recent = list(await client.get_messages(ALT_ARABIC115_SOURCE_CHAT, limit=50) or [])
    sample = next((message for message in recent if contains_arabic(text_of(message))), None)
    if sample is None:
        raise RuntimeError("No recent Arabic text message found for translation preflight")
    probe_route = {
        "name": "ARABIC115 PREFLIGHT",
        "source_chat": ALT_ARABIC115_SOURCE_CHAT,
        "source_topic": None,
        "dest_chat": ALT_ARABIC115_DEST_CHAT,
        "dest_topic": ALT_ARABIC115_DEST_TOPIC,
    }
    translated_text, _ = await translated_payload(probe_route, sample)
    if not translated_text:
        raise RuntimeError("Translation preflight returned empty text")
    log.warning(
        f"[ALT ARABIC115 TRANSLATION PREFLIGHT OK] source={ALT_ARABIC115_SOURCE_CHAT} source_title={getattr(source, 'title', None)!r} sample_id={int(sample.id)} premium={bool(getattr(me, 'premium', False))} SEND_TEST=False TARGET_LANGUAGE=en"
    )
    return True


# END ALT_ARABIC115_TRANSLATION_V1


def real_media(message):
    media = getattr(message, "media", None)

    if not media:
        return False

    return not isinstance(media, MessageMediaWebPage)


def known_topics(chat_id):
    return {
        int(route["source_topic"])
        for route in ROUTES
        if (int(route["source_chat"]) == int(chat_id) and route["source_topic"] is not None)
    }


def topic_of(message, chat_id):
    topics = known_topics(chat_id)
    for attr in ("reply_to_top_id", "top_msg_id"):
        value = getattr(message, attr, None)
        if value:
            try:
                return int(value)
            except Exception:
                pass
    reply = getattr(message, "reply_to", None)
    if reply:
        for attr in ("reply_to_top_id", "top_msg_id"):
            value = getattr(reply, attr, None)
            if value:
                try:
                    return int(value)
                except Exception:
                    pass
    direct_reply = getattr(message, "reply_to_msg_id", None)
    if direct_reply:
        try:
            direct_reply = int(direct_reply)
            if not topics or direct_reply in topics:
                return direct_reply
        except Exception:
            pass
    return None


def matching_routes(chat_id, message):
    chat_id = int(chat_id)
    source_topic = topic_of(message, chat_id)
    result = []
    for route in ROUTES:
        if route["source_chat"] != chat_id:
            continue
        wanted_topic = route["source_topic"]
        if wanted_topic is not None and wanted_topic != source_topic:
            continue
        result.append(route)
    return result


# ==============================================================
# SINGLE-WRITER LOCKS
# ==============================================================

COPY_LOCKS = {}
EDIT_LOCKS = {}


def copy_lock(route, messages):
    first = messages[0]
    grouped_id = getattr(first, "grouped_id", None)
    if grouped_id:
        message_token = f"album:{grouped_id}"
    else:
        message_token = f"msg:{int(first.id)}"
    key = f"{route_key(route)}:{message_token}"
    lock = COPY_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        COPY_LOCKS[key] = lock
    return lock


def edit_lock(route, source_message_id):
    key = map_key(route, source_message_id)

    lock = EDIT_LOCKS.get(key)

    if lock is None:
        lock = asyncio.Lock()
        EDIT_LOCKS[key] = lock

    return lock


# ==============================================================
# MEDIA SENDERS
# ==============================================================


async def send_single(route, message):
    if owner_policy.is_stopped(route):
        return None
    if alt_vip5_block(route):
        log.warning("[ALT VIP5 DESTINATION PERMANENT BLOCK] writer=send_single")
        return None

    if has_username_mention(message):

        log_username_filter_alt([message], "send_single_hard_guard", route=route)

        return None

    text, entities = await translated_payload(route, message)

    if not real_media(message):
        if not text:
            return None

        return await client.send_message(
            route["dest_chat"],
            text,
            formatting_entities=entities,
            parse_mode=None,
            reply_to=route["dest_topic"],
            link_preview=True,
        )

    # Direct Telegram media reference first.
    try:
        return await client.send_file(
            route["dest_chat"],
            message.media,
            caption=(text if text else None),
            formatting_entities=(entities if text else None),
            parse_mode=None,
            reply_to=route["dest_topic"],
        )

    except Exception as exc:
        log.warning(
            "[ALT DIRECT MEDIA FAILED - "
            "USING REUPLOAD] "
            f"source_msg={message.id} "
            f"{type(exc).__name__}: {exc}"
        )

    downloaded = await message.download_media(file=str(MEDIA_DIR / f"single_{message.id}"))

    if not downloaded:
        raise RuntimeError("Media download fallback returned nothing")

    try:
        return await client.send_file(
            route["dest_chat"],
            downloaded,
            caption=(text if text else None),
            formatting_entities=(entities if text else None),
            parse_mode=None,
            reply_to=route["dest_topic"],
        )

    finally:
        try:
            Path(downloaded).unlink(missing_ok=True)
        except Exception:
            pass


async def send_album(route, messages):
    if owner_policy.is_stopped(route):
        return None
    if alt_vip5_block(route):
        log.warning("[ALT VIP5 DESTINATION PERMANENT BLOCK] writer=send_album")
        return None
    if unit_has_username_mention(messages):
        log_username_filter_alt(messages, "send_album_hard_guard", route=route)
        return None
    caption = ""
    caption_entities = None
    caption_message = None
    for message in messages:
        candidate = text_of(message)
        if candidate:
            caption_message = message
            break
    if caption_message is not None:
        caption, caption_entities = await translated_payload(route, caption_message)
    media_objects = [message.media for message in messages if real_media(message)]
    if not media_objects:
        return None
    try:
        sent = await client.send_file(
            route["dest_chat"],
            media_objects,
            caption=caption if caption else None,
            formatting_entities=caption_entities if caption else None,
            parse_mode=None,
            reply_to=route["dest_topic"],
        )
        return sent if isinstance(sent, list) else [sent]
    except Exception as exc:
        log.warning(f"[ALT DIRECT ALBUM FAILED - USING REUPLOAD] {type(exc).__name__}: {exc}")
    downloaded_files = []
    try:
        for message in messages:
            if not real_media(message):
                continue
            downloaded = await message.download_media(file=str(MEDIA_DIR / f"album_{message.id}"))
            if not downloaded:
                raise RuntimeError("Album download returned nothing")
            downloaded_files.append(downloaded)
        sent = await client.send_file(
            route["dest_chat"],
            downloaded_files,
            caption=caption if caption else None,
            formatting_entities=caption_entities if caption else None,
            parse_mode=None,
            reply_to=route["dest_topic"],
        )
        return sent if isinstance(sent, list) else [sent]
    finally:
        for filename in downloaded_files:
            try:
                Path(filename).unlink(missing_ok=True)
            except Exception:
                pass


# ==============================================================
# FORWARD UNIT
# ==============================================================


async def _owner_original_copy_unit(route, messages, reason):
    if alt_vip5_block(route):
        log.warning("[ALT VIP5 DESTINATION PERMANENT BLOCK] copy_unit")
        return True
    messages = sorted(list(messages), key=lambda message: int(message.id))

    if not messages:
        return True

    if unit_has_username_mention(messages):

        update_checkpoint(route, max(int(message.id) for message in messages))

        log_username_filter_alt(messages, "copy_unit", route=route)

        return True

    async with copy_lock(route, messages):
        mapped = [bool(mapped_ids(route, message.id)) for message in messages]

        if all(mapped):
            update_checkpoint(route, max(int(message.id) for message in messages))
            return True

        if len(messages) > 1 and any(mapped):
            log.error("[ALT PARTIAL ALBUM MAP - " "FAIL CLOSED] " f"ids=" f"{[m.id for m in messages]}")
            return False

        try:
            if len(messages) > 1 and getattr(messages[0], "grouped_id", None):
                sent_items = await send_album(route, messages)

                if not sent_items:
                    raise RuntimeError("Album send returned no messages")

                pairs = list(zip(messages, sent_items))
                if len(sent_items) != len(messages):
                    # send_album only uploads real media; text-only items
                    # ride on the album. Map exactly what was sent so the
                    # watchdog does not send the album again.
                    media_messages = [m for m in messages if real_media(m)]
                    if len(media_messages) != len(sent_items):
                        raise RuntimeError("Album destination count mismatch")
                    pairs = list(zip(media_messages, sent_items)) + [
                        (m, sent_items[0]) for m in messages if not real_media(m)
                    ]
                    log.warning(
                        "[ALT ALBUM MEDIA-ONLY MAP] "
                        f"route={route['name']} "
                        f"ids={[m.id for m in messages]} "
                        f"sent={len(sent_items)}"
                    )

                for source, destination in pairs:
                    remember(route, source, destination)

                log.warning(
                    "[ALT COPIED] "
                    f"route={route['name']} "
                    f"ids="
                    f"{[m.id for m in messages]} "
                    "kind=ALBUM "
                    f"reason={reason}"
                )

                return True

            message = messages[0]

            sent = await send_single(route, message)

            if sent is None:
                update_checkpoint(route, message.id)

                log.info("[ALT EMPTY MESSAGE SKIPPED] " f"source_msg={message.id}")

                return True

            remember(route, message, sent)

            log.warning(
                "[ALT COPIED] "
                f"route={route['name']} "
                f"source_msg={message.id} "
                f"dest_msg="
                f"{getattr(sent, 'id', None)} "
                f"reason={reason}"
            )

            return True

        except FloodWaitError as exc:
            log.warning("[ALT FLOODWAIT] " f"route={route['name']} " f"seconds={int(exc.seconds)}")

            return False

        except Exception as exc:
            permanent = owner_policy.permanent_access_error(exc)
            if permanent:
                owner_policy.stop_route(route, permanent, log)
            log.exception(
                "[ALT COPY FAILED] "
                f"route={route['name']} "
                f"ids="
                f"{[m.id for m in messages]} "
                f"{type(exc).__name__}: {exc}"
            )

            return False


# Telethon delivers updates from connect(), but destination access hashes are
# only known after main() loads the dialogs. A send in that window fails with
# "Could not find the input entity", which the owner policy would record as a
# permanent stop. Copies and edits therefore wait (bounded) for the dialogs.
DIALOGS_READY = asyncio.Event()
DIALOGS_READY_WAIT_SECONDS = 60


async def wait_dialogs_ready():
    if DIALOGS_READY.is_set():
        return
    try:
        await asyncio.wait_for(DIALOGS_READY.wait(), DIALOGS_READY_WAIT_SECONDS)
    except asyncio.TimeoutError:
        log.warning("[ALT DIALOGS NOT READY] proceeding after wait=%ss", DIALOGS_READY_WAIT_SECONDS)


async def copy_unit(*args, **kwargs):
    route = kwargs.get("route") or (args[0] if len(args) > 0 else None)
    if route and owner_policy.is_stopped(route):
        return False
    await wait_dialogs_ready()
    try:
        return await _owner_original_copy_unit(*args, **kwargs)
    except Exception as exc:
        reason = owner_policy.permanent_access_error(exc)
        if reason and route:
            owner_policy.stop_route(route, reason, log)
            return False
        raise


# ==============================================================
# TRUE IN-PLACE EDITS
# ==============================================================


async def edit_existing(route, source_message):
    await wait_dialogs_ready()
    if has_username_mention(source_message):

        log_username_filter_alt([source_message], "edit_existing", route=route)

        return False

    async with edit_lock(route, source_message.id):
        # The source may edit almost immediately after sending.
        # Give NewMessage forwarding time to create its mapping.
        deadline = time.monotonic() + 10.0

        ids = []

        while time.monotonic() < deadline:
            ids = mapped_ids(route, source_message.id)

            if ids:
                break

            await asyncio.sleep(0.1)

        # Never import an old edit.
        if not ids:
            log.warning("[ALT EDIT SKIPPED - NO MAP] " f"source_msg={source_message.id}")
            return False

        if len(ids) != 1:
            log.warning(
                "[ALT EDIT SKIPPED - " "AMBIGUOUS MAP] " f"source_msg={source_message.id} " f"dest_ids={ids}"
            )
            return False

        destination_id = int(ids[0])

        try:
            destination_message = await client.get_messages(route["dest_chat"], ids=destination_id)

            if not destination_message:
                log.warning("[ALT EDIT DESTINATION MISSING] " f"dest_msg={destination_id}")
                return False

            # Text -> text and caption -> caption are okay.
            # We deliberately never delete/resend to change
            # the media structure.
            if real_media(source_message) != real_media(destination_message):
                log.warning(
                    "[ALT EDIT MEDIA STRUCTURE "
                    "CHANGE SKIPPED] "
                    f"source_msg="
                    f"{source_message.id} "
                    "NO_DELETE=True "
                    "NO_RESEND=True"
                )
                return False

            new_text, new_entities = await translated_payload(route, source_message)

            if not real_media(source_message) and not new_text:
                return False

            try:
                await client.edit_message(
                    route["dest_chat"],
                    destination_id,
                    new_text or "",
                    formatting_entities=(new_entities if new_text else None),
                    parse_mode=None,
                    link_preview=True,
                )

            except Exception as exc:
                if type(exc).__name__ == "MessageNotModifiedError":
                    return True

                raise

            log.warning(
                "[ALT EDITED IN PLACE] "
                f"route={route['name']} "
                f"source_msg="
                f"{source_message.id} "
                f"dest_msg={destination_id} "
                "SAME_MESSAGE_ID=True "
                "DELETED=False "
                "RESENT=False"
            )

            return True

        except Exception as exc:
            log.exception(
                "[ALT EDIT FAILED] " f"source_msg=" f"{source_message.id} " f"{type(exc).__name__}: {exc}"
            )

            return False


# BEGIN ALT_PAIR106_ORIGINAL_DEDUPE_V1

ALT_PAIR106_SOURCE_CHATS = {-1003252087470, -1002438454194}

ALT_PAIR106_DEST_CHAT = -1004367822325
ALT_PAIR106_DEST_TOPIC = 106

ALT_PAIR106_SEEN_FILE = DATA_DIR / "pair106_original_dedupe.json"

ALT_PAIR106_SEEN = load_json(ALT_PAIR106_SEEN_FILE)

ALT_PAIR106_LOCK = asyncio.Lock()

# Pair routes are not allowed to process Telegram updates until
# their first-start no-history baseline has been established.
ALT_PAIR106_READY_ROUTE_KEYS = set()


def alt_pair106_route(route):
    try:
        return (
            int(route["source_chat"]) in ALT_PAIR106_SOURCE_CHATS
            and route["source_topic"] is None
            and (int(route["dest_chat"]) == ALT_PAIR106_DEST_CHAT)
            and (int(route["dest_topic"]) == ALT_PAIR106_DEST_TOPIC)
        )
    except Exception:
        return False


# END ALT_PAIR106_ORIGINAL_DEDUPE_V1


# ==============================================================
# TELEGRAM PUSH EVENTS
# ==============================================================


@client.on(events.NewMessage(chats=SOURCE_CHATS))
async def new_message_handler(event):
    try:
        message = event.message

        # Album event owns grouped messages.
        if getattr(message, "grouped_id", None):
            return

        routes = matching_routes(event.chat_id, message)

        if not routes:
            return

        await asyncio.gather(*[copy_unit(route, [message], "telegram_push") for route in routes])

    except Exception as exc:
        log.exception("[ALT NEW MESSAGE HANDLER ERROR] " f"{type(exc).__name__}: {exc}")


@client.on(events.Album(chats=SOURCE_CHATS))
async def album_handler(event):
    try:
        messages = sorted(list(event.messages or []), key=lambda message: int(message.id))
        if not messages:
            return
        routes = matching_routes(event.chat_id, messages[0])
        if not routes:
            return
        await asyncio.gather(*[copy_unit(route, messages, "telegram_album") for route in routes])
    except Exception as exc:
        log.exception(f"[ALT ALBUM HANDLER ERROR] {type(exc).__name__}: {exc}")


@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def edited_handler(event):
    try:
        routes = matching_routes(event.chat_id, event.message)
        if not routes:
            return
        await asyncio.gather(*[edit_existing(route, event.message) for route in routes])
    except Exception as exc:
        log.exception(f"[ALT EDIT HANDLER ERROR] {type(exc).__name__}: {exc}")


# ==============================================================
# WATCHDOG / DEPLOYMENT RECOVERY
# ==============================================================


async def _owner_original_fetch_route_messages(route, limit, min_id=0):
    # min_id lets Telegram return only messages newer than a checkpoint, so a
    # quiet route costs one request instead of reading `limit` messages.
    if route["source_topic"] is not None:
        # Exact topic only. Never whole-chat fallback.
        return await client.get_messages(
            route["source_chat"], limit=int(limit), reply_to=int(route["source_topic"]), min_id=int(min_id)
        )

    return await client.get_messages(route["source_chat"], limit=int(limit), min_id=int(min_id))


async def fetch_route_messages(*args, **kwargs):
    route = kwargs.get("route") or (args[0] if len(args) > 0 else None)
    if route and owner_policy.is_stopped(route):
        return []
    try:
        return await _owner_original_fetch_route_messages(*args, **kwargs)
    except Exception as exc:
        reason = owner_policy.permanent_access_error(exc)
        if reason and route:
            owner_policy.stop_route(route, reason, log)
            return []
        raise


def build_units(messages):
    messages = sorted(list(messages), key=lambda message: int(message.id))
    units = []
    used_ids = set()
    for message in messages:
        message_id = int(message.id)
        if message_id in used_ids:
            continue
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            unit = [
                candidate for candidate in messages if getattr(candidate, "grouped_id", None) == grouped_id
            ]
        else:
            unit = [message]
        unit = sorted(unit, key=lambda item: int(item.id))
        for item in unit:
            used_ids.add(int(item.id))
        units.append(unit)
    return units


# BEGIN ALT LEGACY WATCHDOG EFFICIENCY V1
# Only inaccessible legacy PAIR106 sources are cooled down. Live push,
# healthy watchdogs, route checkpoints, mappings and history are untouched.
ALT_LEGACY_WATCHDOG_RETRY_SECONDS = max(60, int(os.environ.get("ALT_LEGACY_WATCHDOG_RETRY_SECONDS", "300")))


def alt_legacy_watchdog_access_failure(route, exc):
    return alt_pair106_route(route) and (
        isinstance(exc, ChannelPrivateError)
        or (isinstance(exc, ValueError) and "Could not find the input entity" in str(exc))
    )


# END ALT LEGACY WATCHDOG EFFICIENCY V1


async def watchdog(route):
    if owner_policy.is_stopped(route):
        return
    stored_checkpoint = checkpoint(route)

    # ----------------------------------------------------------
    # FIRST EVER START
    #
    # No persistent checkpoint exists:
    # Snapshot current newest message and copy NOTHING.
    # ----------------------------------------------------------

    if stored_checkpoint <= 0:
        while True:
            if owner_policy.is_stopped(route):
                return
            try:
                current = list(await fetch_route_messages(route, 50) or [])

                if owner_policy.is_stopped(route):
                    return

                stored_checkpoint = max([int(message.id) for message in current] or [0])

                route_state[route_key(route)] = stored_checkpoint

                save_json(STATE_FILE, route_state)

                if stored_checkpoint > 0 and alt_pair106_route(route):
                    ALT_PAIR106_READY_ROUTE_KEYS.add(route_key(route))

                log.warning(
                    "[ALT WATCHDOG FIRST BASELINE] "
                    f"route={route['name']} "
                    f"baseline={stored_checkpoint} "
                    "COPIED_HISTORY=0"
                )

                break

            except Exception as exc:
                if alt_legacy_watchdog_access_failure(route, exc):
                    log.warning(
                        "[ALT LEGACY WATCHDOG ACCESS COOLDOWN] "
                        f"phase=baseline source={route['source_chat']} "
                        f"reason={type(exc).__name__} "
                        f"retry_seconds={ALT_LEGACY_WATCHDOG_RETRY_SECONDS} "
                        "CHECKPOINT_UNCHANGED=True LIVE_PUSH_UNAFFECTED=True"
                    )
                    await asyncio.sleep(ALT_LEGACY_WATCHDOG_RETRY_SECONDS)
                else:
                    log.warning("[ALT WATCHDOG BASELINE FAILED] " f"{type(exc).__name__}: {exc}")
                    await asyncio.sleep(2)

    else:
        # ------------------------------------------------------
        # RESTART / FUTURE DEPLOY
        #
        # Continue from durable checkpoint instead of taking a
        # new baseline. Anything missed during deployment can
        # therefore be recovered.
        # ------------------------------------------------------

        log.warning(
            "[ALT WATCHDOG RESTORE] "
            f"route={route['name']} "
            f"checkpoint={stored_checkpoint} "
            "DEPLOYMENT_GAP_RECOVERY=True"
        )

    last_id = int(stored_checkpoint)

    while True:
        if owner_policy.is_stopped(route):
            return
        await asyncio.sleep(WATCHDOG_SECONDS)

        try:
            current = list(await fetch_route_messages(route, WATCHDOG_LIMIT, min_id=last_id) or [])

            fresh = [message for message in current if int(message.id) > last_id]

            for unit in build_units(fresh):
                if not unit:
                    continue

                # Give Telegram a moment to finish albums.
                grouped_id = getattr(unit[0], "grouped_id", None)

                if grouped_id:
                    timestamps = []

                    for item in unit:
                        value = getattr(item, "date", None)

                        if value:
                            try:
                                timestamps.append(float(value.timestamp()))
                            except Exception:
                                pass

                    if timestamps and (time.time() - max(timestamps)) < 1.5:
                        break

                max_id = max(int(message.id) for message in unit)

                success = await copy_unit(route, unit, "watchdog_recovery")

                if not success:
                    # Preserve ordering.
                    break

                last_id = max(last_id, max_id)

                update_checkpoint(route, last_id)

        except FloodWaitError as exc:
            wait_for = max(1, int(exc.seconds))

            log.warning("[ALT WATCHDOG FLOODWAIT] " f"wait={wait_for}s " "LIVE_PUSH_UNAFFECTED=True")

            await asyncio.sleep(wait_for)

        except Exception as exc:
            if alt_legacy_watchdog_access_failure(route, exc):
                log.warning(
                    "[ALT LEGACY WATCHDOG ACCESS COOLDOWN] "
                    f"phase=recovery source={route['source_chat']} "
                    f"reason={type(exc).__name__} "
                    f"retry_seconds={ALT_LEGACY_WATCHDOG_RETRY_SECONDS} "
                    "CHECKPOINT_UNCHANGED=True LIVE_PUSH_UNAFFECTED=True"
                )
                await asyncio.sleep(ALT_LEGACY_WATCHDOG_RETRY_SECONDS)
            else:
                log.warning(
                    "[ALT WATCHDOG ERROR] " f"{type(exc).__name__}: {exc} " "LIVE_PUSH_UNAFFECTED=True"
                )


# BEGIN ALT_ARABIC115_LAST50_V1


# END ALT_ARABIC115_LAST50_V1


# BEGIN ALT_LAST50_ONCE_HELPER_V1


# END ALT_LAST50_ONCE_HELPER_V1


# BEGIN ALT_PREMIUM_HISTORY_ONCE_HELPER_V1


# END ALT_PREMIUM_HISTORY_ONCE_HELPER_V1


# BEGIN ALT_FORCE50_V3


# END ALT_FORCE50_V3


# BEGIN ALT_ROUTE508_LAST50_V1


# END ALT_ROUTE508_LAST50_V1

# ==============================================================
# MAIN
# ==============================================================


async def main():
    if CONNECT_DELAY:
        log.warning("[ALT CONNECT GUARD] " f"waiting={CONNECT_DELAY}s")

        await asyncio.sleep(CONNECT_DELAY)

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError("ALT Telegram StringSession is unauthorized")

    me = await client.get_me()

    if EXPECTED_USER_ID and int(me.id) != EXPECTED_USER_ID:
        raise RuntimeError(
            "WRONG TELEGRAM ACCOUNT LOADED: " f"expected={EXPECTED_USER_ID} " f"actual={me.id}"
        )

    try:
        await client.get_dialogs(limit=None)
    finally:
        DIALOGS_READY.set()

    # ----------------------------------------------------------
    # ARABIC115 TRANSLATION PREFLIGHT
    #
    # SENDS NOTHING.
    # ----------------------------------------------------------

    try:

        await preflight_arabic115_translation()

    except Exception as exc:

        log.exception(
            "[ALT ARABIC115 TRANSLATION PREFLIGHT FAILED] "
            f"{type(exc).__name__}: {exc} "
            "ROUTE_SHOULD_NOT_BE_ACTIVATED=True"
        )

    # ----------------------------------------------------------
    # ACCESS PREFLIGHT
    # ----------------------------------------------------------

    required_routes = {
        (-1004349952583, 5246, -1004367822325, 7248),
        (-1004349952583, 5247, -1004367822325, 7250),
    }

    for route in list(ROUTES):
        identity = (
            int(route["source_chat"]),
            int(route["source_topic"]) if route["source_topic"] is not None else None,
            int(route["dest_chat"]),
            int(route["dest_topic"]),
        )

        try:
            source = await client.get_entity(route["source_chat"])
            destination = await client.get_entity(route["dest_chat"])

            if route["source_topic"] is not None:
                await client.get_messages(route["source_chat"], limit=1, reply_to=int(route["source_topic"]))

            topic_root = await client.get_messages(route["dest_chat"], ids=int(route["dest_topic"]))

            if not topic_root:
                raise RuntimeError(
                    "Relay topic does not exist: " f"{route['dest_chat']}_{route['dest_topic']}"
                )

        except (ChannelPrivateError, ValueError, RuntimeError) as exc:
            recoverable = (
                isinstance(exc, ChannelPrivateError)
                or (isinstance(exc, ValueError) and "Could not find the input entity" in str(exc))
                or (isinstance(exc, RuntimeError) and str(exc).startswith("Relay topic does not exist:"))
            )

            if not recoverable:
                raise
            owner_policy.stop_route(route, type(exc).__name__, log)
            ROUTES.remove(route)

            log.warning(
                "[ALT LEGACY ROUTE UNAVAILABLE] "
                f"route={route['name']} "
                f"source={route['source_chat']} "
                f"dest={route['dest_chat']}_{route['dest_topic']} "
                f"reason={type(exc).__name__} "
                "WORKER_CONTINUES=True"
            )
            continue

        log.warning(
            "[ALT ROUTE ACCESS OK] "
            f"route={route['name']} "
            f"source_title={getattr(source, 'title', None)!r} "
            f"dest_title={getattr(destination, 'title', None)!r}"
        )

    # ----------------------------------------------------------
    # PAIR106 STRICT NO-HISTORY INITIALISATION
    # ----------------------------------------------------------

    # One-time history imports (ARABIC115 last50, FORCE50 v3, last50, guarded
    # history, route508 last50) were retired on 4 Nov 2026: every flag was off
    # in production and each import had finished. See archive/README.md.

    # Recovery tasks.
    for route in ROUTES:
        asyncio.create_task(watchdog(route))

    log.warning(
        "[ALT RELAY READY] "
        f"user_id={me.id} "
        f"routes={len(ROUTES)} "
        f"sources={SOURCE_CHATS} "
        "PRIMARY=TELEGRAM_PUSH "
        f"WATCHDOG={WATCHDOG_SECONDS}s "
        "FIRST_START_NO_HISTORY=True "
        "RESTART_RECOVERY=True "
        "EDIT_IN_PLACE=True "
        "PERSISTENT_MAPPING=True"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

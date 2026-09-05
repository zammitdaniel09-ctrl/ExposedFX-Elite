import asyncio
import json
import hashlib
import logging
import os
import re
import time
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaWebPage


# ==============================================================
# LOGGING
# ==============================================================

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

log = logging.getLogger("imperium-alt-worker")


# ==============================================================
# ENV
# ==============================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SESSION_STRING = os.environ["ALT_SESSION_STRING"].strip()

EXPECTED_USER_ID = int(
    os.environ.get("ALT_EXPECTED_USER_ID", "0") or 0
)

DATA_DIR = Path(
    os.environ.get("DATA_DIR", "/data-alt")
)

DATA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

MAP_FILE = DATA_DIR / "message_map.json"
STATE_FILE = DATA_DIR / "route_state.json"
MEDIA_DIR = DATA_DIR / "media_cache"

MEDIA_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

CONNECT_DELAY = max(
    0,
    int(
        os.environ.get(
            "TELEGRAM_CONNECT_DELAY_SECONDS",
            "5",
        )
    ),
)

WATCHDOG_SECONDS = max(
    2.0,
    float(
        os.environ.get(
            "ALT_WATCHDOG_SECONDS",
            "5",
        )
    ),
)

WATCHDOG_LIMIT = max(
    50,
    int(
        os.environ.get(
            "ALT_WATCHDOG_LIMIT",
            "500",
        )
    ),
)



# BEGIN ALT_LAST50_ONCE_CONFIG_V1

ALT_LAST50_ONCE = (
    os.environ.get(
        "ALT_LAST50_ONCE",
        "0",
    ).strip()
    == "1"
)

ALT_LAST50_COUNT = max(
    1,
    int(
        os.environ.get(
            "ALT_LAST50_COUNT",
            "50",
        )
    ),
)

ALT_LAST50_FETCH_LIMIT = max(
    200,
    int(
        os.environ.get(
            "ALT_LAST50_FETCH_LIMIT",
            "1000",
        )
    ),
)

ALT_LAST50_SOURCE_CHAT = -1003364661276
ALT_LAST50_DEST_CHAT = -1004367822325
ALT_LAST50_DEST_TOPIC = 2

ALT_LAST50_DONE_FILE = (
    DATA_DIR
    / "alt_last50_3364661276_to_4367822325_2.done.json"
)

ALT_LAST50_PROGRESS_FILE = (
    DATA_DIR
    / "alt_last50_3364661276_to_4367822325_2.progress.json"
)

# END ALT_LAST50_ONCE_CONFIG_V1



# BEGIN ALT_PREMIUM_HISTORY_ONCE_CONFIG_V1

ALT_HISTORY_ONCE = (
    os.environ.get(
        "ALT_HISTORY_ONCE",
        "0",
    ).strip()
    == "1"
)

ALT_HISTORY_RUN_ID = (
    os.environ.get(
        "ALT_HISTORY_RUN_ID",
        "",
    ).strip()
)

ALT_HISTORY_COUNT = max(
    1,
    int(
        os.environ.get(
            "ALT_HISTORY_COUNT",
            "50",
        )
    ),
)

ALT_HISTORY_FETCH_LIMIT = max(
    200,
    int(
        os.environ.get(
            "ALT_HISTORY_FETCH_LIMIT",
            "1000",
        )
    ),
)

ALT_HISTORY_REQUIRE_PREMIUM = (
    os.environ.get(
        "ALT_HISTORY_REQUIRE_PREMIUM",
        "1",
    ).strip()
    == "1"
)

ALT_HISTORY_SOURCE_CHAT = -1003364661276
ALT_HISTORY_DEST_CHAT = -1004367822325
ALT_HISTORY_DEST_TOPIC = 2

# END ALT_PREMIUM_HISTORY_ONCE_CONFIG_V1


# ==============================================================
# ROUTES
# ==============================================================

def load_routes():
    raw = os.environ.get(
        "ALT_ROUTES_JSON",
        "",
    ).strip()

    if not raw:
        raise RuntimeError(
            "ALT_ROUTES_JSON is empty"
        )

    parsed = json.loads(raw)

    if isinstance(parsed, dict):
        parsed = [parsed]

    if not isinstance(parsed, list):
        raise RuntimeError(
            "ALT_ROUTES_JSON must be a JSON list"
        )

    routes = []

    for index, item in enumerate(
        parsed,
        start=1,
    ):
        source_topic = item.get(
            "source_topic"
        )

        if source_topic not in (None, ""):
            source_topic = int(source_topic)
        else:
            source_topic = None

        routes.append({
            "name": str(item.get("name") or f"ALT RELAY {index}"),
            "source_chat": int(
                item["source_chat"]
            ),
            "source_topic": source_topic,
            "dest_chat": int(
                item["dest_chat"]
            ),
            "dest_topic": int(
                item["dest_topic"]
            ),
        })

    if not routes:
        raise RuntimeError(
            "No ALT routes configured"
        )

    return routes


ROUTES = load_routes()

SOURCE_CHATS = sorted({
    route["source_chat"]
    for route in ROUTES
})


# ==============================================================
# CLIENT
# ==============================================================

client = TelegramClient(
    StringSession(
        SESSION_STRING
    ),
    API_ID,
    API_HASH,
)


# ==============================================================
# JSON STORAGE
# ==============================================================

def load_json(path):
    if not path.exists():
        return {}

    try:
        value = json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(value, dict):
            return value

    except Exception as exc:
        log.warning(
            f"[ALT STORAGE LOAD FAILED] "
            f"path={path} "
            f"{type(exc).__name__}: {exc}"
        )

    return {}


def save_json(path, value):
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    temp.write_text(
        json.dumps(
            value,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    temp.replace(path)


message_map = load_json(
    MAP_FILE
)

route_state = load_json(
    STATE_FILE
)


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


def map_key(
    route,
    source_message_id,
):
    return (
        f"{route['source_chat']}:"
        f"{int(source_message_id)}:"
        f"{route['dest_chat']}:"
        f"{route['dest_topic']}"
    )


def mapped_ids(
    route,
    source_message_id,
):
    value = message_map.get(
        map_key(
            route,
            source_message_id,
        )
    )

    if value is None:
        return []

    if isinstance(value, list):
        result = []

        for item in value:
            try:
                result.append(
                    int(item)
                )
            except Exception:
                pass

        return result

    try:
        return [int(value)]

    except Exception:
        return []


def checkpoint(route):
    try:
        return int(
            route_state.get(
                route_key(route),
                0,
            )
            or 0
        )

    except Exception:
        return 0


def update_checkpoint(
    route,
    source_message_id,
):
    current = checkpoint(route)
    new_value = max(
        current,
        int(source_message_id),
    )

    if new_value == current:
        return

    route_state[
        route_key(route)
    ] = new_value

    save_json(
        STATE_FILE,
        route_state,
    )


def remember(
    route,
    source_message,
    destination_message,
):
    if (
        source_message is None
        or destination_message is None
    ):
        return

    if isinstance(
        destination_message,
        list,
    ):
        ids = [
            int(item.id)
            for item in destination_message
            if getattr(
                item,
                "id",
                None,
            )
        ]

        if not ids:
            return

        value = (
            ids[0]
            if len(ids) == 1
            else ids
        )

    else:
        destination_id = getattr(
            destination_message,
            "id",
            None,
        )

        if not destination_id:
            return

        value = int(
            destination_id
        )

    message_map[
        map_key(
            route,
            source_message.id,
        )
    ] = value

    save_json(
        MAP_FILE,
        message_map,
    )

    update_checkpoint(
        route,
        source_message.id,
    )


# ==============================================================
# TELEGRAM MESSAGE HELPERS
# ==============================================================

def text_of(message):
    return (
        getattr(
            message,
            "message",
            None,
        )
        or ""
    )


def entities_of(message):
    return getattr(
        message,
        "entities",
        None,
    )


# BEGIN USERNAME_MENTION_FILTER_V2

FILTER_USERNAME_MENTIONS = (
    os.environ.get(
        "FILTER_USERNAME_MENTIONS",
        "1",
    ).strip()
    == "1"
)


USERNAME_HANDLE_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])"
    r"@[A-Za-z][A-Za-z0-9_]{4,31}"
    r"(?![A-Za-z0-9_])"
)


def has_username_mention(
    message,
):
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


    entities = (
        getattr(
            message,
            "entities",
            None,
        )
        or []
    )


    for entity in entities:

        if (
            type(entity).__name__
            == "MessageEntityMention"
        ):

            return True


    text = text_of(
        message
    )


    if not text:
        return False


    return bool(
        USERNAME_HANDLE_RE.search(
            text
        )
    )


def unit_has_username_mention(
    messages,
):

    return any(
        has_username_mention(
            message
        )
        for message in (
            messages
            or []
        )
    )


def log_username_filter_alt(
    messages,
    context,
    route=None,
):

    messages = list(
        messages
        or []
    )


    ids = [
        int(
            getattr(
                message,
                "id",
                0,
            )
            or 0
        )
        for message in messages
    ]


    route_name = (
        route.get("name")
        if isinstance(route, dict)
        else None
    )


    log.warning(
        "[USERNAME MENTION FILTERED] "
        "account=ALT "
        f"context={context} "
        f"ids={ids} "
        f"route={route_name} "
        "SENT=False "
        "FILTER=EntityOrStandaloneUsername"
    )


# END USERNAME_MENTION_FILTER_V2




# BEGIN ALT_ARABIC115_TRANSLATION_V1

ALT_ARABIC115_SOURCE_CHAT = -1002211486103
ALT_ARABIC115_SOURCE_TOPIC = None

ALT_ARABIC115_DEST_CHAT = -1004367822325
ALT_ARABIC115_DEST_TOPIC = 115

ALT_ARABIC115_TARGET_LANGUAGE = "en"

ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS = max(
    3.0,
    float(
        os.environ.get(
            "ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS",
            "10",
        )
    ),
)

ALT_ARABIC115_TRANSLATION_CONCURRENCY = max(
    1,
    int(
        os.environ.get(
            "ALT_ARABIC115_TRANSLATION_CONCURRENCY",
            "3",
        )
    ),
)

ALT_ARABIC115_TRANSLATION_SEM = asyncio.Semaphore(
    ALT_ARABIC115_TRANSLATION_CONCURRENCY
)

ALT_ARABIC_CHAR_RE = re.compile(
    r"[\u0600-\u06FF"
    r"\u0750-\u077F"
    r"\u08A0-\u08FF"
    r"\uFB50-\uFDFF"
    r"\uFE70-\uFEFF]"
)


def alt_arabic115_route(
    route,
):

    try:

        return (
            int(route["source_chat"])
            == ALT_ARABIC115_SOURCE_CHAT
            and route["source_topic"]
            is ALT_ARABIC115_SOURCE_TOPIC
            and int(route["dest_chat"])
            == ALT_ARABIC115_DEST_CHAT
            and int(route["dest_topic"])
            == ALT_ARABIC115_DEST_TOPIC
        )

    except Exception:

        return False


def contains_arabic(
    text,
):

    return bool(
        text
        and ALT_ARABIC_CHAR_RE.search(
            text
        )
    )


async def translated_payload(
    route,
    message,
):
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

    original_text = text_of(
        message
    )

    original_entities = entities_of(
        message
    )


    if not alt_arabic115_route(
        route
    ):

        return (
            original_text,
            original_entities,
        )


    if not original_text:

        return (
            original_text,
            original_entities,
        )


    if not contains_arabic(
        original_text
    ):

        return (
            original_text,
            original_entities,
        )


    try:

        source_peer = await client.get_input_entity(
            ALT_ARABIC115_SOURCE_CHAT
        )


        request = (
            functions.messages.TranslateTextRequest(
                peer=source_peer,
                id=[
                    int(message.id)
                ],
                text=None,
                to_lang=(
                    ALT_ARABIC115_TARGET_LANGUAGE
                ),
            )
        )


        async with ALT_ARABIC115_TRANSLATION_SEM:

            response = await asyncio.wait_for(
                client(
                    request
                ),
                timeout=(
                    ALT_ARABIC115_TRANSLATION_TIMEOUT_SECONDS
                ),
            )


        results = list(
            getattr(
                response,
                "result",
                None,
            )
            or []
        )


        if len(results) != 1:

            raise RuntimeError(
                "Telegram translation returned "
                f"{len(results)} results"
            )


        translated = results[0]


        translated_text = (
            getattr(
                translated,
                "text",
                None,
            )
            or ""
        )


        translated_entities = (
            getattr(
                translated,
                "entities",
                None,
            )
            or []
        )


        if not translated_text:

            raise RuntimeError(
                "Telegram translation returned empty text"
            )


        log.warning(
            "[ALT TRANSLATED TO EN] "
            f"route={route['name']} "
            f"source_msg={int(message.id)} "
            f"before_chars={len(original_text)} "
            f"after_chars={len(translated_text)} "
            "ARABIC_TO_ENGLISH=True"
        )


        return (
            translated_text,
            translated_entities,
        )


    except Exception as exc:

        log.warning(
            "[ALT TRANSLATION FAILED CLOSED] "
            f"route={route.get('name')} "
            f"source_msg={int(message.id)} "
            f"{type(exc).__name__}: {exc} "
            "ORIGINAL_ARABIC_SENT=False"
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

    source = await client.get_entity(
        ALT_ARABIC115_SOURCE_CHAT
    )

    me = await client.get_me()


    recent = list(
        await client.get_messages(
            ALT_ARABIC115_SOURCE_CHAT,
            limit=50,
        )
        or []
    )


    sample = next(
        (
            message
            for message in recent
            if contains_arabic(
                text_of(
                    message
                )
            )
        ),
        None,
    )


    if sample is None:

        raise RuntimeError(
            "No recent Arabic text message found "
            "for translation preflight"
        )


    probe_route = {
        "name": "ARABIC115 PREFLIGHT",
        "source_chat": (
            ALT_ARABIC115_SOURCE_CHAT
        ),
        "source_topic": None,
        "dest_chat": (
            ALT_ARABIC115_DEST_CHAT
        ),
        "dest_topic": (
            ALT_ARABIC115_DEST_TOPIC
        ),
    }


    translated_text, _ = await translated_payload(
        probe_route,
        sample,
    )


    if not translated_text:

        raise RuntimeError(
            "Translation preflight returned empty text"
        )


    log.warning(
        "[ALT ARABIC115 TRANSLATION PREFLIGHT OK] "
        f"source={ALT_ARABIC115_SOURCE_CHAT} "
        f"source_title="
        f"{getattr(source, 'title', None)!r} "
        f"sample_id={int(sample.id)} "
        f"premium="
        f"{bool(getattr(me, 'premium', False))} "
        "SEND_TEST=False "
        "TARGET_LANGUAGE=en"
    )


    return True


# END ALT_ARABIC115_TRANSLATION_V1



def real_media(message):
    media = getattr(
        message,
        "media",
        None,
    )

    if not media:
        return False

    return not isinstance(
        media,
        MessageMediaWebPage,
    )


def known_topics(chat_id):
    return {
        int(route["source_topic"])
        for route in ROUTES
        if (
            int(route["source_chat"])
            == int(chat_id)
            and route["source_topic"]
            is not None
        )
    }


def topic_of(
    message,
    chat_id,
):
    topics = known_topics(
        chat_id
    )

    for attr in (
        "reply_to_top_id",
        "top_msg_id",
    ):
        value = getattr(
            message,
            attr,
            None,
        )

        if value:
            try:
                return int(value)
            except Exception:
                pass

    reply = getattr(
        message,
        "reply_to",
        None,
    )

    if reply:
        for attr in (
            "reply_to_top_id",
            "top_msg_id",
        ):
            value = getattr(
                reply,
                attr,
                None,
            )

            if value:
                try:
                    return int(value)
                except Exception:
                    pass

    direct_reply = getattr(
        message,
        "reply_to_msg_id",
        None,
    )

    if direct_reply:
        try:
            direct_reply = int(
                direct_reply
            )

            if (
                not topics
                or direct_reply in topics
            ):
                return direct_reply

        except Exception:
            pass

    return None


def matching_routes(
    chat_id,
    message,
):
    chat_id = int(chat_id)

    source_topic = topic_of(
        message,
        chat_id,
    )

    result = []

    for route in ROUTES:
        if (
            route["source_chat"]
            != chat_id
        ):
            continue

        wanted_topic = route[
            "source_topic"
        ]

        if (
            wanted_topic is not None
            and wanted_topic
            != source_topic
        ):
            continue

        result.append(route)

    return result


# ==============================================================
# SINGLE-WRITER LOCKS
# ==============================================================

COPY_LOCKS = {}
EDIT_LOCKS = {}


def copy_lock(
    route,
    messages,
):
    first = messages[0]

    grouped_id = getattr(
        first,
        "grouped_id",
        None,
    )

    if grouped_id:
        message_token = (
            f"album:{grouped_id}"
        )
    else:
        message_token = (
            f"msg:{int(first.id)}"
        )

    key = (
        f"{route_key(route)}:"
        f"{message_token}"
    )

    lock = COPY_LOCKS.get(key)

    if lock is None:
        lock = asyncio.Lock()
        COPY_LOCKS[key] = lock

    return lock


def edit_lock(
    route,
    source_message_id,
):
    key = map_key(
        route,
        source_message_id,
    )

    lock = EDIT_LOCKS.get(key)

    if lock is None:
        lock = asyncio.Lock()
        EDIT_LOCKS[key] = lock

    return lock


# ==============================================================
# MEDIA SENDERS
# ==============================================================

async def send_single(
    route,
    message,
):
    if has_username_mention(
        message
    ):

        log_username_filter_alt(
            [message],
            "send_single_hard_guard",
            route=route,
        )

        return None

    text, entities = await translated_payload(
        route,
        message,
    )

    if not real_media(message):
        if not text:
            return None

        return await client.send_message(
            route["dest_chat"],
            text,
            formatting_entities=entities,
            parse_mode=None,
            reply_to=route[
                "dest_topic"
            ],
            link_preview=True,
        )

    # Direct Telegram media reference first.
    try:
        return await client.send_file(
            route["dest_chat"],
            message.media,
            caption=(
                text
                if text
                else None
            ),
            formatting_entities=(
                entities
                if text
                else None
            ),
            parse_mode=None,
            reply_to=route[
                "dest_topic"
            ],
        )

    except Exception as exc:
        log.warning(
            "[ALT DIRECT MEDIA FAILED - "
            "USING REUPLOAD] "
            f"source_msg={message.id} "
            f"{type(exc).__name__}: {exc}"
        )

    downloaded = await message.download_media(
        file=str(
            MEDIA_DIR
            / f"single_{message.id}"
        )
    )

    if not downloaded:
        raise RuntimeError(
            "Media download fallback returned nothing"
        )

    try:
        return await client.send_file(
            route["dest_chat"],
            downloaded,
            caption=(
                text
                if text
                else None
            ),
            formatting_entities=(
                entities
                if text
                else None
            ),
            parse_mode=None,
            reply_to=route[
                "dest_topic"
            ],
        )

    finally:
        try:
            Path(downloaded).unlink(
                missing_ok=True
            )
        except Exception:
            pass


async def send_album(
    route,
    messages,
):
    if unit_has_username_mention(
        messages
    ):

        log_username_filter_alt(
            messages,
            "send_album_hard_guard",
            route=route,
        )

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

        caption, caption_entities = (
            await translated_payload(
                route,
                caption_message,
            )
        )

    media_objects = [
        message.media
        for message in messages
        if real_media(message)
    ]

    if not media_objects:
        return None

    try:
        sent = await client.send_file(
            route["dest_chat"],
            media_objects,
            caption=(
                caption
                if caption
                else None
            ),
            formatting_entities=(
                caption_entities
                if caption
                else None
            ),
            parse_mode=None,
            reply_to=route[
                "dest_topic"
            ],
        )

        return (
            sent
            if isinstance(sent, list)
            else [sent]
        )

    except Exception as exc:
        log.warning(
            "[ALT DIRECT ALBUM FAILED - "
            "USING REUPLOAD] "
            f"{type(exc).__name__}: {exc}"
        )

    downloaded_files = []

    try:
        for message in messages:
            if not real_media(message):
                continue

            downloaded = await message.download_media(
                file=str(
                    MEDIA_DIR
                    / f"album_{message.id}"
                )
            )

            if not downloaded:
                raise RuntimeError(
                    "Album download returned nothing"
                )

            downloaded_files.append(
                downloaded
            )

        sent = await client.send_file(
            route["dest_chat"],
            downloaded_files,
            caption=(
                caption
                if caption
                else None
            ),
            formatting_entities=(
                caption_entities
                if caption
                else None
            ),
            parse_mode=None,
            reply_to=route[
                "dest_topic"
            ],
        )

        return (
            sent
            if isinstance(sent, list)
            else [sent]
        )

    finally:
        for filename in downloaded_files:
            try:
                Path(filename).unlink(
                    missing_ok=True
                )
            except Exception:
                pass


# ==============================================================
# FORWARD UNIT
# ==============================================================

async def copy_unit(
    route,
    messages,
    reason,
):
    messages = sorted(
        list(messages),
        key=lambda message: int(
            message.id
        ),
    )

    if not messages:
        return True

    if unit_has_username_mention(
        messages
    ):

        update_checkpoint(
            route,
            max(
                int(message.id)
                for message in messages
            ),
        )

        log_username_filter_alt(
            messages,
            "copy_unit",
            route=route,
        )

        return True

    async with copy_lock(
        route,
        messages,
    ):
        mapped = [
            bool(
                mapped_ids(
                    route,
                    message.id,
                )
            )
            for message in messages
        ]

        if all(mapped):
            update_checkpoint(
                route,
                max(
                    int(message.id)
                    for message in messages
                ),
            )
            return True

        if (
            len(messages) > 1
            and any(mapped)
        ):
            log.error(
                "[ALT PARTIAL ALBUM MAP - "
                "FAIL CLOSED] "
                f"ids="
                f"{[m.id for m in messages]}"
            )
            return False

        try:
            if (
                len(messages) > 1
                and getattr(
                    messages[0],
                    "grouped_id",
                    None,
                )
            ):
                sent_items = await send_album(
                    route,
                    messages,
                )

                if not sent_items:
                    raise RuntimeError(
                        "Album send returned no messages"
                    )

                if (
                    len(sent_items)
                    != len(messages)
                ):
                    raise RuntimeError(
                        "Album destination count mismatch"
                    )

                for source, destination in zip(
                    messages,
                    sent_items,
                ):
                    remember(
                        route,
                        source,
                        destination,
                    )

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

            sent = await send_single(
                route,
                message,
            )

            if sent is None:
                update_checkpoint(
                    route,
                    message.id,
                )

                log.info(
                    "[ALT EMPTY MESSAGE SKIPPED] "
                    f"source_msg={message.id}"
                )

                return True

            remember(
                route,
                message,
                sent,
            )

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
            log.warning(
                "[ALT FLOODWAIT] "
                f"route={route['name']} "
                f"seconds={int(exc.seconds)}"
            )

            return False

        except Exception as exc:
            log.exception(
                "[ALT COPY FAILED] "
                f"route={route['name']} "
                f"ids="
                f"{[m.id for m in messages]} "
                f"{type(exc).__name__}: {exc}"
            )

            return False


# ==============================================================
# TRUE IN-PLACE EDITS
# ==============================================================

async def edit_existing(
    route,
    source_message,
):
    if has_username_mention(
        source_message
    ):

        log_username_filter_alt(
            [source_message],
            "edit_existing",
            route=route,
        )

        return False

    async with edit_lock(
        route,
        source_message.id,
    ):
        # The source may edit almost immediately after sending.
        # Give NewMessage forwarding time to create its mapping.
        deadline = (
            time.monotonic()
            + 10.0
        )

        ids = []

        while time.monotonic() < deadline:
            ids = mapped_ids(
                route,
                source_message.id,
            )

            if ids:
                break

            await asyncio.sleep(0.1)

        # Never import an old edit.
        if not ids:
            log.warning(
                "[ALT EDIT SKIPPED - NO MAP] "
                f"source_msg={source_message.id}"
            )
            return False

        if len(ids) != 1:
            log.warning(
                "[ALT EDIT SKIPPED - "
                "AMBIGUOUS MAP] "
                f"source_msg={source_message.id} "
                f"dest_ids={ids}"
            )
            return False

        destination_id = int(
            ids[0]
        )

        try:
            destination_message = (
                await client.get_messages(
                    route["dest_chat"],
                    ids=destination_id,
                )
            )

            if not destination_message:
                log.warning(
                    "[ALT EDIT DESTINATION MISSING] "
                    f"dest_msg={destination_id}"
                )
                return False

            # Text -> text and caption -> caption are okay.
            # We deliberately never delete/resend to change
            # the media structure.
            if (
                real_media(source_message)
                != real_media(
                    destination_message
                )
            ):
                log.warning(
                    "[ALT EDIT MEDIA STRUCTURE "
                    "CHANGE SKIPPED] "
                    f"source_msg="
                    f"{source_message.id} "
                    "NO_DELETE=True "
                    "NO_RESEND=True"
                )
                return False

            new_text, new_entities = (
                await translated_payload(
                    route,
                    source_message,
                )
            )

            if (
                not real_media(source_message)
                and not new_text
            ):
                return False

            try:
                await client.edit_message(
                    route["dest_chat"],
                    destination_id,
                    new_text or "",
                    formatting_entities=(
                        new_entities
                        if new_text
                        else None
                    ),
                    parse_mode=None,
                    link_preview=True,
                )

            except Exception as exc:
                if (
                    type(exc).__name__
                    == "MessageNotModifiedError"
                ):
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
                "[ALT EDIT FAILED] "
                f"source_msg="
                f"{source_message.id} "
                f"{type(exc).__name__}: {exc}"
            )

            return False



# BEGIN ALT_PAIR106_ORIGINAL_DEDUPE_V1

ALT_PAIR106_SOURCE_CHATS = {
    -1003252087470,
    -1002438454194,
}

ALT_PAIR106_DEST_CHAT = -1004367822325
ALT_PAIR106_DEST_TOPIC = 106

ALT_PAIR106_SEEN_FILE = (
    DATA_DIR
    / "pair106_original_dedupe.json"
)

ALT_PAIR106_SEEN = load_json(
    ALT_PAIR106_SEEN_FILE
)

ALT_PAIR106_LOCK = asyncio.Lock()

# Pair routes are not allowed to process Telegram updates until
# their first-start no-history baseline has been established.
ALT_PAIR106_READY_ROUTE_KEYS = set()


def alt_pair106_route(
    route,
):

    try:

        return (
            int(route["source_chat"])
            in ALT_PAIR106_SOURCE_CHATS
            and route["source_topic"] is None
            and int(route["dest_chat"])
            == ALT_PAIR106_DEST_CHAT
            and int(route["dest_topic"])
            == ALT_PAIR106_DEST_TOPIC
        )

    except Exception:

        return False


def alt_pair106_channel_id_from_chat(
    chat_id,
):

    raw = str(
        abs(
            int(chat_id)
        )
    )


    # Telegram full channel IDs are represented as:
    # -1003252087470
    #
    # PeerChannel.channel_id is:
    # 3252087470
    if raw.startswith("100"):

        raw = raw[3:]


    return int(
        raw
    )


def alt_pair106_peer_identity(
    peer,
):

    if peer is None:

        return None


    channel_id = getattr(
        peer,
        "channel_id",
        None,
    )


    if channel_id:

        return (
            "channel",
            int(channel_id),
        )


    chat_id = getattr(
        peer,
        "chat_id",
        None,
    )


    if chat_id:

        return (
            "chat",
            int(chat_id),
        )


    user_id = getattr(
        peer,
        "user_id",
        None,
    )


    if user_id:

        return (
            "user",
            int(user_id),
        )


    return None


def alt_pair106_forward_identity(
    message,
):
    """
    Resolve Telegram's ORIGINAL forwarded post.

    Examples this catches:

    A original post -> B forwards A
    A and B both forward same third-party channel post
    B original post -> A forwards B
    """

    forward = getattr(
        message,
        "fwd_from",
        None,
    )


    if not forward:

        return None


    channel_post = getattr(
        forward,
        "channel_post",
        None,
    )


    peer = getattr(
        forward,
        "from_id",
        None,
    )


    peer_identity = alt_pair106_peer_identity(
        peer
    )


    if (
        peer_identity
        and channel_post
    ):

        return (
            peer_identity[0],
            peer_identity[1],
            int(channel_post),
        )


    saved_peer = getattr(
        forward,
        "saved_from_peer",
        None,
    )


    saved_message = getattr(
        forward,
        "saved_from_msg_id",
        None,
    )


    saved_identity = alt_pair106_peer_identity(
        saved_peer
    )


    if (
        saved_identity
        and saved_message
    ):

        return (
            saved_identity[0],
            saved_identity[1],
            int(saved_message),
        )


    return None


def alt_pair106_direct_identity(
    route,
    message,
):

    return (
        "channel",
        alt_pair106_channel_id_from_chat(
            route["source_chat"]
        ),
        int(message.id),
    )


def alt_pair106_hash_identity_list(
    identities,
):

    encoded = json.dumps(
        identities,
        separators=(",", ":"),
        sort_keys=False,
    ).encode(
        "utf-8"
    )


    return hashlib.sha256(
        encoded
    ).hexdigest()


def alt_pair106_candidate_tokens(
    route,
    messages,
):
    """
    Store/check BOTH identities:

    1. Direct source Telegram post IDs.
    2. Forwarded-original Telegram post IDs.

    This is important because:

      A posts #100 directly
      B forwards A #100

    A's direct token and B's forwarded-original token become
    the exact same token.

    It also handles nested forwarding better because a winner
    can be indexed under both its own source post and the
    original forwarded post.
    """

    messages = sorted(
        list(messages),
        key=lambda item: int(
            item.id
        ),
    )


    direct_identities = [
        alt_pair106_direct_identity(
            route,
            message,
        )
        for message in messages
    ]


    tokens = {
        "direct:"
        + alt_pair106_hash_identity_list(
            direct_identities
        )
    }


    forward_identities = [
        alt_pair106_forward_identity(
            message
        )
        for message in messages
    ]


    if (
        forward_identities
        and all(
            identity is not None
            for identity in forward_identities
        )
    ):

        tokens.add(
            "original:"
            + alt_pair106_hash_identity_list(
                forward_identities
            )
        )


        # Also index the forward origin using the SAME token
        # namespace as a direct source post.
        #
        # This is what makes:
        #
        # direct A post == B's forward of A post.
        tokens.add(
            "direct:"
            + alt_pair106_hash_identity_list(
                forward_identities
            )
        )


    return sorted(
        tokens
    )


def alt_pair106_find_seen(
    tokens,
):

    for token in tokens:

        entry = ALT_PAIR106_SEEN.get(
            token
        )


        if isinstance(
            entry,
            dict,
        ):

            return token, entry


    return None, None


def alt_pair106_store_seen(
    tokens,
    route,
    messages,
    destination_ids,
):

    entry = {
        "winner_source_chat": int(
            route["source_chat"]
        ),
        "winner_source_ids": [
            int(message.id)
            for message in messages
        ],
        "destination_ids": [
            int(value)
            for value in destination_ids
        ],
        "created_at": time.time(),
    }


    for token in tokens:

        ALT_PAIR106_SEEN[
            token
        ] = entry


    save_json(
        ALT_PAIR106_SEEN_FILE,
        ALT_PAIR106_SEEN,
    )


def alt_pair106_alias_mapping(
    route,
    messages,
    entry,
):
    """
    A duplicate is NOT sent.

    But we alias its source IDs to the already-existing relay
    message IDs. This means the normal edit machinery can still
    find the destination copy for the duplicate source post.
    """

    destination_ids = [
        int(value)
        for value in entry.get(
            "destination_ids",
            [],
        )
    ]


    messages = sorted(
        list(messages),
        key=lambda item: int(
            item.id
        ),
    )


    if (
        len(destination_ids)
        != len(messages)
    ):

        return False


    for message, destination_id in zip(
        messages,
        destination_ids,
    ):

        message_map[
            map_key(
                route,
                message.id,
            )
        ] = int(
            destination_id
        )


    save_json(
        MAP_FILE,
        message_map,
    )


    update_checkpoint(
        route,
        max(
            int(message.id)
            for message in messages
        ),
    )


    return True


async def copy_unit_pair_deduped(
    route,
    messages,
    reason,
):

    if not alt_pair106_route(
        route
    ):

        return await copy_unit(
            route,
            messages,
            reason,
        )


    route_identity = route_key(
        route
    )


    # --------------------------------------------------------
    # STRICT FIRST-START NO-HISTORY GATE
    # --------------------------------------------------------

    if (
        route_identity
        not in ALT_PAIR106_READY_ROUTE_KEYS
    ):

        log.warning(
            "[ALT PAIR106 BEFORE BASELINE SKIPPED] "
            f"source={route['source_chat']} "
            f"ids="
            f"{[int(m.id) for m in messages]} "
            f"reason={reason} "
            "COPIED=False "
            "PAST_IMPORT_BLOCK=True"
        )

        return False


    messages = sorted(
        list(messages),
        key=lambda item: int(
            item.id
        ),
    )


    tokens = alt_pair106_candidate_tokens(
        route,
        messages,
    )


    async with ALT_PAIR106_LOCK:

        matched_token, seen_entry = (
            alt_pair106_find_seen(
                tokens
            )
        )


        if seen_entry:

            alias_ok = (
                alt_pair106_alias_mapping(
                    route,
                    messages,
                    seen_entry,
                )
            )


            log.warning(
                "[ALT PAIR106 DUPLICATE IGNORED] "
                f"source={route['source_chat']} "
                f"ids="
                f"{[int(m.id) for m in messages]} "
                f"winner_source="
                f"{seen_entry.get('winner_source_chat')} "
                f"winner_ids="
                f"{seen_entry.get('winner_source_ids')} "
                f"dest_ids="
                f"{seen_entry.get('destination_ids')} "
                f"token={matched_token} "
                f"alias_mapping={alias_ok} "
                "SENT=False "
                "FIRST_COPY_WINS=True"
            )


            return True


        # ----------------------------------------------------
        # FIRST COPY WINS
        # ----------------------------------------------------

        success = await copy_unit(
            route,
            messages,
            reason,
        )


        if not success:

            return False


        destination_ids = []


        for message in messages:

            ids = mapped_ids(
                route,
                message.id,
            )


            if not ids:

                # Nothing was actually sent/mapped
                # (e.g. unsupported empty service message).
                return True


            destination_ids.append(
                int(ids[0])
            )


        alt_pair106_store_seen(
            tokens,
            route,
            messages,
            destination_ids,
        )


        log.warning(
            "[ALT PAIR106 FIRST COPY WON] "
            f"source={route['source_chat']} "
            f"ids="
            f"{[int(m.id) for m in messages]} "
            f"dest_ids={destination_ids} "
            f"tokens={tokens} "
            "FIRST_COPY_WINS=True"
        )


        return True


async def initialise_alt_pair106_no_history():
    """
    First installation:

      snapshot current newest message
      save checkpoint
      copy NOTHING

    Future restart:

      restore durable checkpoint
      watchdog can recover only genuinely newer messages.
    """

    pair_routes = [
        route
        for route in ROUTES
        if alt_pair106_route(
            route
        )
    ]


    if len(pair_routes) != 2:

        raise RuntimeError(
            "ALT PAIR106 expected exactly 2 routes; "
            f"found={len(pair_routes)}"
        )


    for route in pair_routes:

        key = route_key(
            route
        )


        stored = checkpoint(
            route
        )


        if stored > 0:

            ALT_PAIR106_READY_ROUTE_KEYS.add(
                key
            )


            log.warning(
                "[ALT PAIR106 BASELINE RESTORED] "
                f"source={route['source_chat']} "
                f"checkpoint={stored} "
                "COPIED_HISTORY=0 "
                "RESTART_RECOVERY=True"
            )


            continue


        current = list(
            await fetch_route_messages(
                route,
                50,
            )
            or []
        )


        baseline = max(
            [
                int(message.id)
                for message in current
            ]
            or [0]
        )


        route_state[
            key
        ] = int(
            baseline
        )


        save_json(
            STATE_FILE,
            route_state,
        )


        ALT_PAIR106_READY_ROUTE_KEYS.add(
            key
        )


        log.warning(
            "[ALT PAIR106 FIRST BASELINE] "
            f"source={route['source_chat']} "
            f"baseline={baseline} "
            "COPIED_HISTORY=0 "
            "PAST_IMPORT_BLOCK=True"
        )


    log.warning(
        "[ALT PAIR106 DEDUPE READY] "
        "sources=[-1003252087470,-1002438454194] "
        "dest=-1004367822325_106 "
        "MODE=TELEGRAM_ORIGINAL_FIRST_COPY_WINS "
        "PERSISTENT=True "
        "NO_HISTORY=True"
    )


# END ALT_PAIR106_ORIGINAL_DEDUPE_V1


# ==============================================================
# TELEGRAM PUSH EVENTS
# ==============================================================

@client.on(
    events.NewMessage(
        chats=SOURCE_CHATS
    )
)
async def new_message_handler(event):
    try:
        message = event.message

        # Album event owns grouped messages.
        if getattr(
            message,
            "grouped_id",
            None,
        ):
            return

        routes = matching_routes(
            event.chat_id,
            message,
        )

        if not routes:
            return

        await asyncio.gather(
            *[
                copy_unit_pair_deduped(
                    route,
                    [message],
                    "telegram_push",
                )
                for route in routes
            ]
        )

    except Exception as exc:
        log.exception(
            "[ALT NEW MESSAGE HANDLER ERROR] "
            f"{type(exc).__name__}: {exc}"
        )


@client.on(
    events.Album(
        chats=SOURCE_CHATS
    )
)
async def album_handler(event):
    try:
        messages = sorted(
            list(
                event.messages
                or []
            ),
            key=lambda message: int(
                message.id
            ),
        )

        if not messages:
            return

        routes = matching_routes(
            event.chat_id,
            messages[0],
        )

        if not routes:
            return

        await asyncio.gather(
            *[
                copy_unit_pair_deduped(
                    route,
                    messages,
                    "telegram_album",
                )
                for route in routes
            ]
        )

    except Exception as exc:
        log.exception(
            "[ALT ALBUM HANDLER ERROR] "
            f"{type(exc).__name__}: {exc}"
        )


@client.on(
    events.MessageEdited(
        chats=SOURCE_CHATS
    )
)
async def edited_handler(event):
    try:
        routes = matching_routes(
            event.chat_id,
            event.message,
        )

        if not routes:
            return

        await asyncio.gather(
            *[
                edit_existing(
                    route,
                    event.message,
                )
                for route in routes
            ]
        )

    except Exception as exc:
        log.exception(
            "[ALT EDIT HANDLER ERROR] "
            f"{type(exc).__name__}: {exc}"
        )


# ==============================================================
# WATCHDOG / DEPLOYMENT RECOVERY
# ==============================================================

async def fetch_route_messages(
    route,
    limit,
):
    if (
        route["source_topic"]
        is not None
    ):
        # Exact topic only. Never whole-chat fallback.
        return await client.get_messages(
            route["source_chat"],
            limit=int(limit),
            reply_to=int(
                route["source_topic"]
            ),
        )

    return await client.get_messages(
        route["source_chat"],
        limit=int(limit),
    )


def build_units(messages):
    messages = sorted(
        list(messages),
        key=lambda message: int(
            message.id
        ),
    )

    units = []
    used_ids = set()

    for message in messages:
        message_id = int(
            message.id
        )

        if message_id in used_ids:
            continue

        grouped_id = getattr(
            message,
            "grouped_id",
            None,
        )

        if grouped_id:
            unit = [
                candidate
                for candidate in messages
                if getattr(
                    candidate,
                    "grouped_id",
                    None,
                )
                == grouped_id
            ]
        else:
            unit = [message]

        unit = sorted(
            unit,
            key=lambda item: int(
                item.id
            ),
        )

        for item in unit:
            used_ids.add(
                int(item.id)
            )

        units.append(unit)

    return units


async def watchdog(route):
    stored_checkpoint = checkpoint(
        route
    )

    # ----------------------------------------------------------
    # FIRST EVER START
    #
    # No persistent checkpoint exists:
    # Snapshot current newest message and copy NOTHING.
    # ----------------------------------------------------------

    if stored_checkpoint <= 0:
        while True:
            try:
                current = list(
                    await fetch_route_messages(
                        route,
                        50,
                    )
                    or []
                )

                stored_checkpoint = max(
                    [
                        int(message.id)
                        for message in current
                    ]
                    or [0]
                )

                route_state[
                    route_key(route)
                ] = stored_checkpoint

                save_json(
                    STATE_FILE,
                    route_state,
                )

                log.warning(
                    "[ALT WATCHDOG FIRST BASELINE] "
                    f"route={route['name']} "
                    f"baseline={stored_checkpoint} "
                    "COPIED_HISTORY=0"
                )

                break

            except Exception as exc:
                log.warning(
                    "[ALT WATCHDOG BASELINE FAILED] "
                    f"{type(exc).__name__}: {exc}"
                )

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

    last_id = int(
        stored_checkpoint
    )

    while True:
        await asyncio.sleep(
            WATCHDOG_SECONDS
        )

        try:
            current = list(
                await fetch_route_messages(
                    route,
                    WATCHDOG_LIMIT,
                )
                or []
            )

            fresh = [
                message
                for message in current
                if int(message.id) > last_id
            ]

            for unit in build_units(
                fresh
            ):
                if not unit:
                    continue

                # Give Telegram a moment to finish albums.
                grouped_id = getattr(
                    unit[0],
                    "grouped_id",
                    None,
                )

                if grouped_id:
                    timestamps = []

                    for item in unit:
                        value = getattr(
                            item,
                            "date",
                            None,
                        )

                        if value:
                            try:
                                timestamps.append(
                                    float(
                                        value.timestamp()
                                    )
                                )
                            except Exception:
                                pass

                    if (
                        timestamps
                        and (
                            time.time()
                            - max(timestamps)
                        ) < 1.5
                    ):
                        break

                max_id = max(
                    int(message.id)
                    for message in unit
                )

                success = await copy_unit_pair_deduped(
                    route,
                    unit,
                    "watchdog_recovery",
                )

                if not success:
                    # Preserve ordering.
                    break

                last_id = max(
                    last_id,
                    max_id,
                )

                update_checkpoint(
                    route,
                    last_id,
                )

        except FloodWaitError as exc:
            wait_for = max(
                1,
                int(exc.seconds),
            )

            log.warning(
                "[ALT WATCHDOG FLOODWAIT] "
                f"wait={wait_for}s "
                "LIVE_PUSH_UNAFFECTED=True"
            )

            await asyncio.sleep(
                wait_for
            )

        except Exception as exc:
            log.warning(
                "[ALT WATCHDOG ERROR] "
                f"{type(exc).__name__}: {exc} "
                "LIVE_PUSH_UNAFFECTED=True"
            )




# BEGIN ALT_ARABIC115_LAST50_V1

ALT_ARABIC115_LAST50_ONCE = (
    os.environ.get(
        "ALT_ARABIC115_LAST50_ONCE",
        "0",
    ).strip()
    == "1"
)

ALT_ARABIC115_LAST50_COUNT = max(
    1,
    int(
        os.environ.get(
            "ALT_ARABIC115_LAST50_COUNT",
            "50",
        )
    ),
)

ALT_ARABIC115_LAST50_FETCH_LIMIT = max(
    200,
    int(
        os.environ.get(
            "ALT_ARABIC115_LAST50_FETCH_LIMIT",
            "1000",
        )
    ),
)

ALT_ARABIC115_LAST50_DONE_FILE = (
    DATA_DIR
    / "arabic115_last50_v1.done.json"
)

ALT_ARABIC115_LAST50_PROGRESS_FILE = (
    DATA_DIR
    / "arabic115_last50_v1.progress.json"
)


def alt_arabic115_target_route():

    matches = [
        route
        for route in ROUTES
        if alt_arabic115_route(
            route
        )
    ]


    if len(matches) != 1:

        raise RuntimeError(
            "ARABIC115 expected exactly one route; "
            f"found={len(matches)}"
        )


    return matches[0]


def alt_arabic115_copyable(
    unit,
):

    return any(
        bool(
            text_of(
                message
            )
        )
        or real_media(
            message
        )
        for message in unit
    )


def alt_arabic115_unit_token(
    ids,
):

    return ",".join(
        str(
            int(value)
        )
        for value in ids
    )


async def alt_arabic115_fetch_unit(
    route,
    ids,
):

    raw = await client.get_messages(
        route["source_chat"],
        ids=[
            int(value)
            for value in ids
        ],
    )


    if isinstance(
        raw,
        list,
    ):

        messages = [
            message
            for message in raw
            if message is not None
        ]

    else:

        messages = (
            [raw]
            if raw is not None
            else []
        )


    messages = sorted(
        messages,
        key=lambda message: int(
            message.id
        ),
    )


    if (
        len(messages)
        != len(ids)
    ):

        raise RuntimeError(
            "History source unit could not be "
            f"reloaded completely ids={ids}"
        )


    return messages


async def run_alt_arabic115_last50_once():
    """
    The selected history posts are passed through copy_unit().

    Therefore history uses EXACTLY the same:
      - username filtering
      - Arabic detection
      - Telegram translation
      - media handling
      - album handling
      - message mapping

    as a live message.
    """

    if not ALT_ARABIC115_LAST50_ONCE:

        return False


    route = alt_arabic115_target_route()


    if ALT_ARABIC115_LAST50_DONE_FILE.exists():

        log.warning(
            "[ALT ARABIC115 HISTORY ALREADY DONE] "
            "RESENT=False"
        )

        return True


    me = await client.get_me()


    progress = load_json(
        ALT_ARABIC115_LAST50_PROGRESS_FILE
    )


    selected_units = progress.get(
        "selected_units"
    )


    # --------------------------------------------------------
    # FIRST RUN: SNAPSHOT LATEST 50 POSTS
    # --------------------------------------------------------

    if not isinstance(
        selected_units,
        list,
    ) or not selected_units:

        fetched = list(
            await fetch_route_messages(
                route,
                ALT_ARABIC115_LAST50_FETCH_LIMIT,
            )
            or []
        )


        if not fetched:

            raise RuntimeError(
                "Arabic VIP source returned zero messages"
            )


        cutoff_id = max(
            int(message.id)
            for message in fetched
        )


        units = build_units(
            fetched
        )


        units = [
            unit
            for unit in units
            if (
                unit
                and alt_arabic115_copyable(
                    unit
                )
                and max(
                    int(message.id)
                    for message in unit
                )
                <= cutoff_id
            )
        ]


        selected = units[
            -ALT_ARABIC115_LAST50_COUNT:
        ]


        selected = sorted(
            selected,
            key=lambda unit: min(
                int(message.id)
                for message in unit
            ),
        )


        if not selected:

            raise RuntimeError(
                "No copyable Arabic VIP history posts"
            )


        selected_units = [
            [
                int(message.id)
                for message in unit
            ]
            for unit in selected
        ]


        progress = {
            "status": "running",
            "source_chat": (
                ALT_ARABIC115_SOURCE_CHAT
            ),
            "dest_chat": (
                ALT_ARABIC115_DEST_CHAT
            ),
            "dest_topic": (
                ALT_ARABIC115_DEST_TOPIC
            ),
            "requested": (
                ALT_ARABIC115_LAST50_COUNT
            ),
            "selected": len(
                selected_units
            ),
            "selected_units": (
                selected_units
            ),
            "cutoff_id": cutoff_id,
            "completed_tokens": [],
            "started_at": time.time(),
        }


        save_json(
            ALT_ARABIC115_LAST50_PROGRESS_FILE,
            progress,
        )


    completed_tokens = set(
        str(value)
        for value in progress.get(
            "completed_tokens",
            [],
        )
    )


    log.warning(
        "[ALT ARABIC115 HISTORY START] "
        f"premium="
        f"{bool(getattr(me, 'premium', False))} "
        f"requested={ALT_ARABIC115_LAST50_COUNT} "
        f"selected={len(selected_units)} "
        f"resume={len(completed_tokens)} "
        "ORDER=OLDEST_TO_NEWEST "
        "SAME_PATH_AS_LIVE=True "
        "ARABIC_TO_ENGLISH=True"
    )


    completed = len(
        completed_tokens
    )


    for index, ids in enumerate(
        selected_units,
        start=1,
    ):

        ids = [
            int(value)
            for value in ids
        ]


        token = alt_arabic115_unit_token(
            ids
        )


        if token in completed_tokens:

            continue


        success = False


        retry_delays = [
            0,
            2,
            5,
            10,
            20,
        ]


        for attempt, delay in enumerate(
            retry_delays,
            start=1,
        ):

            if delay:

                await asyncio.sleep(
                    delay
                )


            messages = (
                await alt_arabic115_fetch_unit(
                    route,
                    ids,
                )
            )


            success = await copy_unit(
                route,
                messages,
                "arabic115_history_live_path",
            )


            if success:

                break


            log.warning(
                "[ALT ARABIC115 HISTORY RETRY] "
                f"index={index}/{len(selected_units)} "
                f"ids={ids} "
                f"attempt={attempt}"
            )


        if not success:

            progress.update({
                "status": "failed",
                "failed_index": index,
                "failed_ids": ids,
                "completed": completed,
                "failed_at": time.time(),
            })


            save_json(
                ALT_ARABIC115_LAST50_PROGRESS_FILE,
                progress,
            )


            raise RuntimeError(
                "Arabic115 history stopped safely "
                f"at index={index} ids={ids}"
            )


        completed_tokens.add(
            token
        )

        completed += 1


        progress.update({
            "status": "running",
            "completed": completed,
            "completed_tokens": sorted(
                completed_tokens
            ),
            "last_completed_ids": ids,
            "updated_at": time.time(),
        })


        save_json(
            ALT_ARABIC115_LAST50_PROGRESS_FILE,
            progress,
        )


        log.warning(
            "[ALT ARABIC115 HISTORY PROGRESS] "
            f"{completed}/{len(selected_units)} "
            f"source_ids={ids}"
        )


        await asyncio.sleep(
            0.35
        )


    done = {
        "status": "done",
        "source_chat": (
            ALT_ARABIC115_SOURCE_CHAT
        ),
        "dest_chat": (
            ALT_ARABIC115_DEST_CHAT
        ),
        "dest_topic": (
            ALT_ARABIC115_DEST_TOPIC
        ),
        "requested": (
            ALT_ARABIC115_LAST50_COUNT
        ),
        "selected": len(
            selected_units
        ),
        "completed": completed,
        "completed_at": time.time(),
        "same_path_as_live": True,
        "translated_to": "en",
    }


    save_json(
        ALT_ARABIC115_LAST50_DONE_FILE,
        done,
    )


    progress.update({
        "status": "done",
        "completed": completed,
        "completed_at": time.time(),
    })


    save_json(
        ALT_ARABIC115_LAST50_PROGRESS_FILE,
        progress,
    )


    log.warning(
        "[ALT ARABIC115 HISTORY DONE] "
        f"selected={len(selected_units)} "
        f"completed={completed} "
        "ORDER=OLDEST_TO_NEWEST "
        "SAME_PATH_AS_LIVE=True "
        "ARABIC_TO_ENGLISH=True "
        "RERUN_BLOCKED=True"
    )


    return True


# END ALT_ARABIC115_LAST50_V1


# BEGIN ALT_LAST50_ONCE_HELPER_V1

def alt_last50_target_route():
    matches = [
        route
        for route in ROUTES
        if (
            int(route["source_chat"])
            == ALT_LAST50_SOURCE_CHAT
            and route["source_topic"] is None
            and int(route["dest_chat"])
            == ALT_LAST50_DEST_CHAT
            and int(route["dest_topic"])
            == ALT_LAST50_DEST_TOPIC
        )
    ]

    if len(matches) != 1:
        raise RuntimeError(
            "ALT last50 expected exactly one route "
            f"but found {len(matches)}"
        )

    return matches[0]


def alt_last50_unit_copyable(unit):
    return any(
        bool(text_of(message))
        or real_media(message)
        for message in unit
    )


async def run_alt_last50_once():
    if not ALT_LAST50_ONCE:
        return False


    route = alt_last50_target_route()


    # --------------------------------------------------------
    # DURABLE ONE-TIME GUARD
    # --------------------------------------------------------

    if ALT_LAST50_DONE_FILE.exists():
        log.warning(
            "[ALT LAST50 ALREADY DONE] "
            f"done_file={ALT_LAST50_DONE_FILE} "
            "COPIED_AGAIN=False"
        )

        return True


    log.warning(
        "[ALT LAST50 STARTING] "
        f"source={ALT_LAST50_SOURCE_CHAT} "
        f"dest={ALT_LAST50_DEST_CHAT}_"
        f"{ALT_LAST50_DEST_TOPIC} "
        f"requested={ALT_LAST50_COUNT} "
        "LIVE_FORWARDING_CONTINUES=True"
    )


    # --------------------------------------------------------
    # SNAPSHOT SOURCE
    #
    # Fetch ONCE. Anything Telegram sends after this snapshot
    # belongs to the normal live NewMessage handler.
    # --------------------------------------------------------

    fetched = list(
        await fetch_route_messages(
            route,
            ALT_LAST50_FETCH_LIMIT,
        )
        or []
    )


    if not fetched:
        raise RuntimeError(
            "ALT last50 source returned zero messages"
        )


    cutoff_id = max(
        int(message.id)
        for message in fetched
    )


    units = build_units(
        fetched
    )


    copyable_units = [
        unit
        for unit in units
        if (
            unit
            and alt_last50_unit_copyable(
                unit
            )
            and max(
                int(message.id)
                for message in unit
            )
            <= cutoff_id
        )
    ]


    selected = copyable_units[
        -ALT_LAST50_COUNT:
    ]


    if not selected:
        raise RuntimeError(
            "ALT last50 found no copyable posts"
        )


    # build_units() is oldest -> newest.
    selected = sorted(
        selected,
        key=lambda unit: min(
            int(message.id)
            for message in unit
        ),
    )


    already_present = 0

    for unit in selected:
        if all(
            bool(
                mapped_ids(
                    route,
                    message.id,
                )
            )
            for message in unit
        ):
            already_present += 1


    progress = {
        "status": "running",
        "requested_posts": ALT_LAST50_COUNT,
        "selected_posts": len(selected),
        "source_chat": ALT_LAST50_SOURCE_CHAT,
        "dest_chat": ALT_LAST50_DEST_CHAT,
        "dest_topic": ALT_LAST50_DEST_TOPIC,
        "cutoff_id": cutoff_id,
        "already_present_before_start": already_present,
        "completed_posts": 0,
        "started_at": time.time(),
    }


    save_json(
        ALT_LAST50_PROGRESS_FILE,
        progress,
    )


    log.warning(
        "[ALT LAST50 SNAPSHOT] "
        f"cutoff_id={cutoff_id} "
        f"selected_posts={len(selected)} "
        f"already_present={already_present} "
        "ORDER=OLDEST_TO_NEWEST"
    )


    completed = 0


    # --------------------------------------------------------
    # COPY OLDEST -> NEWEST
    #
    # copy_unit() provides:
    #   - message-map duplicate prevention
    #   - album safety
    #   - persistent mapping
    #   - Telegram media handling
    # --------------------------------------------------------

    for index, unit in enumerate(
        selected,
        start=1,
    ):

        ids = [
            int(message.id)
            for message in unit
        ]


        success = False


        # A failed send is retried conservatively.
        retry_waits = [
            0,
            5,
            15,
            30,
            60,
        ]


        for attempt, wait_for in enumerate(
            retry_waits,
            start=1,
        ):

            if wait_for:
                await asyncio.sleep(
                    wait_for
                )


            success = await copy_unit(
                route,
                unit,
                "history_last50_once",
            )


            if success:
                break


            log.warning(
                "[ALT LAST50 RETRY] "
                f"index={index}/{len(selected)} "
                f"ids={ids} "
                f"attempt={attempt}"
            )


        if not success:
            progress.update({
                "status": "failed",
                "completed_posts": completed,
                "failed_ids": ids,
                "failed_at": time.time(),
            })

            save_json(
                ALT_LAST50_PROGRESS_FILE,
                progress,
            )

            raise RuntimeError(
                "ALT last50 stopped safely at "
                f"index={index} ids={ids}"
            )


        completed += 1


        progress.update({
            "status": "running",
            "completed_posts": completed,
            "last_completed_ids": ids,
            "updated_at": time.time(),
        })


        save_json(
            ALT_LAST50_PROGRESS_FILE,
            progress,
        )


        log.warning(
            "[ALT LAST50 PROGRESS] "
            f"{completed}/{len(selected)} "
            f"ids={ids}"
        )


        # Small pacing delay to reduce Telegram flood pressure.
        await asyncio.sleep(
            0.35
        )


    # --------------------------------------------------------
    # DURABLE DONE FLAG
    # --------------------------------------------------------

    done = {
        "status": "done",
        "requested_posts": ALT_LAST50_COUNT,
        "selected_posts": len(selected),
        "completed_posts": completed,
        "already_present_before_start": already_present,
        "source_chat": ALT_LAST50_SOURCE_CHAT,
        "dest_chat": ALT_LAST50_DEST_CHAT,
        "dest_topic": ALT_LAST50_DEST_TOPIC,
        "cutoff_id": cutoff_id,
        "completed_at": time.time(),
    }


    save_json(
        ALT_LAST50_DONE_FILE,
        done,
    )


    progress.update({
        "status": "done",
        "completed_posts": completed,
        "completed_at": time.time(),
    })


    save_json(
        ALT_LAST50_PROGRESS_FILE,
        progress,
    )


    log.warning(
        "[ALT LAST50 DONE] "
        f"requested={ALT_LAST50_COUNT} "
        f"selected={len(selected)} "
        f"completed={completed} "
        f"already_present={already_present} "
        f"cutoff_id={cutoff_id} "
        "LIVE_FORWARDING=True "
        "RERUN_BLOCKED=True"
    )


    return True

# END ALT_LAST50_ONCE_HELPER_V1



# BEGIN ALT_PREMIUM_HISTORY_ONCE_HELPER_V1


def alt_history_safe_run_id():
    value = re.sub(
        r"[^A-Za-z0-9_.-]+",
        "_",
        ALT_HISTORY_RUN_ID,
    ).strip("._")

    if not value:
        raise RuntimeError(
            "ALT_HISTORY_RUN_ID is empty/invalid"
        )

    return value


def alt_history_done_file():
    return (
        DATA_DIR
        / (
            "history_"
            + alt_history_safe_run_id()
            + ".done.json"
        )
    )


def alt_history_progress_file():
    return (
        DATA_DIR
        / (
            "history_"
            + alt_history_safe_run_id()
            + ".progress.json"
        )
    )


def alt_history_route():
    matches = [
        route
        for route in ROUTES
        if (
            int(route["source_chat"])
            == ALT_HISTORY_SOURCE_CHAT
            and route["source_topic"] is None
            and int(route["dest_chat"])
            == ALT_HISTORY_DEST_CHAT
            and int(route["dest_topic"])
            == ALT_HISTORY_DEST_TOPIC
        )
    ]

    if len(matches) != 1:
        raise RuntimeError(
            "Premium history expected exactly one matching "
            f"route; found={len(matches)}"
        )

    return matches[0]


def alt_history_copyable(unit):
    return any(
        bool(text_of(message))
        or real_media(message)
        for message in unit
    )


async def run_alt_history_once():

    if not ALT_HISTORY_ONCE:
        return False


    route = alt_history_route()

    done_file = alt_history_done_file()
    progress_file = alt_history_progress_file()


    # --------------------------------------------------------
    # ONE-TIME DURABLE BLOCK
    # --------------------------------------------------------

    if done_file.exists():

        log.warning(
            "[ALT HISTORY ALREADY DONE] "
            f"run_id={ALT_HISTORY_RUN_ID} "
            "RERUN=False"
        )

        return True


    # --------------------------------------------------------
    # PREMIUM CHECK BEFORE SENDING ANYTHING
    # --------------------------------------------------------

    me = await client.get_me()

    premium = bool(
        getattr(
            me,
            "premium",
            False,
        )
    )


    log.warning(
        "[ALT HISTORY PREMIUM CHECK] "
        f"run_id={ALT_HISTORY_RUN_ID} "
        f"premium={premium}"
    )


    if (
        ALT_HISTORY_REQUIRE_PREMIUM
        and not premium
    ):

        raise RuntimeError(
            "Telegram reports ALT account Premium=False. "
            "Nothing was sent."
        )


    # --------------------------------------------------------
    # SNAPSHOT CURRENT SOURCE
    # --------------------------------------------------------

    fetched = list(
        await fetch_route_messages(
            route,
            ALT_HISTORY_FETCH_LIMIT,
        )
        or []
    )


    if not fetched:
        raise RuntimeError(
            "Source returned zero messages"
        )


    # Snapshot cutoff means anything newer is owned only by
    # normal live forwarding.
    cutoff_id = max(
        int(message.id)
        for message in fetched
    )


    units = build_units(
        fetched
    )


    units = [
        unit
        for unit in units
        if (
            unit
            and alt_history_copyable(unit)
            and max(
                int(message.id)
                for message in unit
            )
            <= cutoff_id
        )
    ]


    # Latest N posts/units.
    selected = units[
        -ALT_HISTORY_COUNT:
    ]


    selected = sorted(
        selected,
        key=lambda unit: min(
            int(message.id)
            for message in unit
        ),
    )


    if not selected:
        raise RuntimeError(
            "No copyable history posts found"
        )


    already_mapped = sum(
        1
        for unit in selected
        if all(
            bool(
                mapped_ids(
                    route,
                    message.id,
                )
            )
            for message in unit
        )
    )


    progress = {
        "run_id": ALT_HISTORY_RUN_ID,
        "status": "running",
        "premium": premium,
        "requested": ALT_HISTORY_COUNT,
        "selected": len(selected),
        "already_mapped": already_mapped,
        "completed": 0,
        "cutoff_id": cutoff_id,
        "started_at": time.time(),
    }


    save_json(
        progress_file,
        progress,
    )


    log.warning(
        "[ALT HISTORY START] "
        f"run_id={ALT_HISTORY_RUN_ID} "
        f"premium={premium} "
        f"requested={ALT_HISTORY_COUNT} "
        f"selected={len(selected)} "
        f"already_mapped={already_mapped} "
        f"cutoff_id={cutoff_id} "
        "ORDER=OLDEST_TO_NEWEST "
        "LIVE_FORWARDING=True"
    )


    completed = 0


    # --------------------------------------------------------
    # COPY
    # --------------------------------------------------------

    for index, unit in enumerate(
        selected,
        start=1,
    ):

        ids = [
            int(message.id)
            for message in unit
        ]


        success = False


        # Conservative retry ladder.
        delays = [
            0,
            3,
            10,
            30,
            60,
        ]


        for attempt, delay in enumerate(
            delays,
            start=1,
        ):

            if delay:
                await asyncio.sleep(
                    delay
                )


            success = await copy_unit(
                route,
                unit,
                "premium_history_last50",
            )


            if success:
                break


            log.warning(
                "[ALT HISTORY RETRY] "
                f"run_id={ALT_HISTORY_RUN_ID} "
                f"index={index}/{len(selected)} "
                f"ids={ids} "
                f"attempt={attempt}"
            )


        if not success:

            progress.update({
                "status": "failed",
                "completed": completed,
                "failed_ids": ids,
                "failed_at": time.time(),
            })


            save_json(
                progress_file,
                progress,
            )


            raise RuntimeError(
                "Premium history failed safely at "
                f"index={index} ids={ids}"
            )


        completed += 1


        progress.update({
            "completed": completed,
            "last_ids": ids,
            "updated_at": time.time(),
        })


        save_json(
            progress_file,
            progress,
        )


        log.warning(
            "[ALT HISTORY PROGRESS] "
            f"run_id={ALT_HISTORY_RUN_ID} "
            f"{completed}/{len(selected)} "
            f"ids={ids}"
        )


        # Reduce flood pressure.
        await asyncio.sleep(
            0.65
        )


    # --------------------------------------------------------
    # DURABLE SUCCESS
    # --------------------------------------------------------

    result = {
        "run_id": ALT_HISTORY_RUN_ID,
        "status": "done",
        "premium": premium,
        "requested": ALT_HISTORY_COUNT,
        "selected": len(selected),
        "completed": completed,
        "already_mapped": already_mapped,
        "cutoff_id": cutoff_id,
        "completed_at": time.time(),
    }


    save_json(
        done_file,
        result,
    )


    progress.update({
        "status": "done",
        "completed": completed,
        "completed_at": time.time(),
    })


    save_json(
        progress_file,
        progress,
    )


    log.warning(
        "[ALT HISTORY DONE] "
        f"run_id={ALT_HISTORY_RUN_ID} "
        f"premium={premium} "
        f"requested={ALT_HISTORY_COUNT} "
        f"selected={len(selected)} "
        f"completed={completed} "
        f"already_mapped={already_mapped} "
        "RERUN_BLOCKED=True "
        "LIVE_FORWARDING=True"
    )


    return True


# END ALT_PREMIUM_HISTORY_ONCE_HELPER_V1



# BEGIN ALT_FORCE50_V3

ALT_FORCE50_ONCE = (
    os.environ.get(
        "ALT_FORCE50_ONCE",
        "0",
    ).strip()
    == "1"
)

ALT_FORCE50_RUN_ID = (
    os.environ.get(
        "ALT_FORCE50_RUN_ID",
        "",
    ).strip()
)

ALT_FORCE50_COUNT = max(
    1,
    int(
        os.environ.get(
            "ALT_FORCE50_COUNT",
            "50",
        )
    ),
)

ALT_FORCE50_FETCH_LIMIT = max(
    200,
    int(
        os.environ.get(
            "ALT_FORCE50_FETCH_LIMIT",
            "1000",
        )
    ),
)

ALT_FORCE50_SOURCE_CHAT = -1003364661276
ALT_FORCE50_DEST_CHAT = -1004367822325
ALT_FORCE50_DEST_TOPIC = 2


def alt_force50_clean_run_id():

    value = ALT_FORCE50_RUN_ID.strip()

    if not value:

        raise RuntimeError(
            "ALT_FORCE50_RUN_ID is empty"
        )


    value = "".join(
        character
        if (
            character.isalnum()
            or character in "._-"
        )
        else "_"
        for character in value
    )


    if not value:

        raise RuntimeError(
            "ALT_FORCE50_RUN_ID became empty"
        )


    return value


def alt_force50_done_file():

    return (
        DATA_DIR
        / (
            "force50_"
            + alt_force50_clean_run_id()
            + ".done.json"
        )
    )


def alt_force50_progress_file():

    return (
        DATA_DIR
        / (
            "force50_"
            + alt_force50_clean_run_id()
            + ".progress.json"
        )
    )


def alt_force50_route():

    matches = [
        route
        for route in ROUTES
        if (
            int(route["source_chat"])
            == ALT_FORCE50_SOURCE_CHAT
            and route["source_topic"] is None
            and int(route["dest_chat"])
            == ALT_FORCE50_DEST_CHAT
            and int(route["dest_topic"])
            == ALT_FORCE50_DEST_TOPIC
        )
    ]


    if len(matches) != 1:

        raise RuntimeError(
            "ALT FORCE50 expected exactly one route; "
            f"found={len(matches)}"
        )


    return matches[0]


def alt_force50_copyable(
    unit,
):

    return any(
        bool(
            text_of(message)
        )
        or real_media(
            message
        )
        for message in unit
    )


def alt_force50_token(
    unit,
):

    return ",".join(
        str(
            int(message.id)
        )
        for message in unit
    )


async def alt_force50_send_fresh(
    route,
    unit,
):
    """
    Intentionally sends directly through send_single/send_album.

    Existing historical message mappings are NOT consulted as
    a reason to skip this one-time resend.

    After Telegram confirms the new send, remember() updates the
    source -> destination mapping to the new relay message.
    """

    unit = sorted(
        list(unit),
        key=lambda message: int(
            message.id
        ),
    )


    if not unit:

        raise RuntimeError(
            "Force50 received empty unit"
        )


    old_mapping = {
        str(
            int(message.id)
        ): list(
            mapped_ids(
                route,
                message.id,
            )
        )
        for message in unit
    }


    async with copy_lock(
        route,
        unit,
    ):

        grouped_id = getattr(
            unit[0],
            "grouped_id",
            None,
        )


        # ----------------------------------------------------
        # ALBUM
        # ----------------------------------------------------

        if (
            len(unit) > 1
            and grouped_id
        ):

            sent_items = await send_album(
                route,
                unit,
            )


            if not sent_items:

                raise RuntimeError(
                    "Album send returned no destination messages"
                )


            if not isinstance(
                sent_items,
                list,
            ):

                sent_items = [
                    sent_items
                ]


            if (
                len(sent_items)
                != len(unit)
            ):

                raise RuntimeError(
                    "Album destination item count mismatch: "
                    f"source={len(unit)} "
                    f"dest={len(sent_items)}"
                )


            for source, destination in zip(
                unit,
                sent_items,
            ):

                remember(
                    route,
                    source,
                    destination,
                )


            return {
                "kind": "ALBUM",
                "old_mapping": old_mapping,
                "new_destination_ids": [
                    int(
                        destination.id
                    )
                    for destination in sent_items
                ],
            }


        # ----------------------------------------------------
        # SINGLE
        # ----------------------------------------------------

        message = unit[0]


        sent = await send_single(
            route,
            message,
        )


        if sent is None:

            raise RuntimeError(
                "Single send returned no destination message"
            )


        remember(
            route,
            message,
            sent,
        )


        return {
            "kind": "SINGLE",
            "old_mapping": old_mapping,
            "new_destination_ids": [
                int(
                    sent.id
                )
            ],
        }


async def run_alt_force50_v3():

    if not ALT_FORCE50_ONCE:

        return False


    route = alt_force50_route()

    done_file = alt_force50_done_file()
    progress_file = alt_force50_progress_file()


    # ========================================================
    # DURABLE COMPLETION GUARD
    # ========================================================

    if done_file.exists():

        log.warning(
            "[ALT FORCE50 V3 ALREADY DONE] "
            f"run_id={ALT_FORCE50_RUN_ID} "
            "NEW_SEND=False"
        )

        return True


    # ========================================================
    # PREMIUM MUST BE ACTIVE
    # ========================================================

    me = await client.get_me()

    premium = bool(
        getattr(
            me,
            "premium",
            False,
        )
    )


    log.warning(
        "[ALT FORCE50 V3 PREMIUM CHECK] "
        f"run_id={ALT_FORCE50_RUN_ID} "
        f"premium={premium}"
    )


    if not premium:

        raise RuntimeError(
            "Telegram reports Premium=False. "
            "Nothing was sent."
        )


    # ========================================================
    # SOURCE SNAPSHOT
    # ========================================================

    fetched = list(
        await fetch_route_messages(
            route,
            ALT_FORCE50_FETCH_LIMIT,
        )
        or []
    )


    if not fetched:

        raise RuntimeError(
            "Source returned zero messages"
        )


    cutoff_id = max(
        int(
            message.id
        )
        for message in fetched
    )


    units = build_units(
        fetched
    )


    units = [
        unit
        for unit in units
        if (
            unit
            and alt_force50_copyable(
                unit
            )
            and max(
                int(message.id)
                for message in unit
            )
            <= cutoff_id
        )
    ]


    # build_units sorts source IDs ascending.
    # Therefore last N units are the newest N posts.
    selected = units[
        -ALT_FORCE50_COUNT:
    ]


    selected = sorted(
        selected,
        key=lambda unit: min(
            int(message.id)
            for message in unit
        ),
    )


    if not selected:

        raise RuntimeError(
            "No copyable posts found"
        )


    mapped_before = sum(
        1
        for unit in selected
        if all(
            bool(
                mapped_ids(
                    route,
                    message.id,
                )
            )
            for message in unit
        )
    )


    # ========================================================
    # RESUME STATE
    # ========================================================

    progress = load_json(
        progress_file
    )


    if (
        progress.get("run_id")
        != ALT_FORCE50_RUN_ID
    ):

        progress = {
            "run_id": ALT_FORCE50_RUN_ID,
            "status": "running",
            "premium": premium,
            "requested": ALT_FORCE50_COUNT,
            "selected": len(selected),
            "mapped_before": mapped_before,
            "cutoff_id": cutoff_id,
            "sent_tokens": [],
            "started_at": time.time(),
        }


        save_json(
            progress_file,
            progress,
        )


    sent_tokens = set(
        progress.get(
            "sent_tokens",
            [],
        )
    )


    log.warning(
        "[ALT FORCE50 V3 START] "
        f"run_id={ALT_FORCE50_RUN_ID} "
        f"premium={premium} "
        f"requested={ALT_FORCE50_COUNT} "
        f"selected={len(selected)} "
        f"mapped_before={mapped_before} "
        f"resume_sent={len(sent_tokens)} "
        f"cutoff_id={cutoff_id} "
        "STALE_MAP_BYPASS=True "
        "ORDER=OLDEST_TO_NEWEST "
        "ACTUAL_NEW_SEND=True"
    )


    completed = len(
        sent_tokens
    )


    # ========================================================
    # FRESH SENDS
    # ========================================================

    for index, unit in enumerate(
        selected,
        start=1,
    ):

        token = alt_force50_token(
            unit
        )


        ids = [
            int(
                message.id
            )
            for message in unit
        ]


        if token in sent_tokens:

            log.warning(
                "[ALT FORCE50 V3 RESUME SKIP] "
                f"index={index}/{len(selected)} "
                f"ids={ids}"
            )

            continue


        result = None
        success = False


        for attempt in range(
            1,
            6,
        ):

            try:

                result = await alt_force50_send_fresh(
                    route,
                    unit,
                )


                success = True

                break


            except FloodWaitError as exc:

                wait_for = max(
                    1,
                    int(
                        exc.seconds
                    )
                    + 1,
                )


                log.warning(
                    "[ALT FORCE50 V3 FLOODWAIT] "
                    f"index={index}/{len(selected)} "
                    f"ids={ids} "
                    f"wait={wait_for}s"
                )


                await asyncio.sleep(
                    wait_for
                )


            except Exception as exc:

                if attempt >= 5:

                    log.exception(
                        "[ALT FORCE50 V3 SEND FAILED] "
                        f"index={index}/{len(selected)} "
                        f"ids={ids} "
                        f"{type(exc).__name__}: {exc}"
                    )

                    break


                delays = [
                    3,
                    8,
                    20,
                    45,
                ]


                wait_for = delays[
                    attempt - 1
                ]


                log.warning(
                    "[ALT FORCE50 V3 RETRY] "
                    f"index={index}/{len(selected)} "
                    f"ids={ids} "
                    f"attempt={attempt} "
                    f"wait={wait_for}s "
                    f"error={type(exc).__name__}: {exc}"
                )


                await asyncio.sleep(
                    wait_for
                )


        if not success:

            progress.update({
                "status": "failed",
                "failed_index": index,
                "failed_ids": ids,
                "failed_at": time.time(),
            })


            save_json(
                progress_file,
                progress,
            )


            raise RuntimeError(
                "Force50 stopped safely at "
                f"index={index} ids={ids}"
            )


        sent_tokens.add(
            token
        )


        completed += 1


        progress.update({
            "status": "running",
            "sent_tokens": sorted(
                sent_tokens
            ),
            "completed": completed,
            "last_source_ids": ids,
            "last_new_destination_ids": (
                result[
                    "new_destination_ids"
                ]
            ),
            "updated_at": time.time(),
        })


        save_json(
            progress_file,
            progress,
        )


        log.warning(
            "[ALT FORCE50 V3 SENT] "
            f"index={index}/{len(selected)} "
            f"source_ids={ids} "
            f"kind={result['kind']} "
            f"old_mapping={result['old_mapping']} "
            f"new_destination_ids="
            f"{result['new_destination_ids']} "
            "ACTUAL_NEW_SEND=True "
            "MAPPING_REPLACED=True"
        )


        # Conservative pacing.
        await asyncio.sleep(
            1.0
        )


    # ========================================================
    # DURABLE DONE
    # ========================================================

    done = {
        "run_id": ALT_FORCE50_RUN_ID,
        "status": "done",
        "premium": premium,
        "requested": ALT_FORCE50_COUNT,
        "selected": len(selected),
        "completed": completed,
        "mapped_before": mapped_before,
        "cutoff_id": cutoff_id,
        "completed_at": time.time(),
    }


    save_json(
        done_file,
        done,
    )


    progress.update({
        "status": "done",
        "completed": completed,
        "completed_at": time.time(),
    })


    save_json(
        progress_file,
        progress,
    )


    log.warning(
        "[ALT FORCE50 V3 DONE] "
        f"run_id={ALT_FORCE50_RUN_ID} "
        f"premium={premium} "
        f"selected={len(selected)} "
        f"completed={completed} "
        f"mapped_before={mapped_before} "
        "ACTUAL_NEW_SEND=True "
        "MAPPINGS_NOW_POINT_TO_NEW_COPIES=True "
        "RERUN_BLOCKED=True "
        "LIVE_FORWARDING=True"
    )


    return True


# END ALT_FORCE50_V3


# ==============================================================
# MAIN
# ==============================================================

async def main():
    if CONNECT_DELAY:
        log.warning(
            "[ALT CONNECT GUARD] "
            f"waiting={CONNECT_DELAY}s"
        )

        await asyncio.sleep(
            CONNECT_DELAY
        )

    await client.connect()

    if not await client.is_user_authorized():
        raise RuntimeError(
            "ALT Telegram StringSession is unauthorized"
        )

    me = await client.get_me()

    if (
        EXPECTED_USER_ID
        and int(me.id)
        != EXPECTED_USER_ID
    ):
        raise RuntimeError(
            "WRONG TELEGRAM ACCOUNT LOADED: "
            f"expected={EXPECTED_USER_ID} "
            f"actual={me.id}"
        )

    await client.get_dialogs(
        limit=None
    )

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

    for route in ROUTES:
        source = await client.get_entity(
            route["source_chat"]
        )

        destination = await client.get_entity(
            route["dest_chat"]
        )

        if (
            route["source_topic"]
            is not None
        ):
            await client.get_messages(
                route["source_chat"],
                limit=1,
                reply_to=int(
                    route["source_topic"]
                ),
            )

        topic_root = await client.get_messages(
            route["dest_chat"],
            ids=int(
                route["dest_topic"]
            ),
        )

        if not topic_root:
            raise RuntimeError(
                "Relay topic does not exist: "
                f"{route['dest_chat']}_"
                f"{route['dest_topic']}"
            )

        log.warning(
            "[ALT ROUTE ACCESS OK] "
            f"route={route['name']} "
            f"source_title="
            f"{getattr(source, 'title', None)!r} "
            f"dest_title="
            f"{getattr(destination, 'title', None)!r}"
        )

    # ----------------------------------------------------------
    # PAIR106 STRICT NO-HISTORY INITIALISATION
    # ----------------------------------------------------------

    await initialise_alt_pair106_no_history()

    # ----------------------------------------------------------
    # ARABIC115 GUARDED LAST50
    # ----------------------------------------------------------

    if ALT_ARABIC115_LAST50_ONCE:

        try:

            await run_alt_arabic115_last50_once()

        except Exception as exc:

            log.exception(
                "[ALT ARABIC115 HISTORY FAILED SAFE] "
                f"{type(exc).__name__}: {exc} "
                "LIVE_FORWARDING_CONTINUES=True "
                "UNTRANSLATED_FALLBACK=False"
            )

    # ----------------------------------------------------------
    # TRUE FORCE50 V3
    # ----------------------------------------------------------

    if ALT_FORCE50_ONCE:

        try:

            await run_alt_force50_v3()

        except Exception as exc:

            log.exception(
                "[ALT FORCE50 V3 FAILED SAFE] "
                f"run_id={ALT_FORCE50_RUN_ID} "
                f"{type(exc).__name__}: {exc} "
                "LIVE_FORWARDING_CONTINUES=True"
            )

    # ----------------------------------------------------------
    # GUARDED ONE-TIME LAST 50
    # ----------------------------------------------------------

    if ALT_LAST50_ONCE:
        try:
            await run_alt_last50_once()

        except Exception as exc:
            log.exception(
                "[ALT LAST50 FAILED SAFE] "
                f"{type(exc).__name__}: {exc} "
                "LIVE_FORWARDING_CONTINUES=True"
            )

    # ----------------------------------------------------------
    # OPTIONAL GUARDED HISTORY RUN
    # ----------------------------------------------------------

    if ALT_HISTORY_ONCE:

        try:

            await run_alt_history_once()

        except Exception as exc:

            log.exception(
                "[ALT HISTORY FAILED SAFE] "
                f"run_id={ALT_HISTORY_RUN_ID} "
                f"{type(exc).__name__}: {exc} "
                "LIVE_FORWARDING_CONTINUES=True"
            )

    # Recovery tasks.
    for route in ROUTES:
        asyncio.create_task(
            watchdog(route)
        )

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

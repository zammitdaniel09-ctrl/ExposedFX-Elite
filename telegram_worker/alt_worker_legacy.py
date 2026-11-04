import asyncio
import json
import logging
import os
import re
from pathlib import Path

from telethon import TelegramClient, events, functions
from telethon.errors import ChannelPrivateError
from telethon.sessions import StringSession
from telegram_worker import owner_forwarding_policy as owner_policy
from telegram_worker import route_table
from telegram_worker.state_store import SQLiteMap, open_message_map
from telegram_worker.engine.mirror import Mirror
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


def live_routes(routes):
    return [route for route in routes if not alt_vip5_block(route) and not owner_policy.is_stopped(route)]


def select_routes():
    """ALT's routes come from route_table.py.

    While Railway still has ALT_ROUTES_JSON, its live routes are compared with
    the table; if they differ, ALT keeps using ALT_ROUTES_JSON (the routes it
    ran with until now) and reports [ROUTE TABLE MISMATCH] to the owner.
    """
    table = live_routes(route_table.routes("ALT"))
    if not os.environ.get("ALT_ROUTES_JSON", "").strip():
        return table
    configured = live_routes(load_routes())

    def keys(routes):
        return sorted(
            (r["name"], r["source_chat"], r["source_topic"] or 0, r["dest_chat"], r["dest_topic"])
            for r in routes
        )

    if keys(table) == keys(configured):
        log.warning("[ROUTE TABLE CHECK] worker=ALT routes=%s matches_ALT_ROUTES_JSON=True", len(table))
        return table
    log.error(
        "[ROUTE TABLE MISMATCH] worker=ALT table=%s ALT_ROUTES_JSON=%s USING=ALT_ROUTES_JSON",
        keys(table),
        keys(configured),
    )
    return configured


ROUTES = select_routes()
log.warning(
    "[OWNER ALT STOP POLICY] stopped_source_chats=%s remaining_routes=%s",
    sorted(owner_policy.STOPPED_SOURCE_CHATS),
    len(ROUTES),
)

SOURCE_CHATS = sorted({route["source_chat"] for route in ROUTES})


# ==============================================================
# CLIENT
# ==============================================================

DELIVERY_ENGINE = None  # engine.alt.AltEngine, installed by alt_worker.main()
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
    if isinstance(value, SQLiteMap):
        value.snapshot_to(path)  # already durable in SQLite; refresh the rollback copy
        return
    temp = path.with_suffix(path.suffix + ".tmp")

    temp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")

    temp.replace(path)


message_map = open_message_map(DATA_DIR, MAP_FILE, load_legacy=lambda: load_json(MAP_FILE))

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


# ==============================================================
# MEDIA SENDERS
# ==============================================================


# ==============================================================
# FORWARD UNIT
# ==============================================================


async def _owner_original_copy_unit(route, messages, reason):
    """Copy one post on one route through the delivery engine (engine/alt.py)."""
    messages = sorted(list(messages), key=lambda message: int(message.id))
    if not messages:
        return True
    async with copy_lock(route, messages):
        return await DELIVERY_ENGINE.deliver(route, messages, reason)


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


# Edits are mirrored by engine/mirror.py (shared with MAIN): the copy is edited
# in place, translated like new posts; its media structure is never changed.
def edit_blocked(chat_id, message):
    if has_username_mention(message):
        log_username_filter_alt([message], "edit_existing")
        return True
    return False


MIRROR = Mirror(
    "ALT",
    client,
    message_map,
    translated_payload,
    blocked=edit_blocked,
    wait_for_map=10.0,
    same_media_only=True,
)


# ==============================================================
# TELEGRAM PUSH EVENTS
# ==============================================================


@client.on(events.MessageEdited(chats=SOURCE_CHATS))
async def edited_handler(event):
    try:
        await wait_dialogs_ready()
        await MIRROR.on_edit(event.chat_id, event.message, matching_routes(event.chat_id, event.message))
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


# BEGIN ENGINE_INTAKE_V1
# New posts reach the delivery engine through engine/intake.py (shared with
# MAIN): Telegram pushes first, a checkpointed watchdog behind them, and
# everything missed while ALT was down delivered in order, however old.
INTAKE = None


async def intake_deliver(route, unit, reason):
    if owner_policy.is_stopped(route):
        return True  # nothing left to deliver on a stopped route
    return bool(await copy_unit(route, unit, reason)) or owner_policy.is_stopped(route)


def start_engine_intake():
    from telegram_worker.engine.intake import Checkpoints, Intake, first_start_checkpoint, group_by_source

    global INTAKE

    async def first_checkpoint(source, routes):
        # First start on the intake: continue from the old per-route checkpoints.
        return await first_start_checkpoint(client, source, [checkpoint(r) for r in routes])

    INTAKE = Intake(
        "ALT",
        client,
        group_by_source([r for r in ROUTES if not owner_policy.is_stopped(r)]),
        intake_deliver,
        Checkpoints(message_map.db),
        topic_of,
        first_checkpoint,
        watchdog_seconds=WATCHDOG_SECONDS,
    )
    return INTAKE.start()


# END ENGINE_INTAKE_V1


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

    for route in list(ROUTES):
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

    # One-time history imports (ARABIC115 last50, FORCE50 v3, last50, guarded
    # history, route508 last50) were retired on 4 Nov 2026: every flag was off
    # in production and each import had finished. See archive/README.md.

    start_engine_intake()

    log.warning(
        "[ALT RELAY READY] "
        f"user_id={me.id} "
        f"routes={len(ROUTES)} "
        f"sources={SOURCE_CHATS} "
        "PRIMARY=TELEGRAM_PUSH INTAKE=engine "
        f"WATCHDOG={WATCHDOG_SECONDS}s "
        "FIRST_START_NO_HISTORY=True "
        "RESTART_RECOVERY=True "
        "EDIT_IN_PLACE=True "
        "PERSISTENT_MAPPING=True"
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())

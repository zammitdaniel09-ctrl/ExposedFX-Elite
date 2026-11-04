"""Frozen copy of the legacy MAIN delivery rules (worker_fixed.py, 4 Nov 2026).

The test oracle for tests/test_engine.py: the delivery engine must keep making
exactly the decisions these functions made. Never imported by production code.
Module globals they expect (log, re, os, BLOCKED_SENDER_IDS) are injected by
the test that executes this file.
"""


def mapped_ids_from_value(value):
    """
    Message map compatibility:
    old format = int
    reinforced format = list[int]
    accidental dict/list values are also handled safely.
    """
    if not value:
        return []

    if isinstance(value, list):
        out = []
        for x in value:
            try:
                out.append(int(x))
            except Exception:
                pass
        return out

    if isinstance(value, dict):
        out = []
        for x in value.values():
            try:
                out.append(int(x))
            except Exception:
                pass
        return out

    try:
        return [int(value)]
    except Exception:
        return []


def text_of(message):
    return message.message or message.raw_text or message.text or ""


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


def sender_ids_for_message(message):
    ids = set()

    for attr in ("sender_id", "from_id"):
        obj = getattr(message, attr, None)
        if isinstance(obj, int):
            ids.add(int(obj))
        else:
            for sub in ("user_id", "channel_id", "chat_id"):
                val = getattr(obj, sub, None)
                if val:
                    try:
                        ids.add(int(val))
                    except Exception:
                        pass

    fwd = getattr(message, "fwd_from", None)
    if fwd:
        for attr in ("from_id", "saved_from_peer"):
            obj = getattr(fwd, attr, None)
            if isinstance(obj, int):
                ids.add(int(obj))
            else:
                for sub in ("user_id", "channel_id", "chat_id"):
                    val = getattr(obj, sub, None)
                    if val:
                        try:
                            ids.add(int(val))
                        except Exception:
                            pass

    return ids


def _marked_peer_id(peer):
    """
    Convert Telethon PeerChannel / PeerChat IDs into the same
    marked format used by ROUTES, e.g. -1003252087470.
    """

    if peer is None:
        return None

    if isinstance(peer, int):
        return int(peer)

    channel_id = getattr(peer, "channel_id", None)

    if channel_id:
        try:
            return int("-100" + str(abs(int(channel_id))))
        except Exception:
            pass

    chat_id = getattr(peer, "chat_id", None)

    if chat_id:
        try:
            value = int(chat_id)
            return -abs(value)
        except Exception:
            pass

    user_id = getattr(peer, "user_id", None)

    if user_id:
        try:
            return int(user_id)
        except Exception:
            pass

    return None


def forward_origin_chat_ids(message):
    """
    IDs contained in Telegram's actual ForwardHeader.

    This detects:
        A forwards B
        B forwards A

    without relying only on matching message text.
    """

    out = set()

    fwd = getattr(message, "fwd_from", None)

    if not fwd:
        return out

    for attr in ("from_id", "saved_from_peer"):

        peer = getattr(fwd, attr, None)

        marked = _marked_peer_id(peer)

        if marked is not None:
            out.add(marked)

    return out


def should_block_private_peer_loop(message, route):
    peers_raw = route.get("loop_guard_peers") if isinstance(route, dict) else None

    if not peers_raw:
        return False

    peers = set()

    for value in peers_raw:
        try:
            peers.add(int(value))
        except Exception:
            pass

    origins = forward_origin_chat_ids(message)

    matched = sorted(origins & peers)

    if not matched:
        return False

    log.warning(
        "[PEER LOOP BLOCKED] "
        f"route={route.get('name')} "
        f"source={route.get('source_chat')}_"
        f"{route.get('source_topic')} "
        f"msg={getattr(message, 'id', None)} "
        f"forward_origins={sorted(origins)} "
        f"matched_peers={matched} "
        f"dest={route.get('dest_chat')}_"
        f"{route.get('dest_topic')}"
    )

    return True


VIP_HARD_BLOCK_CHAT = -1003726286301

VIP_HARD_BLOCKED_SOURCE = -1002521699926


def should_hard_block_2521699926_from_vip(message, route):
    """
    Absolute VIP protection.

    Blocks:
    1. any direct route whose source is -1002521699926
    2. any Telegram-forwarded post whose ForwardHeader says
       the original channel was -1002521699926

    This applies to EVERY topic inside -1003852763875.
    """

    if not isinstance(route, dict):
        return False

    try:
        dest_chat = int(route.get("dest_chat", 0))
    except Exception:
        return False

    if dest_chat != VIP_HARD_BLOCK_CHAT:
        return False

    try:
        direct_source = int(route.get("source_chat", 0))
    except Exception:
        direct_source = 0

    try:
        forward_origins = forward_origin_chat_ids(message)
    except Exception:
        forward_origins = set()

    direct_match = direct_source == VIP_HARD_BLOCKED_SOURCE

    forwarded_match = VIP_HARD_BLOCKED_SOURCE in forward_origins

    if not direct_match and not forwarded_match:
        return False

    log.warning(
        "[VIP SOURCE HARD BLOCK] "
        f"blocked_source={VIP_HARD_BLOCKED_SOURCE} "
        f"direct_source={direct_source} "
        f"forward_origins={sorted(forward_origins)} "
        f"source_msg={getattr(message, 'id', None)} "
        f"route={route.get('name')} "
        f"dest={route.get('dest_chat')}_"
        f"{route.get('dest_topic')}"
    )

    return True


def should_hard_block_sender(message, context="", route=None):
    ids = sender_ids_for_message(message)
    matched = sorted(ids & BLOCKED_SENDER_IDS)

    if not matched:
        return False

    route_name = route.get("name") if isinstance(route, dict) else None
    route_dest = f"{route.get('dest_chat')}_{route.get('dest_topic')}" if isinstance(route, dict) else None

    log.warning(
        f"[blocked sender hard stop] blocked_ids={matched} "
        f"all_sender_ids={sorted(ids)} msg={getattr(message, 'id', None)} "
        f"context={context} route={route_name} dest={route_dest}"
    )

    return True


def map_key(source_chat, source_msg_id, dest_chat, dest_topic):
    return f"{source_chat}:{source_msg_id}:{dest_chat}:{dest_topic}"

"""Owner instruction 2026-11-02: stop inaccessible/paid forwarding, never retry it.

This file contains routing policy, not Telegram credentials. Existing message
maps/checkpoints are never changed here. New runtime stops have their own file.
"""
import json
import os
import re
from pathlib import Path

STOPPED_SOURCE_CHATS = frozenset({
    -1002817163788, -1002385852838, -1003903223523, -1003423126440,
    -1003753584094, -1003788774930, -1003803259179, -1003902184158,
    -1003371106919, -1003893360328, -1003904372271, -1003229476368,
    -1003801452999,  # Current ChannelPrivateError in MAIN.
    -1003252087470, -1004347858259, -1004369388188,  # ALT.
})
# 2026-11-03: owner paused the Hub (-1003918958200) and Partner
# (-1004442967052) destinations. Route definitions stay in routes.py and are
# archived in docs/paused_routes_20261103.md; removing an id here restores them.
STOPPED_DEST_CHATS = frozenset({-1003216105903, -1003918958200, -1004442967052})
WRITE_REQUESTS = frozenset({
    "SendMessageRequest", "SendMediaRequest", "SendMultiMediaRequest",
    "ForwardMessagesRequest", "EditMessageRequest", "SendInlineBotResultRequest",
})
STOPPED_DEST_TOPICS = frozenset({(-1003885074241, 4660)})
REOPENED_7250_DESTINATION = (-1003726286301, 12161)
_RUNTIME = None
_PATH = None


def identity(route):
    return ":".join(str(route.get(k)) for k in
                    ("source_chat", "source_topic", "dest_chat", "dest_topic"))


def relay7250_allowed(route):
    return (int(route.get("dest_chat") or 0), int(route.get("dest_topic") or 0)) == REOPENED_7250_DESTINATION


def known_stopped(route):
    return (int(route.get("source_chat") or 0) in STOPPED_SOURCE_CHATS
            or int(route.get("dest_chat") or 0) in STOPPED_DEST_CHATS
            or (int(route.get("dest_chat") or 0), int(route.get("dest_topic") or 0))
            in STOPPED_DEST_TOPICS)


def configure(data_dir):
    global _RUNTIME, _PATH
    path = Path(data_dir) / "owner_stopped_forwarding_20261102.json"
    if _PATH == path and _RUNTIME is not None:
        return
    _PATH = path
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeError("Invalid owner stop ledger; preserving it")
        _RUNTIME = value
    else:
        _RUNTIME = {}


def is_stopped(route):
    return known_stopped(route) or identity(route) in (_RUNTIME or {})


def stop_route(route, reason, logger=None):
    if _RUNTIME is None:
        configure(os.environ.get("DATA_DIR") or "./data")
    key = identity(route)
    if key in _RUNTIME:
        return
    _RUNTIME[key] = {"reason": str(reason)}
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(_RUNTIME, sort_keys=True), encoding="utf-8")
    tmp.replace(_PATH)
    if logger:
        logger.warning("[OWNER ROUTE STOPPED] route=%s contract=%s reason=%s RETRY=False NOT_DELIVERED=True",
                       route.get("name"), key, reason)


def blocked_write(request):
    """True for a send/forward/edit whose destination chat the owner stopped."""
    if type(request).__name__ not in WRITE_REQUESTS:
        return False
    peer = getattr(request, "to_peer", None) or getattr(request, "peer", None)
    try:
        from telethon import utils
        return int(utils.get_peer_id(peer)) in STOPPED_DEST_CHATS
    except Exception:
        return False


def install_send_guard(client, logger=None):
    """Refuse writes to stopped chats on every code path of this client.

    Raises ChatWriteForbiddenError, which callers already treat as permanent,
    so routes stop instead of retrying. Reads and deletes are unaffected."""
    if getattr(client, "_owner_send_guard", False):
        return
    from telethon import utils
    from telethon.errors import ChatWriteForbiddenError
    original = client._call

    async def guarded_call(sender, request, ordered=False, flood_sleep_threshold=None):
        for item in (request if utils.is_list_like(request) else (request,)):
            if blocked_write(item):
                if logger:
                    logger.warning("[OWNER DEST STOPPED] request=%s NOT_SENT=True", type(item).__name__)
                raise ChatWriteForbiddenError(request=item)
        return await original(sender, request, ordered=ordered, flood_sleep_threshold=flood_sleep_threshold)

    client._call = guarded_call
    client._owner_send_guard = True


def permanent_access_error(exc):
    names = {c.__name__ for c in type(exc).__mro__}
    permanent = {
        "ChannelPrivateError", "ChatWriteForbiddenError", "ChatSendPlainForbiddenError",
        "ChatSendMediaForbiddenError", "ChatSendPhotosForbiddenError",
        "ChatSendVideosForbiddenError", "ChatSendDocsForbiddenError",
        "ChatSendAudiosForbiddenError", "ChatSendVoicesForbiddenError",
        "ChatSendRoundvideosForbiddenError", "ChatSendStickersForbiddenError",
        "ChatSendGifsForbiddenError", "ChatSendPollForbiddenError",
        "ChatAdminRequiredError", "ChatRestrictedError", "UserBannedInChannelError",
        "TopicClosedError", "TopicDeletedError",
    }
    if names & permanent:
        return sorted(names & permanent)[0]
    text = str(getattr(exc, "message", "") or exc)
    if re.search(r"\bALLOW_PAYMENT_REQUIRED(?:_\d+)?\b", text):
        return "PaidMessagesRequired"
    if getattr(exc, "code", None) == 403:
        return "TelegramForbidden"
    if isinstance(exc, ValueError) and "Could not find" in text and "input entity" in text:
        return "EntityUnavailable"
    if isinstance(exc, RuntimeError) and text.startswith("Owner topic unavailable:"):
        return "TopicUnavailable"
    return None


"""One-time deletion of CURRENT messages in the General topic of one forum.

Target only -1003726286301 / General 1. Never use delete-history or
deleteTopicHistory: both could affect the entire group or a separate topic.
Telegram General is the normal supergroup history; all other forum topics
have reply_to.forum_topic and/or a non-1 top-message marker.

We scan the channel's history, conservatively classify individual messages,
refetch candidates by ID for a second check and batch-delete confirmed General
posts. Save a monotonic checkpoint on Railway's persistent volume. New messages
after the first recorded cutoff are not included in this one-time operation.
"""

import asyncio
import json
import time
from pathlib import Path

from telethon.errors import FloodWaitError

CHAT = -1003726286301
GENERAL = 1
KNOWN_NON_GENERAL = 10877
STATE_FILENAME = "general_topic1_owner_delete_20261026_v1.json"
PAGE_SIZE = 100
DELETE_BATCH = 40


def _save(path, state):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, TypeError, ValueError):
        return {}


def _belongs_to_general(msg):
    """Fail closed for every known forum-topic marker or service message."""
    if msg is None or getattr(msg, "date", None) is None:
        return False
    if int(getattr(msg, "id", 0) or 0) <= GENERAL:
        return False
    if getattr(msg, "action", None) is not None:
        return False

    reply = getattr(msg, "reply_to", None)
    if reply is not None:
        # Telegram sets forum_topic for all topics EXCEPT General.
        if getattr(reply, "forum_topic", False):
            return False
        top = getattr(reply, "reply_to_top_id", None)
        if top is not None and int(top) != GENERAL:
            return False

    for attr in ("reply_to_top_id", "top_msg_id"):
        top = getattr(msg, attr, None)
        if top is not None and int(top) != GENERAL:
            return False

    return True


async def _request(call, logger, description):
    for attempt in range(4):
        try:
            return await call()
        except FloodWaitError as exc:
            pause = max(1, int(exc.seconds)) + 1
            logger.warning(
                "[GENERAL1 FLOODWAIT] action=%s attempt=%s/4 sleep=%ss",
                description, attempt + 1, pause,
            )
            if attempt == 3:
                raise
            await asyncio.sleep(pause)


async def remove_general_topic1_history_once(main_module, logger=None):
    log = logger or main_module.log
    client = main_module.client
    directory = Path(main_module.DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILENAME
    state = _load(path)

    if state.get("status") == "done":
        log.warning(
            "[GENERAL1 CLEANUP STATE SKIP] already_done=True "
            "deleted=%s pages=%s",
            state.get("deleted", 0), state.get("pages", 0),
        )
        return state

    # Important: General is NOT a thread. Scanning reply_to=1 is incorrect.
    entity = await _request(
        lambda: client.get_entity(CHAT), log, "group-entity",
    )
    if not getattr(entity, "forum", False):
        raise RuntimeError(
            "target chat does not advertise Telegram forum=True; no deletion"
        )

    # Before any deletion, prove that this Telethon session correctly marks
    # messages from a KNOWN different topic. Without this negative control,
    # a missing forum_topic flag might cause collateral deletion.
    test_messages = await _request(
        lambda: client.get_messages(
            CHAT, reply_to=KNOWN_NON_GENERAL, limit=15,
        ),
        log, "non-general-marker-test",
    )
    ordinary_test_messages = [
        message for message in (test_messages or [])
        if message is not None
        and getattr(message, "date", None) is not None
        and getattr(message, "action", None) is None
    ]
    if not ordinary_test_messages:
        raise RuntimeError(
            "cannot validate forum-topic exclusion markers; no deletion"
        )
    if any(_belongs_to_general(msg) for msg in ordinary_test_messages):
        raise RuntimeError(
            "forum-topic marker validation FAILED; no deletion"
        )

    if not state:
        newest = await _request(
            lambda: client.get_messages(CHAT, limit=1),
            log, "snapshot-current-history-top",
        )
        cutoff = max(
            (int(msg.id) for msg in (newest or []) if msg is not None),
            default=0,
        )
        state = {
            "status": "running",
            "chat": CHAT,
            "topic": GENERAL,
            "cutoff": cutoff,
            "cursor": cutoff + 1,
            "pages": 0,
            "scanned": 0,
            "deleted": 0,
            "protected": 0,
            "started_at": time.time(),
        }
        _save(path, state)

    if (state.get("chat"), state.get("topic")) != (CHAT, GENERAL):
        raise RuntimeError("persistent cleanup target mismatch; no deletion")

    log.warning(
        "[GENERAL1 CLEANUP BEGIN] chat=%s topic=%s cutoff=%s cursor=%s "
        "OTHER_TOPIC_MESSAGES_PROTECTED=True",
        CHAT, GENERAL, state["cutoff"], state["cursor"],
    )

    while True:
        cursor = int(state["cursor"])
        page = await _request(
            lambda c=cursor: client.get_messages(
                CHAT, limit=PAGE_SIZE, offset_id=c,
            ),
            log, "page-scan",
        )
        page = [
            msg for msg in (page or [])
            if msg is not None and getattr(msg, "id", None) is not None
        ]

        if not page:
            state["status"] = "done"
            state["completed_at"] = time.time()
            _save(path, state)
            log.warning(
                "[GENERAL1 CLEANUP DONE] chat=%s topic=%s "
                "scanned=%s deleted=%s protected=%s pages=%s "
                "cutoff=%s OTHER_TOPICS_UNCHANGED=True",
                CHAT, GENERAL, state["scanned"],
                state["deleted"], state["protected"],
                state["pages"], state["cutoff"],
            )
            return state

        page_ids = [int(msg.id) for msg in page if int(msg.id) > 0]
        if not page_ids or min(page_ids) >= cursor:
            raise RuntimeError("history cursor failed to advance; stopping")

        to_check = [
            int(msg.id) for msg in page
            if _belongs_to_general(msg)
        ]

        # Telegram's delete_messages does not re-verify each message's
        # chat/topic. Re-fetch every candidate from our EXACT group and
        # recheck markers just before the destructive operation.
        for start in range(0, len(to_check), DELETE_BATCH):
            ids = to_check[start:start + DELETE_BATCH]
            raw = await _request(
                lambda batch=ids: client.get_messages(CHAT, ids=batch),
                log, "double-check",
            )
            verified = sorted({
                int(msg.id) for msg in (raw or [])
                if _belongs_to_general(msg)
                and int(msg.id) in ids
            })
            if len(verified) != len(ids):
                log.warning(
                    "[GENERAL1 UNSAFE CANDIDATES STOP] "
                    "expected=%s verified=%s NO_FURTHER_DELETION=True",
                    ids, verified,
                )
                raise RuntimeError(
                    "General topic membership changed or became ambiguous"
                )

            await _request(
                lambda batch=verified: client.delete_messages(
                    CHAT, batch, revoke=True,
                ),
                log, "delete-confirmed-general",
            )
            # Explicitly verify server-side removals before updating
            # persistent progress and continuing to older history.
            after = await _request(
                lambda batch=verified: client.get_messages(CHAT, ids=batch),
                log, "verify-removed",
            )
            survivors = [
                int(msg.id) for msg in (after or [])
                if msg is not None and getattr(msg, "date", None) is not None
            ]
            if survivors:
                log.warning(
                    "[GENERAL1 DELETE UNCONFIRMED] ids=%s "
                    "STOPPING_WITHOUT_CHECKPOINT=True",
                    survivors,
                )
                raise RuntimeError(
                    "Telegram still returns some supposedly deleted messages"
                )

            state["deleted"] = int(state.get("deleted", 0)) + len(verified)
            _save(path, state)
            log.warning(
                "[GENERAL1 DELETED BATCH] chat=%s topic=1 "
                "count=%s total_deleted=%s range=%s..%s",
                CHAT, len(verified), state["deleted"],
                min(verified), max(verified),
            )
            await asyncio.sleep(0.5)

        state["pages"] = int(state.get("pages", 0)) + 1
        state["scanned"] = int(state.get("scanned", 0)) + len(page)
        state["protected"] = int(state.get("protected", 0)) + (
            len(page) - len(to_check)
        )
        state["cursor"] = min(page_ids)
        _save(path, state)

        if state["pages"] % 5 == 0:
            log.warning(
                "[GENERAL1 CLEANUP PROGRESS] pages=%s scanned=%s "
                "deleted=%s protected=%s cursor=%s",
                state["pages"], state["scanned"], state["deleted"],
                state["protected"], state["cursor"],
            )
        await asyncio.sleep(1)

"""One-time scoped Telegram forum topic 5 history cleanup.

Delete only confirmed messages in chat -1003726286301 topic 5.
Keep the topic root and all other topics. Store progress in DATA_DIR and
never replay or delete new messages posted after the initial cutoff.
"""
import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

CHAT = -1003726286301
TOPIC = 5
STATE_NAME = "vip5_owner_purge_20261028_v1.json"


def _save(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def _load(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _is_topic5(message):
    if message is None or getattr(message, "date", None) is None:
        return False
    if int(getattr(message, "id", 0) or 0) <= TOPIC:
        return False  # topic root itself
    if getattr(message, "action", None) is not None:
        return False  # topic management record
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return False

    for value in (
        getattr(reply, "reply_to_top_id", None),
        getattr(message, "reply_to_top_id", None),
        getattr(message, "top_msg_id", None),
    ):
        if value is not None:
            try:
                return int(value) == TOPIC
            except (TypeError, ValueError):
                return False

    try:
        return int(getattr(reply, "reply_to_msg_id", 0) or 0) == TOPIC
    except (TypeError, ValueError):
        return False


async def _telegram(call, log, label):
    for attempt in range(4):
        try:
            return await call()
        except FloodWaitError as exc:
            if attempt == 3:
                raise
            seconds = max(1, int(exc.seconds)) + 1
            log.warning("[VIP5 FLOODWAIT] action=%s seconds=%s", label, seconds)
            await asyncio.sleep(seconds)


async def delete_existing_vip5_messages_once(main_module, logger=None):
    log = logger or main_module.log
    client = main_module.client
    data_dir = Path(main_module.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / STATE_NAME
    state = _load(path)

    if state.get("status") == "done":
        log.warning(
            "[VIP5 CLEANUP STATE SKIP] deleted=%s ambiguous=%s",
            state.get("deleted"), state.get("ambiguous"),
        )
        return state

    entity = await _telegram(lambda: client.get_entity(CHAT), log, "entity")
    if not getattr(entity, "forum", False):
        raise RuntimeError("target does not advertise forum=True; no deletions")

    topic_root = await _telegram(
        lambda: client.get_messages(CHAT, ids=TOPIC), log, "topic-root",
    )
    if topic_root is None or getattr(topic_root, "date", None) is None:
        raise RuntimeError("topic root inaccessible; no deletions")

    if not state:
        newest = await _telegram(
            lambda: client.get_messages(CHAT, limit=1, reply_to=TOPIC),
            log, "initial-topic-snapshot",
        )
        cutoff = max(
            [int(m.id) for m in (newest or [])
             if m is not None and getattr(m, "id", None)] or [TOPIC]
        )
        state = dict(
            status="running", chat=CHAT, topic=TOPIC, cutoff=cutoff,
            cursor=cutoff + 1, deleted=0, scanned=0,
            ambiguous=0, pages=0, started_at=time.time(),
        )
        _save(path, state)

    if (state.get("chat"), state.get("topic")) != (CHAT, TOPIC):
        raise RuntimeError("state target mismatch; no deletions")

    log.warning(
        "[VIP5 CLEANUP START] chat=%s topic=%s cutoff=%s "
        "other_topics_protected=True", CHAT, TOPIC, state["cutoff"],
    )

    while True:
        cursor = int(state["cursor"])
        page = await _telegram(
            lambda c=cursor: client.get_messages(
                CHAT, limit=80, reply_to=TOPIC, offset_id=c,
            ), log, "page",
        )
        messages = [
            m for m in (page or [])
            if m is not None and getattr(m, "id", None) is not None
            and TOPIC < int(m.id) <= int(state["cutoff"])
        ]

        if not messages:
            state["status"] = "done"
            state["completed_at"] = time.time()
            _save(path, state)
            log.warning(
                "[VIP5 CLEANUP DONE] deleted=%s ambiguous=%s scanned=%s "
                "pages=%s OUTBOUND_BLOCKED=True OTHER_TOPICS_PRESERVED=True",
                state["deleted"], state["ambiguous"],
                state["scanned"], state["pages"],
            )
            return state

        candidates = sorted({int(m.id) for m in messages if _is_topic5(m)})
        uncertain = len(messages) - len(candidates)

        for index in range(0, len(candidates), 30):
            ids = candidates[index:index + 30]
            reread = await _telegram(
                lambda batch=ids: client.get_messages(CHAT, ids=batch),
                log, "double-verify",
            )
            confirmed = sorted({
                int(m.id) for m in (reread or [])
                if m is not None and int(m.id) in ids and _is_topic5(m)
            })
            uncertain += len(ids) - len(confirmed)

            if confirmed:
                await _telegram(
                    lambda batch=confirmed: client.delete_messages(
                        CHAT, batch, revoke=True,
                    ), log, "delete",
                )
                after = await _telegram(
                    lambda batch=confirmed: client.get_messages(CHAT, ids=batch),
                    log, "verify-delete",
                )
                survivors = [
                    m.id for m in (after or [])
                    if m is not None and getattr(m, "date", None) is not None
                ]
                if survivors:
                    raise RuntimeError("delete unconfirmed: " + repr(survivors))
                state["deleted"] += len(confirmed)
                _save(path, state)
                log.warning(
                    "[VIP5 DELETED BATCH] count=%s total_deleted=%s",
                    len(confirmed), state["deleted"],
                )
                await asyncio.sleep(0.5)

        lowest = min(int(m.id) for m in messages)
        if lowest >= cursor:
            raise RuntimeError("topic history cursor has not advanced")
        state["cursor"] = lowest
        state["pages"] += 1
        state["scanned"] += len(messages)
        state["ambiguous"] += uncertain
        _save(path, state)
        await asyncio.sleep(0.8)

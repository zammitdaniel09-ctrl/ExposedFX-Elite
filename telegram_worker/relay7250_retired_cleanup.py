"""Remove only mapped copies of retired source topic 7250 from VIP topic 10877.

Permanent route blocking is done in routes.py and the worker copy paths.
This cleanup is deliberately conservative: source IDs must be demonstrated
members of topic 7250 by Telegram topic-history or the original durable
bootstrap state, AND the exact destination mapping must point to 10877,
AND the fetched destination message must be in topic 10877.

Any uncertain message is LEFT UNCHANGED. Topic 7248 is never purged.
"""

import asyncio
import json
import time
from pathlib import Path
from telethon.errors import FloodWaitError

SRC_CHAT = -1004367822325
SRC_TOPIC = 7250
DEST_CHAT = -1003726286301
DEST_TOPIC = 10877
BOOTSTRAP_FILE = "relay7250_7248_to_vip10877_last20_v1.json"
CLEANUP_FILE = "relay7250_to_10877_retired_cleanup_v1.json"
STATE_KEY_7250 = f"{SRC_CHAT}:{SRC_TOPIC}:{DEST_CHAT}:{DEST_TOPIC}"
STATE_KEY_7248 = f"{SRC_CHAT}:7248:{DEST_CHAT}:{DEST_TOPIC}"


def _ids_from_entry(entry):
    output = set()
    if not isinstance(entry, dict):
        return output
    for group in entry.get("selected_units") or []:
        if not isinstance(group, list):
            continue
        for mid in group:
            try:
                output.add(int(mid))
            except (ValueError, TypeError):
                continue
    return output


def _load_json(path):
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        return result if isinstance(result, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(path)


def _is_target_topic(message):
    reply = getattr(message, "reply_to", None)
    top = getattr(reply, "reply_to_top_id", None) or getattr(message, "reply_to_top_id", None)
    if top is not None:
        return int(top) == DEST_TOPIC
    parent_id = getattr(reply, "reply_to_msg_id", None) or getattr(message, "reply_to_msg_id", None)
    return parent_id is not None and int(parent_id) == DEST_TOPIC


async def _telegram_delete(client, mid):
    for attempt in range(3):
        try:
            await client.delete_messages(DEST_CHAT, [int(mid)], revoke=True)
            return
        except FloodWaitError as exc:
            if attempt == 2:
                raise
            await asyncio.sleep(max(1, int(exc.seconds)) + 1)


async def cleanup_relay7250_to_vip10877_once(main_module, logger=None):
    log = logger or main_module.log
    client = main_module.client
    data_dir = Path(main_module.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)

    # No mappings for this retired source -> exact destination means there
    # is nothing that could be safely deleted. Avoid a 5,000-message
    # Telegram history request on every Railway restart.
    has_candidate_mapping = False
    for key in main_module.message_map:
        parts = str(key).split(":")
        if (
            len(parts) == 4
            and parts[0] == str(SRC_CHAT)
            and parts[2] == str(DEST_CHAT)
            and parts[3] == str(DEST_TOPIC)
        ):
            has_candidate_mapping = True
            break

    if not has_candidate_mapping:
        log.warning(
            "[RELAY7250 CLEANUP NO MAPS] "
            "source=%s_%s dest=%s_%s "
            "HISTORY_API_CALLS=0 PROVIDER7248_PRESERVED=True",
            SRC_CHAT, SRC_TOPIC, DEST_CHAT, DEST_TOPIC,
        )
        return {
            "status": "no_source_destination_mappings",
            "deleted": [],
            "uncertain": [],
            "errors": [],
        }

    # The original bootstrap selected its source topic via reply_to=7250.
    bootstrap = _load_json(data_dir / BOOTSTRAP_FILE)
    identified = _ids_from_entry(bootstrap.get(STATE_KEY_7250))
    protected_source = _ids_from_entry(bootstrap.get(STATE_KEY_7248))

    # Live copies after the bootstrap are also eligible, but only if Telegram
    # itself says the source message belongs to topic 7250.
    try:
        posts = await client.get_messages(SRC_CHAT, reply_to=SRC_TOPIC, limit=5000)
        identified.update(
            int(msg.id) for msg in (posts or [])
            if msg is not None and getattr(msg, "date", None) is not None
        )
        log.warning(
            "[RELAY7250 CLEANUP SOURCE VERIFIED] "
            "source=%s_%s verified_ids=%s historical_ids=%s",
            SRC_CHAT, SRC_TOPIC, len(identified),
            len(_ids_from_entry(bootstrap.get(STATE_KEY_7250))),
        )
    except Exception as exc:
        log.warning(
            "[RELAY7250 CLEANUP SOURCE FETCH FAILED SAFE] "
            "%s: %s bootstrap_ids_only=%s",
            type(exc).__name__, exc, len(identified),
        )

    # Do not trust the raw map for source-topic identity: it stores source
    # chat and message ID but not source topic. Confirm IDs above first.
    wanted = {}
    other_destination_ids = set()
    for key, value in list(main_module.message_map.items()):
        parts = str(key).split(":")
        if len(parts) != 4:
            continue
        try:
            src_chat, src_msg, dest_chat, dest_topic = (int(part) for part in parts)
        except (TypeError, ValueError):
            continue
        if (dest_chat, dest_topic) != (DEST_CHAT, DEST_TOPIC):
            continue
        mapped = set(main_module.mapped_ids_from_value(value))
        if src_chat == SRC_CHAT and src_msg in identified and src_msg not in protected_source:
            wanted[key] = mapped
        else:
            other_destination_ids.update(mapped)

    status = {
        "timestamp": time.time(),
        "source": [SRC_CHAT, SRC_TOPIC],
        "destination": [DEST_CHAT, DEST_TOPIC],
        "source_ids_identified": len(identified),
        "mapped_posts_matched": len(wanted),
        "deleted": [],
        "already_absent": [],
        "uncertain": [],
        "errors": [],
    }

    if not wanted:
        log.warning(
            "[RELAY7250 CLEANUP DONE] mapped_posts=0 deleted=0 "
            "PROVIDER7248_PRESERVED=True OUTBOUND7250_BLOCKED=True"
        )
        _save_json(data_dir / CLEANUP_FILE, status)
        return status

    # A valid topic-root check prevents misdirected deletion when the
    # destination group/topic has changed since the mapping was recorded.
    topic_root = await client.get_messages(DEST_CHAT, ids=DEST_TOPIC)
    if topic_root is None or getattr(topic_root, "date", None) is None:
        raise RuntimeError("cleanup aborted: destination topic 10877 unavailable")

    affected = set()
    unique_dest_ids = sorted(set().union(*wanted.values()))
    for start in range(0, len(unique_dest_ids), 40):
        batch = unique_dest_ids[start:start + 40]
        messages = await client.get_messages(DEST_CHAT, ids=batch)
        by_id = {
            int(msg.id): msg
            for msg in (messages or [])
            if msg is not None and getattr(msg, "date", None) is not None
        }

        for mid in batch:
            if mid in other_destination_ids or mid == DEST_TOPIC:
                status["uncertain"].append(mid)
                continue
            message = by_id.get(mid)
            if message is None:
                status["already_absent"].append(mid)
                affected.add(mid)
                continue
            if not _is_target_topic(message):
                status["uncertain"].append(mid)
                continue
            try:
                await _telegram_delete(client, mid)
                status["deleted"].append(mid)
                affected.add(mid)
                log.warning(
                    "[RELAY7250 COPY DELETED] dest=%s_%s dest_msg=%s "
                    "ONLY_TOPIC7250=True PROVIDER7248_PRESERVED=True",
                    DEST_CHAT, DEST_TOPIC, mid,
                )
            except Exception as exc:
                status["errors"].append({
                    "id": mid, "error": f"{type(exc).__name__}: {exc}",
                })
                log.warning(
                    "[RELAY7250 DELETE FAILED SAFE] dest_msg=%s %s: %s",
                    mid, type(exc).__name__, exc,
                )

        # Persist after each batch so Railway crashes cannot cause map loss.
        if affected:
            for key, ids in list(wanted.items()):
                remaining = sorted(ids - affected)
                if len(remaining) == len(ids):
                    continue
                if remaining:
                    main_module.message_map[key] = remaining
                else:
                    main_module.message_map.pop(key, None)
                wanted[key] = set(remaining)
            main_module.save_map()
            affected.clear()
        _save_json(data_dir / CLEANUP_FILE, status)

    log.warning(
        "[RELAY7250 CLEANUP DONE] mapped_posts=%s deleted=%s "
        "already_absent=%s uncertain=%s errors=%s "
        "PROVIDER7248_PRESERVED=True OUTBOUND7250_BLOCKED=True",
        status["mapped_posts_matched"], len(status["deleted"]),
        len(status["already_absent"]), len(status["uncertain"]),
        len(status["errors"]),
    )
    return status

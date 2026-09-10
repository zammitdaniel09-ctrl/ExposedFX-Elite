import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple

from telethon import events
from telethon.tl.types import MessageEntityCustomEmoji


# One or many assignments can be placed in the same Saved Message, e.g.
# GreenTick = <emoji>
# Boom = <emoji>
# RedCross = <emoji>
# Warning = <emoji>
#
# Keep this line-based. Telegram entity offsets refer to the exact full message.
_ASSIGNMENT_LINE_RE = re.compile(
    r"^[ \t]*([A-Za-z][A-Za-z0-9_-]{0,63})[ \t]*=[ \t]*(.*?)[ \t]*$",
    re.M,
)
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]{0,63})\}")


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


class CommunityEmojiRegistry:
    """ExposedFX custom emoji registry backed by Telegram Saved Messages.

    Supported input (one assignment per line OR many in one message):

        GreenTick = <custom emoji>
        Boom = <custom emoji>
        RedCross = <custom emoji>
        Warning = <custom emoji>

    The latest assignment for a given alias wins.
    """

    def __init__(self, data_dir: Path, logger=None):
        self.log = logger or logging.getLogger("community-emoji-registry")
        self.path = Path(data_dir) / "community_emoji_registry.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self):
        if not self.path.exists():
            self.entries = {}
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = raw if isinstance(raw, dict) else {}
        except Exception as exc:
            self.log.warning("[COMMUNITY EMOJI] registry load failed: %s", exc)
            self.entries = {}

    def save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp.replace(self.path)

    def get(self, alias: str):
        if alias in self.entries:
            return self.entries[alias]

        wanted = (alias or "").casefold()
        for key, value in self.entries.items():
            if key.casefold() == wanted:
                return value
        return None

    def remove(self, alias: str) -> bool:
        exact = alias if alias in self.entries else None
        if exact is None:
            wanted = (alias or "").casefold()
            exact = next((key for key in self.entries if key.casefold() == wanted), None)

        if exact is None:
            return False

        self.entries.pop(exact, None)
        self.save()
        return True

    def _capture_assignment(self, message, raw_text: str, match) -> Tuple[bool, str]:
        alias = match.group(1).strip()
        right_side = match.group(2)
        right_side_clean = right_side.strip()

        if right_side_clean.upper() in {"DELETE", "REMOVE"}:
            removed = self.remove(alias)
            result = f"removed:{alias}" if removed else f"missing:{alias}"
            self.log.warning("[COMMUNITY EMOJI SAVED] %s", result)
            return True, result

        # Convert this line's RHS character span into Telegram UTF-16 offsets.
        rhs_start_utf16 = _utf16_len(raw_text[:match.start(2)])
        rhs_end_utf16 = _utf16_len(raw_text[:match.end(2)])

        custom_entities = []
        for entity in (getattr(message, "entities", None) or []):
            if not isinstance(entity, MessageEntityCustomEmoji):
                continue

            entity_start = int(entity.offset)
            entity_end = entity_start + int(entity.length)

            # Entity must overlap THIS assignment's RHS only.
            if entity_end <= rhs_start_utf16 or entity_start >= rhs_end_utf16:
                continue

            custom_entities.append(entity)

        custom_entities.sort(key=lambda e: int(e.offset))

        if len(custom_entities) == 1:
            entity = custom_entities[0]
            document_id = int(entity.document_id)

            self.entries[alias] = {
                "type": "custom",
                "document_id": document_id,
                "fallback": right_side_clean or "⭐",
                "source_message_id": int(getattr(message, "id", 0) or 0),
            }
            self.save()

            self.log.warning(
                "[COMMUNITY EMOJI SAVED] alias=%s type=custom document_id=%s",
                alias,
                document_id,
            )
            return True, f"saved_custom:{alias}:{document_id}"

        if len(custom_entities) > 1:
            sequence_entities = []
            document_ids = []

            for entity in custom_entities:
                relative_offset = max(0, int(entity.offset) - rhs_start_utf16)
                document_id = int(entity.document_id)
                document_ids.append(document_id)
                sequence_entities.append(
                    {
                        "document_id": document_id,
                        "offset": relative_offset,
                        "length": int(entity.length),
                    }
                )

            self.entries[alias] = {
                "type": "custom_sequence",
                "text": right_side_clean or "⭐",
                "entities": sequence_entities,
                "source_message_id": int(getattr(message, "id", 0) or 0),
            }
            self.save()

            ids_text = ",".join(str(x) for x in document_ids)
            self.log.warning(
                "[COMMUNITY EMOJI SAVED] alias=%s type=custom_sequence count=%s document_ids=%s",
                alias,
                len(sequence_entities),
                ids_text,
            )
            return True, f"saved_custom_sequence:{alias}:{len(sequence_entities)}:{ids_text}"

        # Normal Unicode emoji is still allowed as a fallback.
        if right_side_clean:
            self.entries[alias] = {
                "type": "unicode",
                "text": right_side_clean,
                "source_message_id": int(getattr(message, "id", 0) or 0),
            }
            self.save()

            self.log.warning(
                "[COMMUNITY EMOJI SAVED] alias=%s type=unicode text=%r",
                alias,
                right_side_clean,
            )
            return True, f"saved_unicode:{alias}"

        return False, f"no_emoji_found:{alias}"

    def capture_message(self, message) -> Tuple[bool, str]:
        # Do not strip: Telegram entity offsets are based on the exact message.
        raw_text = getattr(message, "message", None) or ""
        matches = list(_ASSIGNMENT_LINE_RE.finditer(raw_text))

        if not matches:
            return False, "not_registry_assignment"

        results = []
        handled_any = False

        for match in matches:
            handled, result = self._capture_assignment(message, raw_text, match)
            results.append(result)
            handled_any = handled_any or handled

        if handled_any:
            aliases = [m.group(1).strip() for m in matches]
            self.log.warning(
                "[COMMUNITY EMOJI MESSAGE CAPTURED] assignments=%s aliases=%s",
                len(matches),
                ",".join(aliases),
            )
            return True, "saved_batch:" + "|".join(results)

        return False, "|".join(results)

    async def rebuild_from_saved_messages(self, client, limit: int = 500):
        """Reconstruct the registry from Telegram Saved Messages."""
        messages = list(await client.get_messages("me", limit=max(1, int(limit))) or [])
        messages.reverse()  # oldest -> newest, so latest assignment wins

        # Telegram Saved Messages are the source of truth. Drop stale cache data,
        # including entries produced by older parser versions.
        self.entries = {}
        self.save()

        handled_messages = 0

        for message in messages:
            handled, _ = self.capture_message(message)
            if handled:
                handled_messages += 1

        self.log.warning(
            "[COMMUNITY EMOJI REBUILD DONE] scanned=%s assignment_messages=%s aliases=%s alias_names=%s",
            len(messages),
            handled_messages,
            len(self.entries),
            ",".join(sorted(self.entries.keys(), key=str.casefold)),
        )

        return handled_messages

    def render(self, template: str):
        """Replace {Alias} placeholders and return (text, Telegram entities)."""
        output_parts: List[str] = []
        entities = []
        cursor = 0

        for match in _PLACEHOLDER_RE.finditer(template or ""):
            literal = template[cursor:match.start()]
            output_parts.append(literal)

            alias = match.group(1)
            entry = self.get(alias)

            if not entry:
                output_parts.append(match.group(0))
                cursor = match.end()
                continue

            current_text = "".join(output_parts)
            base_offset = _utf16_len(current_text)
            entry_type = entry.get("type")

            if entry_type == "custom":
                fallback = str(entry.get("fallback") or "⭐")
                output_parts.append(fallback)
                entities.append(
                    MessageEntityCustomEmoji(
                        offset=base_offset,
                        length=_utf16_len(fallback),
                        document_id=int(entry["document_id"]),
                    )
                )

            elif entry_type == "custom_sequence":
                sequence_text = str(entry.get("text") or "⭐")
                output_parts.append(sequence_text)

                for item in entry.get("entities") or []:
                    entities.append(
                        MessageEntityCustomEmoji(
                            offset=base_offset + int(item.get("offset", 0)),
                            length=max(1, int(item.get("length", 1))),
                            document_id=int(item["document_id"]),
                        )
                    )

            else:
                output_parts.append(str(entry.get("text") or ""))

            cursor = match.end()

        output_parts.append((template or "")[cursor:])
        return "".join(output_parts), entities


async def install_saved_messages_emoji_collector(
    client,
    registry: CommunityEmojiRegistry,
    logger=None,
    scan_limit: int = 500,
):
    """Attach the live Saved Messages collector to the logged-in account."""
    log = logger or registry.log
    me = await client.get_me()
    me_id = int(me.id)

    await registry.rebuild_from_saved_messages(client, limit=scan_limit)

    async def _handler(event):
        try:
            if int(getattr(event, "chat_id", 0) or 0) != me_id:
                return

            handled, result = registry.capture_message(event.message)

            if handled:
                log.warning("[COMMUNITY EMOJI LIVE CAPTURE] %s", result)
            elif "no_emoji_found" in result:
                log.warning("[COMMUNITY EMOJI INVALID] %s", result)

        except Exception as exc:
            log.exception(
                "[COMMUNITY EMOJI LIVE CAPTURE FAILED] %s: %s",
                type(exc).__name__,
                exc,
            )

    client.add_event_handler(
        _handler,
        events.NewMessage(outgoing=True),
    )

    log.warning(
        "[COMMUNITY EMOJI COLLECTOR READY] saved_messages_user_id=%s scan_limit=%s",
        me_id,
        scan_limit,
    )

    return _handler

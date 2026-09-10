import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, List, Tuple

from telethon import events
from telethon.tl.types import MessageEntityCustomEmoji


_ALIAS_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]{0,63})\s*=\s*(.*?)\s*$", re.S)
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z][A-Za-z0-9_-]{0,63})\}")


def _utf16_len(value: str) -> int:
    return len((value or "").encode("utf-16-le")) // 2


class CommunityEmojiRegistry:
    """
    ExposedFX Community custom emoji registry.

    Input format in Telegram Saved Messages:
        GreenTick = <custom emoji>
        Boom = <custom emoji>
        RedCross = ❌

    The latest message for a given alias wins.

    Custom Telegram/Premium emoji entries store the exact Telegram
    document_id from MessageEntityCustomEmoji. Normal Unicode emoji are
    also supported as a fallback.
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

    def capture_message(self, message) -> Tuple[bool, str]:
        text = (getattr(message, "message", None) or "").strip()
        match = _ALIAS_RE.match(text)

        if not match:
            return False, "not_registry_assignment"

        alias = match.group(1).strip()
        right_side = match.group(2).strip()

        if right_side.upper() in {"DELETE", "REMOVE"}:
            removed = self.remove(alias)
            return True, f"removed:{alias}" if removed else f"missing:{alias}"

        custom_entities = [
            entity
            for entity in (getattr(message, "entities", None) or [])
            if isinstance(entity, MessageEntityCustomEmoji)
        ]

        if len(custom_entities) > 1:
            return False, f"multiple_custom_emojis:{alias}"

        if len(custom_entities) == 1:
            entity = custom_entities[0]
            document_id = int(entity.document_id)

            self.entries[alias] = {
                "type": "custom",
                "document_id": document_id,
                "fallback": right_side or "⭐",
                "source_message_id": int(getattr(message, "id", 0) or 0),
            }
            self.save()

            self.log.warning(
                "[COMMUNITY EMOJI SAVED] alias=%s type=custom document_id=%s",
                alias,
                document_id,
            )
            return True, f"saved_custom:{alias}:{document_id}"

        if right_side:
            self.entries[alias] = {
                "type": "unicode",
                "text": right_side,
                "source_message_id": int(getattr(message, "id", 0) or 0),
            }
            self.save()

            self.log.warning(
                "[COMMUNITY EMOJI SAVED] alias=%s type=unicode text=%r",
                alias,
                right_side,
            )
            return True, f"saved_unicode:{alias}"

        return False, f"no_emoji_found:{alias}"

    async def rebuild_from_saved_messages(self, client, limit: int = 500):
        """
        Reconstruct the registry from Telegram Saved Messages.

        This means the registry survives Railway redeploys even if local
        DATA_DIR storage is ephemeral: the Telegram messages are the source
        of truth and the local JSON file is only a cache.
        """
        messages = list(await client.get_messages("me", limit=max(1, int(limit))) or [])
        messages.reverse()  # oldest -> newest, so latest assignment wins

        rebuilt = 0

        for message in messages:
            handled, _ = self.capture_message(message)
            if handled:
                rebuilt += 1

        self.log.warning(
            "[COMMUNITY EMOJI REBUILD DONE] scanned=%s assignments=%s aliases=%s",
            len(messages),
            rebuilt,
            len(self.entries),
        )

        return rebuilt

    def render(self, template: str):
        """
        Replace {Alias} placeholders with their saved emoji and return:
            (plain_text, formatting_entities)

        Custom emoji offsets/lengths are calculated in Telegram UTF-16 units.
        This is ready to pass to Telethon client.send_message(...,
        formatting_entities=entities, parse_mode=None).
        """
        from telethon.tl.types import MessageEntityCustomEmoji

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

            if entry.get("type") == "custom":
                fallback = str(entry.get("fallback") or "⭐")
                offset = _utf16_len(current_text)
                length = _utf16_len(fallback)

                output_parts.append(fallback)
                entities.append(
                    MessageEntityCustomEmoji(
                        offset=offset,
                        length=length,
                        document_id=int(entry["document_id"]),
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
    """
    Attach a live collector to the logged-in account's Saved Messages.

    It only processes outgoing messages sent to self, so normal DMs and
    Telegram groups are ignored.
    """
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
            elif result.startswith("multiple_custom_emojis") or result.startswith("no_emoji_found"):
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

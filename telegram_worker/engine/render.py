"""Turn a source post into the text, entities and media that will be sent.

Formatters are plugins keyed by name. "copy" keeps the post exactly as the
provider wrote it; "imperium_vip" restyles XAUUSD signals and trade updates
for VIP topic 7 with the owner's custom emoji (imperium_vip_inline_hook).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

from .rules import entities_of, is_real_media, text_of


@dataclass
class Rendered:
    messages: List  # the (possibly restyled) messages, unit order
    text: str  # text or album caption actually sent
    entities: list
    media: List  # media objects in send order
    kind: str  # "text" | "media" | "album"


def _copy(message, context):
    return message


def _imperium_vip(message, context):
    from telegram_worker.imperium_vip_inline_hook import format_imperium_vip_text

    text = getattr(message, "message", None) or getattr(message, "raw_text", None) or ""
    if not text:
        return message
    out_text, out_entities, kind = format_imperium_vip_text(
        text, getattr(message, "entities", None), context.get("emoji_registry")
    )
    if not kind:
        return message
    cloned = copy.copy(message)
    cloned.message = out_text
    cloned.entities = out_entities
    return cloned


FORMATTERS: Dict[str, Callable] = {"copy": _copy, "imperium_vip": _imperium_vip}


def render(unit: Sequence, formatter: str, context: Dict) -> Rendered:
    restyle = FORMATTERS[formatter]
    messages = [restyle(m, context) for m in unit]
    if len(messages) > 1 and getattr(messages[0], "grouped_id", None):
        caption_source = next((m for m in messages if text_of(m)), None)
        return Rendered(
            messages=messages,
            text=text_of(caption_source) if caption_source else "",
            entities=list(entities_of(caption_source)) if caption_source else [],
            media=[m.media for m in messages if is_real_media(m)],
            kind="album",
        )
    first = messages[0]
    media = [first.media] if is_real_media(first) else []
    return Rendered(
        messages=messages,
        text=text_of(first),
        entities=list(entities_of(first)),
        media=media,
        kind="media" if media else "text",
    )


def caption_args(rendered: Rendered) -> Tuple:
    """(text, entities) exactly as the legacy send passed them to Telethon."""
    text = rendered.text or ""
    return (text if text else None, rendered.entities if text else None)

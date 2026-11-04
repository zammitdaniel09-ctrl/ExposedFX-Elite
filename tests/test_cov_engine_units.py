"""Engine building blocks: media identity, caption args, map value parsing, peer ids and rule order."""

import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import (
    Document,
    MessageEntityBold,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaPhoto,
    PeerChannel,
    PeerChat,
    PeerUser,
    Photo,
)

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker.engine import render
from telegram_worker.engine.model import Route, Skip, media_key
from telegram_worker.engine.plan import Planner
from telegram_worker.engine.replies import MessageMap
from telegram_worker.engine.rules import Rules, marked_peer_id

RELAY, VIP = -1004367822325, -1003726286301
ROUTE = Route.from_legacy(
    {
        "name": "VIP RELAY Topic2 To VIP1364",
        "source_chat": RELAY,
        "source_topic": 2,
        "dest_chat": VIP,
        "dest_topic": 1364,
    }
)


def msg(msg_id, text="", media=None, grouped=None, sender=None, fwd=None):
    return SimpleNamespace(
        id=msg_id,
        message=text,
        raw_text=text,
        text=text,
        entities=[],
        media=media,
        grouped_id=grouped,
        reply_to=None,
        reply_to_msg_id=None,
        fwd_from=fwd,
        sender_id=sender,
        from_id=None,
    )


class ModelAndRenderTests(unittest.TestCase):
    def test_media_identity_is_the_photo_or_document_id(self):
        photo = MessageMediaPhoto(
            photo=Photo(id=11, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1)
        )
        document = MessageMediaDocument(
            document=Document(
                id=22,
                access_hash=1,
                file_reference=b"",
                date=None,
                mime_type="x",
                size=1,
                dc_id=1,
                attributes=[],
            )
        )
        self.assertIsNone(media_key(None))
        self.assertEqual(media_key(photo), ("photo", 11))
        self.assertEqual(media_key(document), ("document", 22))
        self.assertEqual(media_key(MessageMediaPhoto(photo=None)), ("MessageMediaPhoto", None))
        self.assertEqual(media_key(MessageMediaGeo(geo=None)), ("MessageMediaGeo", None))

    def test_caption_args_match_the_legacy_send(self):
        bold = [MessageEntityBold(0, 2)]
        with_text = render.Rendered(messages=[], text="TP", entities=bold, media=[], kind="text")
        self.assertEqual(render.caption_args(with_text), ("TP", bold))
        without = render.Rendered(messages=[], text="", entities=bold, media=[], kind="media")
        self.assertEqual(render.caption_args(without), (None, None), "no caption means no entities")

    def test_vip_formatter_leaves_media_without_text_untouched(self):
        post = msg(1, "", media=MessageMediaPhoto(photo=None))
        self.assertIs(render.FORMATTERS["imperium_vip"](post, {"emoji_registry": None}), post)

    def test_text_only_album_is_not_sent(self):
        planner = Planner(rules=Rules(), maps=MessageMap({}))
        self.assertEqual(
            planner.plan(ROUTE, [msg(2, "b", grouped=5), msg(1, "a", grouped=5)]), Skip("empty_album")
        )


class MessageMapTests(unittest.TestCase):
    def test_map_values_of_every_legacy_shape_are_read(self):
        maps = MessageMap(
            {
                ROUTE.map_key(1): {"first": 901, "dup": "901", "bad": "x", "zero": 0, "none": None},
                ROUTE.map_key(2): (902, 903),
                ROUTE.map_key(3): "oops",
                ROUTE.map_key(4): 0,
            }
        )
        self.assertEqual(maps.dest_ids(ROUTE, 1), [901])
        self.assertEqual(maps.dest_ids(ROUTE, 2), [902, 903])
        self.assertEqual(maps.dest_ids(ROUTE, 3), [])
        self.assertFalse(maps.any_mapped(ROUTE, [msg(3), msg(4)]))


class RulesTests(unittest.TestCase):
    def test_marked_peer_ids(self):
        self.assertEqual(marked_peer_id(-1001), -1001)
        self.assertEqual(marked_peer_id(PeerChannel(4367822325)), RELAY)
        self.assertEqual(marked_peer_id(PeerChat(55)), -55)
        self.assertEqual(marked_peer_id(PeerUser(77)), 77)
        self.assertIsNone(marked_peer_id(SimpleNamespace()))

    def test_mentions_pass_when_the_filter_is_off(self):
        self.assertFalse(Rules(filter_mentions=False).has_mention(msg(1, "ask @ExposedFXSupport")))
        self.assertTrue(Rules().has_mention(msg(1, "ask @ExposedFXSupport")))

    def test_vip_blocked_source_and_forward_loop_are_named(self):
        rules = Rules()
        forwarded_from_blocked = SimpleNamespace(from_id=PeerChannel(2521699926), saved_from_peer=None)
        self.assertEqual(
            rules.skip_reason([msg(1, "TP1", fwd=forwarded_from_blocked)], ROUTE), "vip_blocked_source"
        )
        guarded = Route(**{**ROUTE.__dict__, "loop_guard_peers": frozenset({VIP})})
        forwarded_from_vip = SimpleNamespace(from_id=PeerChannel(3726286301), saved_from_peer=None)
        self.assertEqual(rules.skip_reason([msg(2, "TP1", fwd=forwarded_from_vip)], guarded), "forward_loop")


if __name__ == "__main__":
    unittest.main()

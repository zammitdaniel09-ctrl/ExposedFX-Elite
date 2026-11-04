"""The VIP custom-emoji registry survives its Saved Messages scrolling past the scan window."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import MessageEntityCustomEmoji

from telegram_worker.community_emoji_registry import CommunityEmojiRegistry, _utf16_len


def assignment(message_id, alias, document_id):
    text = f"{alias} = ✅"
    entity = MessageEntityCustomEmoji(offset=_utf16_len(f"{alias} = "), length=1, document_id=document_id)
    return SimpleNamespace(id=message_id, message=text, entities=[entity])


def note(message_id, text):
    return SimpleNamespace(id=message_id, message=text, entities=[])


class SavedMessages:
    """Newest first, like client.get_messages('me', limit=N)."""

    def __init__(self, messages):
        self.messages = messages

    async def get_messages(self, chat, limit):
        assert chat == "me"
        return sorted(self.messages, key=lambda m: -m.id)[:limit]


class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = CommunityEmojiRegistry(Path(self.tmp.name))

    def rebuild(self, messages, limit=500):
        return asyncio.run(self.registry.rebuild_from_saved_messages(SavedMessages(messages), limit=limit))

    def test_assignments_in_saved_messages_are_loaded_latest_first_wins(self):
        self.rebuild(
            [assignment(1, "GreenTick", 11), assignment(2, "GreenTick", 22), assignment(3, "Boom", 33)]
        )
        self.assertEqual(self.registry.get("GreenTick")["document_id"], 22)
        self.assertEqual(self.registry.get("Boom")["document_id"], 33)

    def test_aliases_older_than_the_scan_window_are_kept(self):
        self.rebuild([assignment(1, "GreenTick", 11), assignment(2, "Boom", 33)])
        alerts = [note(100 + i, f"⚠️ health alert {i}") for i in range(5)]
        with self.assertLogs(self.registry.log, "WARNING") as logs:
            self.rebuild([assignment(1, "GreenTick", 11), assignment(2, "Boom", 33)] + alerts, limit=5)
        self.assertEqual(self.registry.get("GreenTick")["document_id"], 11)
        self.assertEqual(self.registry.get("Boom")["document_id"], 33)
        self.assertTrue(any("[COMMUNITY EMOJI KEPT] aliases=Boom,GreenTick" in line for line in logs.output))
        reloaded = CommunityEmojiRegistry(Path(self.tmp.name))
        self.assertEqual(sorted(reloaded.entries), ["Boom", "GreenTick"], "saved to disk too")

    def test_a_newer_assignment_in_the_window_replaces_the_kept_value(self):
        self.rebuild([assignment(1, "GreenTick", 11)])
        self.rebuild([assignment(1, "GreenTick", 11), assignment(9, "GreenTick", 99)], limit=1)
        self.assertEqual(self.registry.get("GreenTick")["document_id"], 99)

    def test_delete_in_the_window_still_removes_the_alias(self):
        self.rebuild([assignment(1, "GreenTick", 11), assignment(2, "Boom", 33)])
        self.rebuild(
            [assignment(1, "GreenTick", 11), assignment(2, "Boom", 33), note(3, "greentick = DELETE")], 1
        )
        self.assertIsNone(self.registry.get("GreenTick"))
        self.assertEqual(self.registry.get("Boom")["document_id"], 33)

    def test_reassigned_after_delete_in_the_window_is_present(self):
        self.rebuild([note(1, "Boom = DELETE"), assignment(2, "Boom", 44)])
        self.assertEqual(self.registry.get("Boom")["document_id"], 44)


if __name__ == "__main__":
    unittest.main()

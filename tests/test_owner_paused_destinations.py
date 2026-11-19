"""Hub and Partner destinations paused 3 Nov 2026: no route and no code path may write there."""

import asyncio
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker.routes import ROUTES
from telethon.errors import ChatWriteForbiddenError
from telethon.tl.functions.channels import GetMessagesRequest
from telethon.tl.functions.messages import (
    DeleteMessagesRequest,
    EditMessageRequest,
    ForwardMessagesRequest,
    SendMessageRequest,
)
from telethon.tl.types import InputPeerChannel

HUB, PARTNER, VIP = (
    InputPeerChannel(3918958200, 0),
    InputPeerChannel(4442967052, 0),
    InputPeerChannel(3726286301, 0),
)


class FakeClient:
    def __init__(self):
        self.sent = []

    async def _call(self, sender, request, ordered=False, flood_sleep_threshold=None):
        self.sent.append(request)
        return "ok"


class PausedDestinationTests(unittest.TestCase):
    def test_no_live_route_targets_paused_chats(self):
        paused = (-1003918958200, -1004442967052, -1003802436353, -1004365831817)
        self.assertFalse([r for r in ROUTES if int(r["dest_chat"]) in paused])
        for channel_id in (3802436353, 4365831817):
            self.assertTrue(policy.blocked_write(SendMessageRequest(InputPeerChannel(channel_id, 0), "x")))
        self.assertTrue(any(int(r["dest_chat"]) == -1003726286301 for r in ROUTES))

    def test_writes_to_paused_chats_are_blocked(self):
        for request in (
            SendMessageRequest(HUB, "x"),
            SendMessageRequest(-1004442967052, "x"),
            ForwardMessagesRequest(VIP, [1], PARTNER),
            EditMessageRequest(HUB, 5, message="x"),
        ):
            self.assertTrue(policy.blocked_write(request), request)
        for request in (
            SendMessageRequest(VIP, "x"),
            ForwardMessagesRequest(HUB, [1], VIP),
            DeleteMessagesRequest([1]),
            GetMessagesRequest(HUB, [1]),
        ):
            self.assertFalse(policy.blocked_write(request), request)

    def test_market_makers_general_is_stopped_but_its_topics_are_not(self):
        from telethon.tl.functions.messages import SendMediaRequest, SendMultiMediaRequest
        from telethon.tl.types import InputMediaEmpty, InputReplyToMessage

        mm = InputPeerChannel(3786932333, 0)
        topic8 = InputReplyToMessage(reply_to_msg_id=8)
        topic24 = InputReplyToMessage(reply_to_msg_id=5000, top_msg_id=24)
        for request in (
            SendMessageRequest(mm, "x"),
            SendMediaRequest(mm, InputMediaEmpty(), "x"),
            SendMultiMediaRequest(mm, []),
            ForwardMessagesRequest(VIP, [1], mm),
        ):
            self.assertTrue(policy.blocked_write(request), request)
        for request in (
            SendMessageRequest(mm, "x", reply_to=topic8),
            SendMessageRequest(mm, "x", reply_to=topic24),
            SendMultiMediaRequest(mm, [], reply_to=topic8),
            ForwardMessagesRequest(VIP, [1], mm, top_msg_id=28),
            EditMessageRequest(mm, 1117, message="x"),
            DeleteMessagesRequest([1]),
        ):
            self.assertFalse(policy.blocked_write(request), request)
        self.assertTrue(any(int(r["dest_chat"]) == -1003786932333 for r in ROUTES), "topic routes stay")

    def test_guard_refuses_before_reaching_telegram(self):
        client = FakeClient()
        policy.install_send_guard(client)
        policy.install_send_guard(client)  # idempotent
        with self.assertRaises(ChatWriteForbiddenError):
            asyncio.run(client._call(None, SendMessageRequest(HUB, "x")))
        with self.assertRaises(ChatWriteForbiddenError):
            asyncio.run(client._call(None, [SendMessageRequest(VIP, "x"), SendMessageRequest(PARTNER, "y")]))
        self.assertEqual(asyncio.run(client._call(None, SendMessageRequest(VIP, "x"))), "ok")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(
            policy.permanent_access_error(ChatWriteForbiddenError(request=None)), "ChatWriteForbiddenError"
        )


if __name__ == "__main__":
    unittest.main()

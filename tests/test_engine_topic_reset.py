"""One-time topic reset (6 Nov 2026): deletes only that topic's posts, once, and reports what is left."""

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import FloodWaitError

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker.engine import topic_reset

RELAY, TOPIC = -1004367822325, 8323


def post(mid, parent=TOPIC, top=None):
    return SimpleNamespace(id=mid, reply_to=SimpleNamespace(reply_to_msg_id=parent, reply_to_top_id=top))


ROOT_POST = SimpleNamespace(id=TOPIC, reply_to=None)


class Client:
    def __init__(self, posts, forbidden=(), floods=0, connected=True):
        self.posts = {m.id: m for m in posts if m is not None}
        self.listing = list(posts)
        self.forbidden, self.floods, self.connected = set(forbidden), floods, connected
        self.deletes, self.read = [], []

    def is_connected(self):
        was, self.connected = self.connected, True  # connected by the next check
        return was

    async def iter_messages(self, chat, reply_to=None):
        self.read.append((chat, reply_to))
        for message in self.listing:
            if message is None or message.id in self.posts:
                yield message

    async def delete_messages(self, chat, ids):
        self.deletes.append((chat, list(ids)))
        if self.floods:
            self.floods -= 1
            raise FloodWaitError(request=None, capture=7)
        if self.forbidden & set(ids):
            raise RuntimeError("MESSAGE_DELETE_FORBIDDEN")
        for one in ids:
            self.posts.pop(one, None)


class ResetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def reset(self, client):
        return await topic_reset.reset_topic_once(client, RELAY, TOPIC, self.tmp.name, self.sleep)

    async def test_deletes_only_the_topics_posts_then_never_again(self):
        other_topic = post(8500, parent=7250)
        client = Client([ROOT_POST, post(8401), post(8402, parent=8401, top=TOPIC), other_topic, None])
        with self.assertLogs(topic_reset.log, "WARNING") as logs:
            result = await self.reset(client)
        self.assertEqual(client.deletes, [(RELAY, [8401, 8402])])
        self.assertEqual(sorted(client.posts), [8323, 8500], "root and other topics are kept")
        self.assertEqual((result["found"], result["refused"], result["remaining"]), (2, [], []))
        self.assertIn(
            f"[TOPIC RESET DONE] chat={RELAY} topic={TOPIC} found=2 deleted=2 remaining=[]", logs.output[0]
        )
        saved = json.loads(topic_reset.state_path(self.tmp.name, RELAY, TOPIC).read_text(encoding="utf-8"))
        self.assertEqual(saved["found"], 2)
        client.posts[8403] = post(8403)
        client.listing.append(client.posts[8403])
        with self.assertLogs(topic_reset.log, "WARNING") as logs:
            self.assertIsNone(await self.reset(client))
        self.assertIn("[TOPIC RESET SKIPPED]", logs.output[0])
        self.assertIn(8403, client.posts, "a post after the reset is never deleted")

    async def test_large_topics_are_deleted_in_batches(self):
        client = Client([post(9000 + i) for i in range(150)])
        await self.reset(client)
        self.assertEqual([len(ids) for _, ids in client.deletes], [100, 50])
        self.assertEqual(client.posts, {})

    async def test_a_post_that_cannot_be_deleted_is_reported_and_the_rest_go(self):
        client = Client([post(8401), post(8402), post(8403)], forbidden={8402})
        with self.assertLogs(topic_reset.log, "WARNING") as logs:
            result = await self.reset(client)
        self.assertEqual(sorted(client.posts), [8402])
        self.assertEqual((result["refused"], result["remaining"]), ([8402], [8402]))
        self.assertTrue(any("[TOPIC RESET REFUSED]" in line and "id=8402" in line for line in logs.output))
        self.assertTrue(any("deleted=2 remaining=[8402]" in line for line in logs.output))

    async def test_flood_waits_are_slept_through(self):
        client = Client([post(8401)], floods=2)
        await self.reset(client)
        self.assertEqual((self.slept, client.posts), ([8, 8], {}))

    async def test_a_flood_wait_on_a_single_post_is_not_counted_as_refused(self):
        client = Client([post(8401), post(8402)], forbidden={8401})
        original = client.delete_messages

        async def flaky(chat, ids):
            if len(ids) == 1:
                raise FloodWaitError(request=None, capture=1)
            return await original(chat, ids)

        client.delete_messages = flaky
        with self.assertRaises(FloodWaitError):
            await self.reset(client)
        self.assertFalse(topic_reset.state_path(self.tmp.name, RELAY, TOPIC).exists(), "retried next start")

    async def test_endless_flood_wait_fails_the_batch_and_nothing_is_saved(self):
        client = Client([post(8401)], floods=99)
        with self.assertRaises(FloodWaitError):
            await self.reset(client)
        self.assertEqual(len(self.slept), topic_reset.FLOOD_ATTEMPTS - 1)
        self.assertFalse(topic_reset.state_path(self.tmp.name, RELAY, TOPIC).exists())

    async def test_reset_when_ready_waits_for_the_connection_and_retries_then_reports(self):
        client = Client([post(8401)], connected=False)
        result = await topic_reset.reset_when_ready(client, RELAY, TOPIC, self.tmp.name, sleep=self.sleep)
        self.assertEqual(self.slept, [0.25])
        self.assertEqual(result["found"], 1)

        broken = Client([post(8401, parent=9999)], floods=99)
        with self.assertLogs(topic_reset.log, "WARNING") as logs:
            result = await topic_reset.reset_when_ready(
                broken, RELAY, 9999, self.tmp.name, attempts=2, retry_seconds=3, sleep=self.sleep
            )
        self.assertIsNone(result)
        self.assertEqual(sum("[TOPIC RESET RETRY]" in line for line in logs.output), 2)
        self.assertIn(f"[TOPIC RESET FAILED] chat={RELAY} topic=9999 attempts=2", logs.output[-1])

    def test_topic_of(self):
        self.assertEqual(topic_reset.topic_of(post(1)), TOPIC)
        self.assertEqual(topic_reset.topic_of(post(1, parent=50, top=TOPIC)), TOPIC)
        self.assertIsNone(topic_reset.topic_of(ROOT_POST))


if __name__ == "__main__":
    unittest.main()

"""VIP General (topic 1) cleanup, run again 16 Nov 2026: only General posts go, every topic stays."""

import json
import logging
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import general_topic1_cleanup as cleanup

CHAT = cleanup.CHAT
MARKET_MAKERS = -1003786932333
DATE = object()


def general(mid, reply_to_msg=None):
    reply = SimpleNamespace(forum_topic=False, reply_to_top_id=None, reply_to_msg_id=reply_to_msg)
    return SimpleNamespace(id=mid, date=DATE, action=None, reply_to=reply if reply_to_msg else None)


def in_topic(mid, topic):
    reply = SimpleNamespace(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=topic)
    return SimpleNamespace(id=mid, date=DATE, action=None, reply_to=reply)


def service(mid, action="MessageActionTopicCreate"):
    return SimpleNamespace(id=mid, date=DATE, action=SimpleNamespace(kind=action), reply_to=None)


class Client:
    def __init__(self, messages, forum=True, chat=CHAT):
        self.messages = {m.id: m for m in messages}
        self.forum, self.chat = forum, chat
        self.deleted = []

    async def get_entity(self, chat):
        assert chat == self.chat
        return SimpleNamespace(forum=self.forum)

    async def get_messages(self, chat, limit=None, offset_id=None, ids=None, reply_to=None):
        assert chat == self.chat
        if ids is not None:
            return [self.messages.get(i) for i in ids]
        if reply_to is not None:
            found = [
                m for m in self.messages.values() if m.reply_to and m.reply_to.reply_to_msg_id == reply_to
            ]
            return sorted(found, key=lambda m: -m.id)[:limit]
        newest = sorted(self.messages.values(), key=lambda m: -m.id)
        if offset_id:
            newest = [m for m in newest if m.id < offset_id]
        return newest[:limit]

    async def delete_messages(self, chat, ids, revoke=True):
        self.deleted.append(list(ids))
        for one in ids:
            self.messages.pop(one, None)


class GeneralCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = logging.getLogger("general-test")

    def module(self, client):
        return SimpleNamespace(client=client, DATA_DIR=self.tmp.name, log=self.log)

    def test_a_new_state_file_so_it_runs_again(self):
        self.assertEqual(cleanup.STATE_FILENAME, "general_topic1_owner_delete_20261116_v1.json")
        self.assertEqual((cleanup.CHAT, cleanup.GENERAL), (-1003726286301, 1))

    def test_classification_fails_closed(self):
        self.assertTrue(cleanup._belongs_to_general(general(500)))
        self.assertTrue(cleanup._belongs_to_general(general(501, reply_to_msg=500)))
        self.assertFalse(cleanup._belongs_to_general(in_topic(502, 10877)))
        self.assertFalse(cleanup._belongs_to_general(service(9, "MessageActionTopicCreate")))
        self.assertFalse(cleanup._belongs_to_general(general(1)))
        self.assertFalse(cleanup._belongs_to_general(None))
        top = SimpleNamespace(forum_topic=False, reply_to_top_id=9, reply_to_msg_id=600)
        self.assertFalse(
            cleanup._belongs_to_general(SimpleNamespace(id=601, date=DATE, action=None, reply_to=top))
        )

    async def test_deletes_only_general_posts_once(self):
        keep = [service(9), service(10877), in_topic(10900, 10877), in_topic(10901, 9), service(10950, "Pin")]
        drop = [general(10800), general(10902), general(10903, reply_to_msg=10902)]
        client = Client(keep + drop)
        with self.assertLogs("general-test", "WARNING") as logs:
            state = await cleanup.remove_general_topic1_history_once(self.module(client))
        self.assertEqual(sorted(i for batch in client.deleted for i in batch), [10800, 10902, 10903])
        self.assertEqual(sorted(client.messages), sorted(m.id for m in keep), "topics and service posts kept")
        self.assertEqual((state["status"], state["deleted"]), ("done", 3))
        self.assertTrue(any("[GENERAL1 CLEANUP DONE]" in line for line in logs.output))
        saved = json.loads((Path(self.tmp.name) / cleanup.STATE_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "done")

        client.messages[11000] = general(11000)
        with self.assertLogs("general-test", "WARNING") as logs:
            await cleanup.remove_general_topic1_history_once(self.module(client))
        self.assertIn(11000, client.messages, "never runs a second time")
        self.assertIn("[GENERAL1 CLEANUP STATE SKIP]", logs.output[0])

    async def test_nothing_is_deleted_when_the_topic_check_cannot_be_proved(self):
        client = Client([general(10800), service(10877)])  # no ordinary post in 10877 to test against
        with self.assertRaisesRegex(RuntimeError, "cannot validate"):
            await cleanup.remove_general_topic1_history_once(self.module(client))
        self.assertEqual(client.deleted, [])
        unmarked = in_topic(10900, 10877)
        unmarked.reply_to.forum_topic = False  # a session that loses the topic marker
        client = Client([general(10800), service(10877), unmarked])
        with self.assertRaisesRegex(RuntimeError, "validation FAILED"):
            await cleanup.remove_general_topic1_history_once(self.module(client))
        self.assertEqual(client.deleted, [])
        with self.assertRaisesRegex(RuntimeError, "forum=True"):
            await cleanup.remove_general_topic1_history_once(self.module(Client([], forum=False)))


class WorkerWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_after_the_seeds_and_a_failure_is_only_logged(self):
        import ast
        import asyncio

        source = (ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8")
        self.assertIn(
            "owner_module.GENERAL_CLEANUP_TASK = asyncio.create_task(clear_general_topic_once())", source
        )
        node = next(
            n
            for n in ast.parse(source).body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "clear_general_topic_once"
        )
        calls = []

        async def fake_remove(module, logger=None, **options):
            calls.append(options)
            raise RuntimeError("cannot validate")

        gate = asyncio.Event()
        mm = {"chat": MARKET_MAKERS, "known_topic": 8, "state_filename": "mm.json"}
        ns = {
            "asyncio": asyncio,
            "SEED_GATES": {"seed": gate},
            "GENERAL_CLEANUPS": ({}, mm),
            "log": logging.getLogger("general-test"),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<worker>", "exec"), ns)
        original = cleanup.remove_general_topic1_history_once
        cleanup.remove_general_topic1_history_once = fake_remove
        try:
            task = asyncio.create_task(ns["clear_general_topic_once"]())
            await asyncio.sleep(0)
            self.assertEqual(calls, [], "waits for the seed")
            gate.set()
            with self.assertLogs("general-test", "ERROR") as logs:
                await asyncio.wait_for(task, 1)
        finally:
            cleanup.remove_general_topic1_history_once = original
        self.assertEqual(calls, [{}, mm], "a failure on one group never stops the next")
        self.assertIn("[GENERAL1 CLEANUP FAILED SAFE] chat=VIP RuntimeError: cannot validate", logs.output[0])
        self.assertIn(f"chat={MARKET_MAKERS} RuntimeError", logs.output[1])
        self.assertIn('"chat": -1003786932333', source)
        self.assertIn('"known_topic": (8, 24, 28)', source)
        self.assertIn('"state_filename": "general_topic1_market_makers_delete_20261119_v1.json"', source)


class MarketMakersTests(unittest.IsolatedAsyncioTestCase):
    async def test_clears_market_makers_general_only_and_reports_senders(self):
        fwd = SimpleNamespace(from_id=SimpleNamespace(channel_id=555))
        posts = [
            SimpleNamespace(id=2000, date=DATE, action=None, reply_to=None, fwd_from=fwd, sender_id=-1001),
            SimpleNamespace(id=2001, date=DATE, action=None, reply_to=None, fwd_from=None, sender_id=42),
            SimpleNamespace(
                id=2002, date=DATE, action=None, reply_to=None, fwd_from=None, sender_id=7, via_bot_id=99
            ),
        ]
        keep = [service(8), in_topic(2003, 24), in_topic(2004, 28), service(28)]  # topic 8 has no posts
        client = Client(posts + keep, chat=MARKET_MAKERS)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        module = SimpleNamespace(client=client, DATA_DIR=tmp.name, log=logging.getLogger("general-test"))
        with self.assertLogs("general-test", "WARNING") as logs:
            state = await cleanup.remove_general_topic1_history_once(
                module, chat=MARKET_MAKERS, known_topic=(8, 24, 28), state_filename="mm.json"
            )
        self.assertEqual(sorted(client.messages), [8, 28, 2003, 2004], "topic posts and topic roots kept")
        checks = [line for line in logs.output if "[GENERAL1 TOPIC CHECK]" in line]
        self.assertEqual(len(checks), 3)
        self.assertIn("topic=8 ordinary_posts=0", checks[0])
        self.assertIn("topic=24 ordinary_posts=1", checks[1])
        self.assertEqual((state["chat"], state["deleted"]), (MARKET_MAKERS, 3))
        self.assertEqual(state["senders"], {"fwd:555": 1, "sender:42": 1, "via_bot:99": 1})
        self.assertTrue(any("[GENERAL1 SENDERS]" in line for line in logs.output))
        self.assertTrue((Path(tmp.name) / "mm.json").exists())
        self.assertFalse((Path(tmp.name) / cleanup.STATE_FILENAME).exists(), "VIP state untouched")

    def test_sender_key_falls_back(self):
        self.assertEqual(cleanup.sender_key(SimpleNamespace()), "unknown")
        user_fwd = SimpleNamespace(fwd_from=SimpleNamespace(from_id=SimpleNamespace(user_id=5)))
        self.assertEqual(cleanup.sender_key(user_fwd), "fwd:5")
        hidden = SimpleNamespace(fwd_from=SimpleNamespace(from_id=None), sender_id=3)
        self.assertEqual(cleanup.sender_key(hidden), "sender:3")


if __name__ == "__main__":
    unittest.main()

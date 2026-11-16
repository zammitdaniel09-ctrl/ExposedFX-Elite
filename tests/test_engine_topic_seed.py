"""One-time topic seed (16 Nov 2026): clear VIP 10877, copy the last 50 sendable posts, then go live."""

import ast
import asyncio
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
from telegram_worker import route_table
from telegram_worker.engine import topic_reset, topic_seed

SOURCE, VIP, TOPIC = -1003838973021, -1003726286301, 10877
ROUTE = route_table.one("Private Live 3838973021 To 10877").as_dict()


def post(mid, grouped=None, text="signal"):
    return SimpleNamespace(id=mid, grouped_id=grouped, message=text, reply_to=None)


def topic_post(mid):
    return SimpleNamespace(id=mid, reply_to=SimpleNamespace(reply_to_msg_id=TOPIC, reply_to_top_id=None))


class Client:
    def __init__(self, source_posts, topic_posts=(), connected=True):
        self.source = list(source_posts)
        self.topic = {m.id: m for m in topic_posts}
        self.connected = connected
        self.deletes, self.reads = [], []

    def is_connected(self):
        return self.connected

    async def iter_messages(self, chat, limit=None, reply_to=None):
        self.reads.append((chat, limit, reply_to))
        if chat == VIP:
            for message in list(self.topic.values()):
                yield message
            return
        for message in sorted(self.source, key=lambda m: -m.id)[:limit]:
            yield message

    async def delete_messages(self, chat, ids):
        self.deletes.append((chat, list(ids)))
        for one in ids:
            self.topic.pop(one, None)


def sendable(route, unit):
    return "promo" not in unit[0].message


class SeedTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.slept, self.sent = [], []
        self.fail_ids, self.flood_once = set(), set()

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def deliver(self, route, unit):
        first = unit[0].id
        if first in self.flood_once:
            self.flood_once.discard(first)
            raise FloodWaitError(request=None, capture=4)
        if first in self.fail_ids:
            raise RuntimeError("boom")
        self.sent.append([m.id for m in unit])
        return True

    async def run_seed(self, client, last=50, gate=None):
        gate = gate or asyncio.Event()
        result = await topic_seed.clear_then_seed(
            client, ROUTE, last, self.tmp.name, self.deliver, sendable, gate, self.sleep
        )
        self.assertTrue(gate.is_set(), "the gate always opens")
        return result

    def test_the_route_replaces_relay_7248_and_keeps_topic_9(self):
        names = {r["name"]: r for r in route_table.routes("MAIN", "intake")}
        self.assertNotIn("Relay7248 To VIP10877", names)
        self.assertIn("Private Live 3838973021 To 9", names)
        self.assertEqual((ROUTE["source_chat"], ROUTE["source_topic"]), (SOURCE, None))
        self.assertEqual((ROUTE["dest_chat"], ROUTE["dest_topic"]), (VIP, TOPIC))
        self.assertEqual(ROUTE["loop_guard_peers"], [VIP])
        self.assertNotIn("from_start", ROUTE)

    async def test_clears_the_topic_then_sends_the_last_n_sendable_posts_oldest_first(self):
        source = [post(i) for i in range(1, 71)] + [post(71, text="promo"), post(72)]
        client = Client(source, topic_posts=[topic_post(20001), topic_post(20002)])
        result = await self.run_seed(client)
        self.assertEqual(client.deletes, [(VIP, [20001, 20002])])
        self.assertEqual(client.topic, {})
        self.assertEqual(self.sent, [[i] for i in range(22, 71)] + [[72]], "promo skipped, 50 sent")
        self.assertEqual((result["selected"], result["handled"], result["failed"]), (50, 50, []))
        self.assertEqual((result["first_source_id"], result["last_source_id"]), (22, 72))
        self.assertTrue(topic_reset.is_own_deletion(VIP, 20001))
        self.assertFalse(topic_reset.is_own_deletion(VIP, 99999))
        self.assertFalse(topic_reset.is_own_deletion(None, 20001))
        saved = json.loads(topic_seed.state_path(self.tmp.name, ROUTE).read_text(encoding="utf-8"))
        self.assertEqual(saved["handled"], 50)

    async def test_runs_once_only(self):
        await self.run_seed(Client([post(1), post(2)]))
        client = Client([post(1), post(2), post(3)], topic_posts=[topic_post(20009)])
        self.sent.clear()
        with self.assertLogs(topic_seed.log, "WARNING") as logs:
            self.assertIsNone(await self.run_seed(client))
        self.assertEqual((self.sent, client.deletes), ([], []), "no second clear, no second seed")
        self.assertIn("[TOPIC SEED SKIPPED]", logs.output[0])

    async def test_albums_are_one_post_and_a_cut_off_album_is_dropped(self):
        source = [post(1, grouped=7), post(2, grouped=7), post(3), post(4, grouped=8), post(5, grouped=8)]
        await self.run_seed(Client(source), last=2)
        self.assertEqual(self.sent, [[3], [4, 5]])
        self.sent.clear()
        old = topic_seed.FETCH_LIMIT
        topic_seed.FETCH_LIMIT = 3  # reads 3, 4, 5: no partial album at the edge, nothing dropped
        try:
            units = await topic_seed.last_units(Client(source), ROUTE, 10, sendable)
            self.assertEqual([[m.id for m in u] for u in units], [[3], [4, 5]])
            topic_seed.FETCH_LIMIT = 4  # reads 2..5: album 7 is cut off, so it is left out
            units = await topic_seed.last_units(Client(source), ROUTE, 10, sendable)
            self.assertEqual([[m.id for m in u] for u in units], [[3], [4, 5]])
        finally:
            topic_seed.FETCH_LIMIT = old
        self.assertEqual(await topic_seed.last_units(Client(source), ROUTE, 0, sendable), [])

    async def test_flood_waits_are_slept_and_a_failing_post_is_reported_not_fatal(self):
        self.flood_once, self.fail_ids = {2}, {3}
        with self.assertLogs(topic_seed.log, "WARNING") as logs:
            result = await self.run_seed(Client([post(1), post(2), post(3), post(4)]))
        self.assertEqual(self.sent, [[1], [2], [4]])
        self.assertEqual((result["handled"], result["failed"]), (3, [[3]]))
        self.assertIn(5, self.slept, "flood wait of 4 s slept as 5")
        self.assertTrue(any("[TOPIC SEED POST FAILED]" in line for line in logs.output))

    async def test_a_send_that_returns_false_is_retried_then_given_up(self):
        calls = []

        async def refuse(route, unit):
            calls.append(unit[0].id)
            return False

        self.deliver = refuse
        result = await self.run_seed(Client([post(1)]))
        self.assertEqual((len(calls), result["failed"]), (topic_seed.SEND_ATTEMPTS, [[1]]))

    async def test_no_seed_when_the_reset_did_not_finish(self):
        client = Client([post(1)], topic_posts=[topic_post(20001)])

        async def broken(chat, ids):
            raise FloodWaitError(request=None, capture=1)

        client.delete_messages = broken
        old = topic_reset.FLOOD_ATTEMPTS
        topic_reset.FLOOD_ATTEMPTS = 1
        try:
            with self.assertLogs(topic_seed.log, "ERROR") as logs:
                self.assertIsNone(await self.run_seed(client))
        finally:
            topic_reset.FLOOD_ATTEMPTS = old
        self.assertEqual(self.sent, [])
        self.assertIn("reset_not_done=True", logs.output[-1])
        self.assertFalse(topic_seed.state_path(self.tmp.name, ROUTE).exists())

    async def test_an_unexpected_error_is_reported_and_the_gate_still_opens(self):
        client = Client([post(1)])
        topic_reset.state_path(self.tmp.name, VIP, TOPIC).write_text("{}", encoding="utf-8")  # reset done

        async def explode(*args, **kwargs):
            raise RuntimeError("no network")
            yield  # pragma: no cover

        client.iter_messages = explode
        with self.assertLogs(topic_seed.log, "ERROR") as logs:
            self.assertIsNone(await self.run_seed(client))
        self.assertTrue(any("[TOPIC SEED FAILED]" in line for line in logs.output))

    async def test_the_gate_holds_live_posts_until_the_seed_ends_with_a_time_limit(self):
        await topic_seed.wait_for_gate(None)
        gate = asyncio.Event()
        waiter = asyncio.create_task(topic_seed.wait_for_gate(gate))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        gate.set()
        await asyncio.wait_for(waiter, 1)
        with self.assertLogs(topic_seed.log, "WARNING") as logs:
            await topic_seed.wait_for_gate(asyncio.Event(), timeout=0.01)
        self.assertIn("[TOPIC SEED GATE TIMEOUT]", logs.output[0])


TREE = ast.parse((ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8"))


def worker_functions(*names, **extra):
    """The named worker_fixed functions, without their decorators, in a namespace of `extra`."""
    nodes = [
        n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(nodes) == len(names)
    for node in nodes:
        node.decorator_list = []
    ns = {"asyncio": asyncio, **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<worker>", "exec"), ns)
    return ns


class WorkerWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_the_seed_is_configured_for_the_new_route_only(self):
        source = (ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8")
        self.assertIn('TOPIC_SEEDS = {"Private Live 3838973021 To 10877": 50}', source)
        self.assertIn("owner_module.TOPIC_SEED_TASKS = start_topic_seeds()", source)

    async def test_start_topic_seeds_starts_the_seed_or_opens_the_gate(self):
        started = []

        async def fake_clear_then_seed(*args):
            started.append(args)

        gate = asyncio.Event()
        ns = worker_functions(
            "start_topic_seeds",
            TOPIC_SEEDS={ROUTE["name"]: 50},
            SEED_GATES={ROUTE["name"]: gate},
            fast_vip_routes=lambda: [ROUTE],
            client="client",
            DATA_DIR="data",
            live_deliver="deliver",
            intake_pending="pending",
        )
        original = topic_seed.clear_then_seed
        topic_seed.clear_then_seed = fake_clear_then_seed
        try:
            tasks = ns["start_topic_seeds"]()
            await asyncio.gather(*tasks)
            self.assertEqual(started, [("client", ROUTE, 50, "data", "deliver", "pending", gate)])
            self.assertFalse(gate.is_set(), "the seed opens it")
            ns["fast_vip_routes"] = lambda: []
            self.assertEqual(ns["start_topic_seeds"](), [])
            self.assertTrue(gate.is_set(), "no route: nothing is ever held")
        finally:
            topic_seed.clear_then_seed = original

    async def test_only_the_seeded_route_waits_for_the_gate(self):
        sent = []

        async def fast_vip_deliver_route(route, unit, skip_permanent_errors=False):
            sent.append((route["name"], skip_permanent_errors))
            return True

        gate = asyncio.Event()
        ns = worker_functions(
            "live_deliver",
            "intake_deliver",
            SEED_GATES={ROUTE["name"]: gate},
            fast_vip_deliver_route=fast_vip_deliver_route,
        )
        topic9 = route_table.one("Private Live 3838973021 To 9").as_dict()
        self.assertTrue(await ns["intake_deliver"](topic9, [post(1)], "live"))
        held = asyncio.create_task(ns["intake_deliver"](ROUTE, [post(1)], "live"))
        await asyncio.sleep(0)
        self.assertEqual(sent, [(topic9["name"], True)])
        gate.set()
        self.assertTrue(await held)
        self.assertEqual(sent[-1], (ROUTE["name"], True))

    async def test_a_resets_own_deletions_never_reach_the_delete_mirror(self):
        mirrored = []

        class Mirror:
            async def on_delete(self, chat, ids):
                mirrored.append((chat, ids))

        ns = worker_functions(
            "on_message_deleted", MIRROR_DELETED_MESSAGES=True, MIRROR=Mirror(), log=None, alert_crash=None
        )
        topic_reset.OWN_DELETIONS.update({(VIP, 30001), (VIP, 30002)})
        try:
            await ns["on_message_deleted"](SimpleNamespace(chat_id=VIP, deleted_ids=[30001, 30002]))
            self.assertEqual(mirrored, [])
            await ns["on_message_deleted"](SimpleNamespace(chat_id=VIP, deleted_ids=[30001, 30003]))
            await ns["on_message_deleted"](SimpleNamespace(chat_id=SOURCE, deleted_ids=[], deleted_id=5))
            self.assertEqual(mirrored, [(VIP, [30003]), (SOURCE, [5])])
        finally:
            topic_reset.OWN_DELETIONS.difference_update({(VIP, 30001), (VIP, 30002)})


if __name__ == "__main__":
    unittest.main()

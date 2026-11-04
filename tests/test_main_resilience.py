"""MAIN keeps its long-running jobs alive, and a flood wait at start-up is retried, not forgotten."""

import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import FloodWaitError

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "telegram_worker/worker_fixed.py").read_text(encoding="utf-8"))


def load_functions(*names, **extra):
    ns = {"asyncio": asyncio, "log": logging.getLogger("main-test"), **extra}
    nodes = [
        n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<main>", "exec"), ns)
    return ns


class SupervisedTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_crashed_job_is_logged_and_started_again(self):
        runs = []

        async def job():
            runs.append(1)
            if len(runs) < 3:
                raise RuntimeError("state file unreadable")

        ns = load_functions("supervised", SUPERVISED_RESTART_SECONDS=0)
        with self.assertLogs("main-test", "ERROR") as logs:
            await ns["supervised"]("owner_routes", job)
        self.assertEqual(len(runs), 3, "restarted until it ran to the end")
        self.assertIn("[TASK CRASHED] task=owner_routes RuntimeError: state file unreadable", logs.output[0])

    async def test_a_job_that_finishes_is_not_restarted_and_cancel_stops_it(self):
        runs = []

        async def done():
            runs.append(1)

        ns = load_functions("supervised", SUPERVISED_RESTART_SECONDS=0)
        await ns["supervised"]("finished", done)
        self.assertEqual(runs, [1])

        async def forever():
            await asyncio.Event().wait()

        task = ns["supervised"]("forever", forever)
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(task.get_name(), "forever")


class Dialogs:
    def __init__(self, flood_first):
        self.flood_first = flood_first
        self.calls = 0

    def iter_dialogs(self):
        self.calls += 1
        flood = self.calls == 1 and self.flood_first

        async def gen():
            if flood:
                raise FloodWaitError(request=None, capture=120)
            yield "dialog"

        return gen()

    async def get_input_entity(self, chat_id):
        return chat_id


class WarmupTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_flood_wait_is_retried_and_holds_entity_strikes_meanwhile(self):
        policy = SimpleNamespace(ENTITY_CACHE_READY=True)
        client = Dialogs(flood_first=True)
        slept, tasks = [], []

        async def fake_sleep(seconds):
            slept.append(seconds)

        fake_asyncio = SimpleNamespace(sleep=fake_sleep, create_task=lambda coro: tasks.append(coro) or coro)
        ns = load_functions(
            "warm_entity_cache_once",
            "retry_entity_cache_warmup",
            asyncio=fake_asyncio,
            client=client,
            owner_policy=policy,
            FloodWaitError=FloodWaitError,
            WARM_ENTITY_CACHE_ON_START=True,
            ROUTES=[{"source_chat": -1001, "dest_chat": -1002}],
            WARMUP_RETRY=[],
        )
        with self.assertLogs("main-test", "WARNING"):
            await ns["warm_entity_cache_once"]()
        self.assertFalse(policy.ENTITY_CACHE_READY, "'not found' is not lost access while loading")
        self.assertEqual(len(tasks), 1, "retried in the background")
        with self.assertLogs("main-test", "WARNING") as logs:
            await tasks[0]
        self.assertEqual(slept, [121])
        self.assertTrue(policy.ENTITY_CACHE_READY)
        self.assertIn("[ENTITY CACHE WARMUP DONE] dialogs=1 route_peers=2 resolved=2", logs.output[0])


if __name__ == "__main__":
    unittest.main()

"""SQLite message map: dict behaviour, legacy JSON merge, rollback snapshots."""

import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from unittest.mock import patch

from telegram_worker import reply_hardening, state_store
from telegram_worker.state_store import SQLiteMap, open_message_map


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.json = self.dir / "message_map.json"
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.tmp.cleanup()

    def open(self, **kwargs):
        store = SQLiteMap(self.dir / "message_map.sqlite3", legacy_json=self.json, **kwargs)
        self.stores.append(store)
        return store

    def test_behaves_like_the_dict_it_replaces(self):
        store = self.open()
        store["-100:1:-200:5"] = 900
        store["-100:2:-200:5"] = [901, 902]
        self.assertEqual(store.get("-100:1:-200:5"), 900)
        self.assertEqual(store["-100:2:-200:5"], [901, 902])
        self.assertIsNone(store.get("missing"))
        self.assertIn("-100:1:-200:5", store)
        self.assertEqual(len(store), 2)
        self.assertEqual(sorted(store), ["-100:1:-200:5", "-100:2:-200:5"])
        for key, _ in store.items():  # callers delete while iterating items()
            del store[key]
        self.assertEqual(len(store), 0)
        self.assertEqual(store.pop("gone", "default"), "default")
        with self.assertRaises(KeyError):
            store["gone"]

    def test_first_start_imports_the_json_map_and_survives_restart(self):
        self.json.write_text(json.dumps({"a:1:b:2": 7, "a:2:b:2": [8, 9]}), encoding="utf-8")
        store = self.open()
        self.assertEqual((store.imported, store["a:2:b:2"]), (2, [8, 9]))
        store["a:3:b:2"] = 10
        reopened = self.open()
        self.assertEqual(reopened.imported, 0, "nothing new in the JSON")
        self.assertEqual(reopened["a:3:b:2"], 10)

    def test_rollback_entries_are_merged_but_never_overwrite(self):
        store = self.open()
        store["a:1:b:2"] = 7
        self.json.write_text(json.dumps({"a:1:b:2": 999, "a:5:b:2": 11}), encoding="utf-8")
        reopened = self.open()
        self.assertEqual((reopened["a:1:b:2"], reopened["a:5:b:2"], reopened.imported), (7, 11, 1))

    def test_unreadable_json_is_left_to_the_worker_loader(self):
        calls = []
        store = self.open(load_legacy=lambda: calls.append(1) or {})
        self.assertEqual((calls, len(store)), ([1], 0))

    def test_snapshot_writes_a_complete_json_copy_and_is_rate_limited(self):
        store = self.open()
        for i in range(50):
            store[f"a:{i}:b:2"] = i
        self.assertTrue(store.snapshot_to())
        store.wait_snapshot()
        self.assertEqual(len(json.loads(self.json.read_text(encoding="utf-8"))), 50)
        self.assertFalse(store.snapshot_to(), "at most once per SNAPSHOT_SECONDS")
        self.assertTrue(store.snapshot_to(force=True))
        store.wait_snapshot()

    def test_snapshot_while_writing(self):
        store = self.open()
        done = threading.Event()

        def writer():
            for i in range(300):
                store[f"w:{i}:b:2"] = i
            done.set()

        thread = threading.Thread(target=writer)
        thread.start()
        store.snapshot_to(force=True)
        thread.join()
        store.wait_snapshot()
        self.assertTrue(done.is_set())
        self.assertEqual(len(store), 300)

    def test_reply_recovery_still_caches_into_the_store(self):
        worker = types.SimpleNamespace(message_map=self.open(), saves=[])
        worker.save_map = lambda: worker.saves.append(1)
        route = {"source_chat": -100, "dest_chat": -200, "dest_topic": 5}
        reply_hardening._cache_mapping(worker, route, 41, 777)
        self.assertEqual((worker.message_map["-100:41:-200:5"], worker.saves), (777, [1]))
        reply_hardening._drop_exact_mapping(worker, route, 41)
        self.assertNotIn("-100:41:-200:5", worker.message_map)

    def test_shutdown_writes_the_current_map_then_ends_as_before(self):
        store = self.open()
        store["-100:1:-200:5"] = 900
        installed, killed = [], []
        with patch.object(
            state_store.signal, "signal", lambda sig, handler: installed.append(handler)
        ), patch.object(state_store.signal, "getsignal", lambda sig: None), patch.object(
            state_store.os, "kill", lambda pid, sig: killed.append(sig)
        ):
            self.assertTrue(store.snapshot_on_exit())
            installed[0](state_store.signal.SIGTERM, None)
        self.assertEqual(json.loads(self.json.read_text(encoding="utf-8")), {"-100:1:-200:5": 900})
        self.assertEqual(installed[1], state_store.signal.SIG_DFL, "the default action is restored")
        self.assertEqual(killed, [state_store.signal.SIGTERM])

    def test_shutdown_waits_for_in_flight_sends_before_the_snapshot(self):
        import asyncio

        store = self.open()
        store["-100:1:-200:5"] = 900
        installed, killed, drained = [], [], []

        async def hook():
            await asyncio.sleep(0)
            store["-100:2:-200:5"] = 901  # a send in flight records its copy
            drained.append(1)

        async def scenario():
            installed[0](state_store.signal.SIGTERM, None)
            for _ in range(20):
                await asyncio.sleep(0.01)
                if killed:
                    break

        with patch.object(
            state_store.signal, "signal", lambda sig, handler: installed.append(handler)
        ), patch.object(state_store.signal, "getsignal", lambda sig: None), patch.object(
            state_store.os, "kill", lambda pid, sig: killed.append(sig)
        ), patch.object(
            state_store, "SHUTDOWN_HOOKS", [hook]
        ):
            self.assertTrue(store.snapshot_on_exit())
            asyncio.run(scenario())
        self.assertEqual((drained, killed), ([1], [state_store.signal.SIGTERM]))
        self.assertEqual(
            json.loads(self.json.read_text(encoding="utf-8")), {"-100:1:-200:5": 900, "-100:2:-200:5": 901}
        )

    def test_register_shutdown_hook(self):
        async def hook():
            return None

        with patch.object(state_store, "SHUTDOWN_HOOKS", []):
            state_store.register_shutdown_hook(hook)
            self.assertEqual(state_store.SHUTDOWN_HOOKS, [hook])

    def test_shutdown_snapshot_needs_a_target_and_the_main_thread(self):
        store = SQLiteMap(self.dir / "other.sqlite3")
        self.stores.append(store)
        self.assertFalse(store.snapshot_on_exit())

        def refuse(sig, handler):
            raise ValueError("signal only works in main thread")

        with patch.object(state_store.signal, "signal", refuse):
            self.assertFalse(self.open().snapshot_on_exit())

    def test_open_message_map_names_the_database_next_to_the_json(self):
        store = open_message_map(self.dir, self.json)
        self.stores.append(store)
        self.assertEqual(store.path, self.dir / "message_map.sqlite3")

    def test_snapshot_temp_files_a_kill_left_behind_are_removed_at_start(self):
        orphan = self.dir / "message_map.json.1234.snapshot.tmp"
        other = self.dir / "route_state.json.tmp"
        orphan.write_text("{", encoding="utf-8")
        other.write_text("{}", encoding="utf-8")
        self.open()
        self.assertFalse(orphan.exists())
        self.assertTrue(other.exists(), "only this map's snapshot temp files")

    def test_shutdown_waits_only_briefly_for_a_background_snapshot(self):
        store = self.open()
        handlers, waits = [], []
        with patch.object(
            state_store.signal, "signal", lambda sig, handler: handlers.append(handler)
        ), patch.object(state_store.signal, "getsignal", lambda sig: None), patch.object(
            state_store.os, "kill", lambda pid, sig: None
        ), patch.object(
            store, "wait_snapshot", lambda timeout=30.0: waits.append(timeout)
        ):
            store.snapshot_on_exit()
            handlers[0](state_store.signal.SIGTERM, None)
        self.assertEqual(waits, [state_store.EXIT_SNAPSHOT_WAIT_SECONDS])
        self.assertLess(state_store.DRAIN_SECONDS + state_store.EXIT_SNAPSHOT_WAIT_SECONDS, 3.0)


class SendGuardTests(unittest.TestCase):
    """Sends outside the intake (owner routes, SamGtrades) finish before exit."""

    def test_shutdown_waits_for_a_send_in_flight_and_starts_no_new_one(self):
        import asyncio

        guard = state_store.SendGuard(timeout=2.0)
        events = []

        async def send(name, release):
            async with guard.sending():
                events.append(f"{name} started")
                await release.wait()
                events.append(f"{name} recorded")

        async def scenario():
            first = asyncio.Event()
            sending = asyncio.create_task(send("first", first))
            await asyncio.sleep(0.01)
            draining = asyncio.create_task(guard.drain())
            await asyncio.sleep(0.01)
            late = asyncio.create_task(send("late", asyncio.Event()))
            await asyncio.sleep(0.01)
            first.set()
            await draining
            await sending
            late_started = late.done()
            late.cancel()
            return late_started

        with patch.object(state_store, "SHUTDOWN_HOOKS", []):
            with self.assertLogs("state-store", "WARNING") as logs:
                late_started = asyncio.run(scenario())
            self.assertEqual(state_store.SHUTDOWN_HOOKS, [guard.drain], "registered on first use")
        self.assertEqual(events, ["first started", "first recorded"])
        self.assertFalse(late_started, "a send after SIGTERM waits for the exit")
        self.assertIn("[SENDS DRAINED] in_flight=0", logs.output[0])
        self.assertEqual(guard.inflight, 0)

    def test_drain_is_bounded(self):
        import asyncio

        guard = state_store.SendGuard(timeout=0.05)
        guard.inflight = 1  # a send that never returns
        with self.assertLogs("state-store", "WARNING") as logs:
            asyncio.run(guard.drain())
        self.assertIn("in_flight=1", logs.output[0])


if __name__ == "__main__":
    unittest.main()

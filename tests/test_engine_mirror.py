"""Edits and deletions mirrored onto copies (engine/mirror.py, MAIN and ALT)."""

import asyncio
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import MessageEntityBold, MessageMediaPhoto, Photo

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker.engine import mirror
from telegram_worker.state_store import SQLiteMap

RELAY, VIP, MM = -1004367822325, -1003726286301, -1003786932333
RELAY2 = {
    "name": "VIP RELAY Topic2 To VIP1364",
    "source_chat": RELAY,
    "source_topic": 2,
    "dest_chat": VIP,
    "dest_topic": 1364,
}
PARTNER = {
    "name": "Owner VIP1364 To Partner28",
    "source_chat": VIP,
    "source_topic": 1364,
    "dest_chat": MM,
    "dest_topic": 28,
}
PHOTO = MessageMediaPhoto(photo=Photo(id=1, access_hash=1, file_reference=b"", date=None, sizes=[], dc_id=1))
EDITED = datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc)


def post(msg_id, text="TP1 hit", media=None, entities=None, edit_date=EDITED):
    return SimpleNamespace(
        id=msg_id, message=text, raw_text=text, text=text, media=media, entities=entities, edit_date=edit_date
    )


class Flood(Exception):
    seconds = 7


class Client:
    def __init__(self, stored=None, fail=None):
        self.stored = stored or {}
        self.fail = fail
        self.edits, self.deleted = [], []

    async def edit_message(self, chat, msg_id, text, **kwargs):
        if self.fail:
            raise self.fail
        self.edits.append((chat, msg_id, text))

    async def get_messages(self, chat, ids):
        return self.stored.get((chat, ids))

    async def delete_messages(self, chat, ids):
        if self.fail:
            raise self.fail
        self.deleted.append((chat, list(ids)))


async def plain(route, message):
    return message.message, message.entities


async def shouting(route, message):
    return message.message.upper(), None


def build(mapping, client, **kwargs):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    sync = mirror.Mirror("TEST", client, mapping, kwargs.pop("render", plain), sleep=sleep, **kwargs)
    return sync, slept


class EditTests(unittest.TestCase):
    def test_copy_is_edited_in_place_as_the_route_renders_it(self):
        client = Client()
        sync, _ = build({f"{RELAY}:10:{VIP}:1364": 900}, client, render=shouting)
        self.assertTrue(asyncio.run(sync.edit(RELAY2, post(10, "tp1 hit"))))
        self.assertEqual(client.edits, [(VIP, 900, "TP1 HIT")])

    def test_no_copy_or_album_copy_is_left_alone(self):
        client = Client()
        sync, _ = build({f"{RELAY}:11:{VIP}:1364": [901, 902]}, client)
        self.assertFalse(asyncio.run(sync.edit(RELAY2, post(10))))
        with self.assertLogs("engine-mirror", "WARNING"):
            self.assertFalse(asyncio.run(sync.edit(RELAY2, post(11))))
        self.assertEqual(client.edits, [])

    def test_unchanged_copy_counts_as_done_and_other_errors_as_not_applied(self):
        class NotModified(Exception):
            pass

        sync, _ = build(
            {f"{RELAY}:10:{VIP}:1364": 900},
            Client(fail=NotModified("Content of the message was not modified")),
        )
        self.assertTrue(asyncio.run(sync.edit(RELAY2, post(10))))
        sync, _ = build({f"{RELAY}:10:{VIP}:1364": 900}, Client(fail=RuntimeError("MESSAGE_ID_INVALID")))
        with self.assertLogs("engine-mirror", "WARNING"):
            self.assertFalse(asyncio.run(sync.edit(RELAY2, post(10))))

    def test_flood_wait_is_raised_by_edit_and_waited_out_by_the_handler(self):
        sync, slept = build({f"{RELAY}:10:{VIP}:1364": 900}, Client(fail=Flood("wait")))
        with self.assertRaises(Flood):
            asyncio.run(sync.edit(RELAY2, post(10)))
        with self.assertLogs("engine-mirror", "WARNING"):
            asyncio.run(sync.on_edit(RELAY, post(10), [RELAY2]))
        self.assertEqual(slept, [8.0])

    def test_unexpected_handler_error_is_logged_not_raised(self):
        async def broken(route, message):
            raise ValueError("render failed")

        sync, _ = build({f"{RELAY}:10:{VIP}:1364": 900}, Client(), render=broken)
        with self.assertLogs("engine-mirror", "ERROR"):
            asyncio.run(sync.on_edit(RELAY, post(10), [RELAY2]))

    def test_blocked_edits_are_not_passed_on(self):
        client = Client()
        sync, _ = build({f"{RELAY}:10:{VIP}:1364": 900}, client, blocked=lambda chat, m: "t.me" in m.message)
        asyncio.run(sync.on_edit(RELAY, post(10, "join t.me/x"), [RELAY2]))
        self.assertEqual(client.edits, [])

    def test_edit_cascades_to_copies_of_the_copy_once(self):
        client = Client(stored={(VIP, 900): post(900, "TP1 hit")})
        mapping = {f"{RELAY}:10:{VIP}:1364": 900, f"{VIP}:900:{MM}:28": 5000}
        routes = {(VIP, 1364): [PARTNER, PARTNER]}
        sync, _ = build(
            mapping, client, routes_from=lambda chat, topic: routes.get((chat, topic), []), cascade_depth=4
        )
        self.assertTrue(asyncio.run(sync.edit(RELAY2, post(10))))
        self.assertEqual(client.edits, [(VIP, 900, "TP1 hit"), (MM, 5000, "TP1 hit")])

    def test_cascade_survives_a_missing_or_unreadable_copy(self):
        class Broken(Client):
            async def get_messages(self, chat, ids):
                raise ConnectionError("offline")

        mapping = {f"{RELAY}:10:{VIP}:1364": 900}
        for client in (Client(), Broken()):
            sync, _ = build(mapping, client, routes_from=lambda chat, topic: [PARTNER], cascade_depth=4)
            self.assertTrue(asyncio.run(sync.edit(RELAY2, post(10))))


class AltEditTests(unittest.TestCase):
    """ALT: never change a copy's media structure; wait briefly for a just-sent copy."""

    def test_media_added_by_the_edit_is_skipped(self):
        client = Client(stored={(RELAY, 900): post(900)})
        sync, _ = build({f"{RELAY}:10:{RELAY}:2": 900}, client, same_media_only=True)
        route = {**RELAY2, "dest_chat": RELAY, "dest_topic": 2}
        with self.assertLogs("engine-mirror", "WARNING") as logs:
            self.assertFalse(asyncio.run(sync.edit(route, post(10, media=PHOTO))))
        self.assertIn("MEDIA STRUCTURE CHANGE SKIPPED", logs.output[0])
        self.assertEqual(client.edits, [])

    def test_deleted_copy_and_empty_text_are_skipped(self):
        route = {**RELAY2, "dest_chat": RELAY, "dest_topic": 2}
        sync, _ = build({f"{RELAY}:10:{RELAY}:2": 900}, Client(), same_media_only=True)
        with self.assertLogs("engine-mirror", "WARNING"):
            self.assertFalse(asyncio.run(sync.edit(route, post(10))))
        client = Client(stored={(RELAY, 900): post(900)})
        sync, _ = build({f"{RELAY}:10:{RELAY}:2": 900}, client, same_media_only=True)
        self.assertFalse(asyncio.run(sync.edit(route, post(10, text=""))))
        self.assertEqual(client.edits, [])

    def test_waits_for_a_copy_that_is_still_being_sent(self):
        mapping = {}
        now = [0.0]

        async def sleep(seconds):
            now[0] += seconds
            if now[0] >= 0.3:
                mapping[f"{RELAY}:10:{VIP}:1364"] = 900

        client = Client()
        sync = mirror.Mirror(
            "ALT", client, mapping, plain, wait_for_map=10.0, sleep=sleep, monotonic=lambda: now[0]
        )
        self.assertTrue(asyncio.run(sync.edit(RELAY2, post(10))))
        self.assertEqual(client.edits, [(VIP, 900, "TP1 hit")])


class DeleteTests(unittest.TestCase):
    def test_deleted_source_deletes_every_copy_and_its_map_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMap(Path(tmp) / "m.sqlite3")
            try:
                store[f"{RELAY}:10:{VIP}:1364"] = 900
                store[f"{RELAY}:10:{VIP}:11"] = [901, 902]
                store[f"{RELAY}:100:{VIP}:1364"] = 990
                saves = []
                client = Client()
                sync = mirror.Mirror("MAIN", client, store, plain, save=lambda: saves.append(1))
                self.assertTrue(asyncio.run(sync.delete(RELAY, 10)))
                self.assertEqual(
                    [(chat, sorted(ids)) for chat, ids in client.deleted], [(VIP, [900, 901, 902])]
                )
                self.assertEqual(
                    list(store), [f"{RELAY}:100:{VIP}:1364"], "only that post's entries are removed"
                )
                self.assertEqual(saves, [1])
            finally:
                store.close()

    def test_deletion_follows_copies_of_copies_like_edits(self):
        mapping = {f"{RELAY}:10:{VIP}:1364": 900, f"{VIP}:900:{MM}:28": 5000, f"{MM}:5000:{MM}:99": 7000}
        client = Client()
        sync, _ = build(mapping, client, cascade_depth=1)
        self.assertTrue(asyncio.run(sync.delete(RELAY, 10)))
        self.assertEqual(client.deleted, [(VIP, [900]), (MM, [5000])], "one level deep, as configured")
        self.assertEqual(list(mapping), [f"{MM}:5000:{MM}:99"])

    def test_failed_deletion_keeps_the_map_and_nothing_mapped_is_a_no_op(self):
        mapping = {f"{RELAY}:10:{VIP}:1364": 900, "bad-key": 1}
        sync, _ = build(mapping, Client(fail=RuntimeError("no rights")))
        with self.assertLogs("engine-mirror", "WARNING"):
            self.assertFalse(asyncio.run(sync.delete(RELAY, 10)))
        self.assertIn(f"{RELAY}:10:{VIP}:1364", mapping)
        self.assertFalse(asyncio.run(sync.delete(RELAY, 55)))

    def test_delete_handler_needs_a_chat_and_survives_errors(self):
        class Exploding(dict):
            def get(self, key, default=None):
                raise RuntimeError("map unreadable")

        sync, _ = build(Exploding({f"{RELAY}:10:{VIP}:1364": 900}), Client())
        with self.assertLogs("engine-mirror", "WARNING") as logs:
            asyncio.run(sync.on_delete(None, [10]))
            asyncio.run(sync.on_delete(RELAY, [10]))
        self.assertIn("missing chat_id", logs.output[0])
        self.assertIn("map unreadable", logs.output[1])

    def test_mapped_ids_reads_ints_and_album_lists(self):
        self.assertEqual(mirror.mapped_ids(900), [900])
        self.assertEqual(mirror.mapped_ids([901, "902", None]), [901, 902])
        self.assertEqual(mirror.mapped_ids(None), [])


class ReconcilerTests(unittest.TestCase):
    def test_fingerprint_is_stable_across_fetches_and_changes_with_the_text(self):
        """repr() of Telethon entities holds a memory address: every fetch looked edited."""

        def fetch():
            return post(8129, "TP1 ✅", entities=[MessageEntityBold(0, 3)])

        self.assertEqual(mirror.edit_fingerprint(fetch()), mirror.edit_fingerprint(fetch()))
        self.assertNotEqual(mirror.edit_fingerprint(fetch()), mirror.edit_fingerprint(post(8129, "TP2 ✅")))
        self.assertIsNone(mirror.edit_fingerprint(post(1, edit_date=None)))
        self.assertIsNotNone(mirror.edit_fingerprint(post(1, edit_date="2026-11-04")))

    def reconciler(self, tmp, posts, copies, mapping, blocked=lambda chat, m: False, render=shouting):
        client = Client(stored=copies)
        sync, slept = build(mapping, client, render=render, blocked=blocked)

        async def fetch(limit):
            return posts

        return mirror.EditReconciler(sync, RELAY2, fetch, Path(tmp) / "state.json"), client, slept

    def test_only_copies_that_differ_are_edited_and_each_version_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts = [
                post(1, "tp1"),
                post(2, "tp2"),
                post(3, "never edited", edit_date=None),
                post(4, "album"),
            ]
            copies = {(VIP, 901): post(901, "old"), (VIP, 902): post(902, "TP2")}
            mapping = {
                f"{RELAY}:1:{VIP}:1364": 901,
                f"{RELAY}:2:{VIP}:1364": 902,
                f"{RELAY}:4:{VIP}:1364": [1, 2],
            }
            reconciler, client, _ = self.reconciler(tmp, posts, copies, mapping)
            with self.assertLogs("engine-mirror", "WARNING"):
                self.assertEqual(asyncio.run(reconciler.once()), 1)
            self.assertEqual(client.edits, [(VIP, 901, "TP1")])
            self.assertEqual(asyncio.run(reconciler.once()), 0, "the same versions are not checked again")
            again, _, _ = self.reconciler(tmp, posts, copies, mapping)
            self.assertEqual(set(again.state), {"1", "2"}, "remembered across restarts")

    def test_missing_copies_and_filtered_posts_are_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts = [post(1, "tp1"), post(2, "join t.me/x")]
            mapping = {f"{RELAY}:1:{VIP}:1364": 901, f"{RELAY}:2:{VIP}:1364": 902}
            reconciler, client, _ = self.reconciler(
                tmp, posts, {}, mapping, blocked=lambda c, m: "t.me" in m.message
            )
            with self.assertLogs("engine-mirror", "WARNING"):
                asyncio.run(reconciler.once())
            self.assertEqual((set(reconciler.state), client.edits), ({"1", "2"}, []))

    def test_failures_are_retried_and_flood_waits_back_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            posts = [post(1, "tp1")]
            copies = {(VIP, 901): post(901, "old")}
            mapping = {f"{RELAY}:1:{VIP}:1364": 901}
            reconciler, client, slept = self.reconciler(tmp, posts, copies, mapping)
            client.fail = RuntimeError("MESSAGE_ID_INVALID")
            with self.assertLogs("engine-mirror", "WARNING"):
                self.assertEqual(asyncio.run(reconciler.once()), 0)
            self.assertEqual(reconciler.state, {}, "not remembered, so it is tried again")

            async def broken(route, message):
                raise ValueError("render failed")

            reconciler.mirror.render = broken
            with self.assertLogs("engine-mirror", "WARNING"):
                asyncio.run(reconciler.once())
            client.fail = Flood("wait")
            reconciler.mirror.render = shouting
            with self.assertRaises(Flood):
                asyncio.run(reconciler.once())

    def test_run_loop_waits_out_flood_and_survives_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            reconciler, _, slept = self.reconciler(tmp, [], {}, {})
            calls = []

            async def once():
                calls.append(1)
                if len(calls) == 1:
                    raise Flood("wait")
                if len(calls) == 2:
                    raise RuntimeError("boom")
                raise asyncio.CancelledError

            reconciler.once = once
            with self.assertLogs("engine-mirror", "WARNING"), self.assertRaises(asyncio.CancelledError):
                asyncio.run(reconciler.run())
            self.assertEqual(slept, [8.0, 30.0, 30.0])

    def test_unreadable_state_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "state.json").write_text("[1, 2]", encoding="utf-8")
            reconciler, _, _ = self.reconciler(tmp, [], {}, {})
            self.assertEqual(reconciler.state, {})


if __name__ == "__main__":
    unittest.main()

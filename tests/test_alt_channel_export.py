"""One-time channel export (6 Nov 2026): reads everything once, read-only, and the log chunks rebuild it."""

import base64
import gzip
import hashlib
import json
import logging
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon.errors import FloodWaitError

ROOT = Path(__file__).resolve().parents[1]
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import alt_channel_export as export

WHEN = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)


def post(mid, text="", **extra):
    return SimpleNamespace(
        id=mid, date=WHEN, edit_date=None, message=text, reply_to=None, media=None, **extra
    )


class Client:
    def __init__(self, posts, flood_after=None):
        self.posts, self.flood_after, self.calls = posts, flood_after, []

    def is_connected(self):
        return True

    async def iter_messages(self, chat, reverse=False, min_id=0, wait_time=None):
        self.calls.append((chat, reverse, min_id))
        for message in sorted(self.posts, key=lambda m: m.id):
            if message.id <= min_id:
                continue
            if self.flood_after is not None and message.id > self.flood_after:
                self.flood_after = None
                raise FloodWaitError(request=None, capture=5)
            yield message

    async def send_message(self, *args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("the export never sends")


class ExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = logging.getLogger("export-test")
        self.slept = []

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def test_everything_is_read_once_and_the_chunks_rebuild_it(self):
        posts = [post(i, "شراء الذهب 4180" * 40) for i in range(1, 60)]
        posts.append(post(60, "", grouped_id=7))
        client = Client(posts, flood_after=30)
        with self.assertLogs(self.log, "WARNING") as logs:
            rows = await export.export_once(client, self.tmp.name, self.log, self.sleep)
        self.assertEqual([r["id"] for r in rows], list(range(1, 61)))
        self.assertEqual(client.calls[1][2], 30, "resumed after the flood wait, not from the start")
        self.assertIn(6, self.slept)
        start = next(line for line in logs.output if "[CHANNEL EXPORT START]" in line)
        chunks = [re.search(r"\[CHANNEL EXPORT CHUNK\] \d+/\d+ (\S+)", line) for line in logs.output]
        blob = "".join(m.group(1) for m in chunks if m)
        self.assertIn(hashlib.sha256(blob.encode()).hexdigest(), start)
        rebuilt = json.loads(gzip.decompress(base64.b64decode(blob)))
        self.assertEqual(rebuilt, json.loads(json.dumps(rows)))
        self.assertEqual(rebuilt[-1]["group"], 7)
        self.assertEqual(rebuilt[0]["date"], int(WHEN.timestamp()))
        self.assertTrue((Path(self.tmp.name) / export.EXPORT_FILE).exists())
        with self.assertLogs(self.log, "WARNING") as logs:
            self.assertIsNone(await export.export_once(client, self.tmp.name, self.log, self.sleep))
        self.assertIn("[CHANNEL EXPORT SKIPPED]", logs.output[0])

    async def test_a_failure_is_logged_and_never_raised(self):
        class Broken(Client):
            async def iter_messages(self, *args, **kwargs):
                raise RuntimeError("channel unavailable")
                yield  # pragma: no cover

        self.addCleanup(setattr, export, "SETTLE_SECONDS", export.SETTLE_SECONDS)
        export.SETTLE_SECONDS = 0
        with self.assertLogs(self.log, "WARNING") as logs:
            await export.export_guarded(Broken([]), self.tmp.name, self.log)
        self.assertIn("[CHANNEL EXPORT FAILED] RuntimeError: channel unavailable", logs.output[0])


if __name__ == "__main__":
    unittest.main()

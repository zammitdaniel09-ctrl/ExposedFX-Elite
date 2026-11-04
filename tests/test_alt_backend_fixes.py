"""ALT backend fixes 4 Nov 2026: incremental watchdog fetch and the dialogs-ready gate."""

import ast
import asyncio
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
TREE = ast.parse((ROOT / "telegram_worker/alt_worker_legacy.py").read_text(encoding="utf-8"))


def load_functions(*names, **extra):
    ns = {"asyncio": asyncio, "log": logging.getLogger("alt-test"), **extra}
    nodes = [
        n for n in TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names
    ]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<alt>", "exec"), ns)
    return ns


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get_messages(self, chat, **kwargs):
        self.calls.append((chat, kwargs))
        return []


class AltBackendFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_watchdog_fetch_asks_only_for_newer_messages(self):
        client = FakeClient()
        ns = load_functions("_owner_original_fetch_route_messages", client=client)
        topic = dict(source_chat=-100111, source_topic=7194)
        whole = dict(source_chat=-100222, source_topic=None)
        await ns["_owner_original_fetch_route_messages"](topic, 500, min_id=7946)
        await ns["_owner_original_fetch_route_messages"](whole, 500, min_id=12)
        await ns["_owner_original_fetch_route_messages"](whole, 20)
        self.assertEqual(
            client.calls,
            [
                (-100111, {"limit": 500, "reply_to": 7194, "min_id": 7946}),
                (-100222, {"limit": 500, "min_id": 12}),
                (-100222, {"limit": 20, "min_id": 0}),
            ],
        )

    def test_watchdog_passes_its_checkpoint(self):
        source = " ".join((ROOT / "telegram_worker/alt_worker_legacy.py").read_text(encoding="utf-8").split())
        self.assertIn("fetch_route_messages(route, WATCHDOG_LIMIT, min_id=last_id)", source)

    async def test_send_single_preserves_replies_and_survives_a_deleted_parent(self):
        """send_single is the reply-hardened version (moved from alt_reply_hardening)."""
        from telegram_worker import owner_forwarding_policy as policy
        from telegram_worker.alt_reply_hardening import _reply_target_invalid, _reupload_cannot_help

        calls = []

        class Client:
            async def send_message(self, chat, text, **kwargs):
                calls.append(kwargs["reply_to"])
                if kwargs["reply_to"] == 555:
                    raise RuntimeError("REPLY_MESSAGE_ID_INVALID")
                return SimpleNamespace(id=900)

        async def translated_payload(route, message):
            return message.message, []

        async def resolve_reply_target(worker, route, message, logger=None):
            return 555

        ns = load_functions(
            "send_single",
            owner_policy=policy,
            alt_vip5_block=lambda route: False,
            client=Client(),
            has_username_mention=lambda m: False,
            translated_payload=translated_payload,
            real_media=lambda m: False,
            resolve_reply_target=resolve_reply_target,
            _reply_target_invalid=_reply_target_invalid,
            _reupload_cannot_help=_reupload_cannot_help,
            _WORKER=None,
        )
        route = dict(name="ALT RELAY 1", source_chat=-1003364661276, dest_chat=-1004367822325, dest_topic=2)
        sent = await ns["send_single"](route, SimpleNamespace(id=5, message="TP1 hit"))
        self.assertEqual(sent.id, 900)
        self.assertEqual(calls, [555, 2], "mapped parent first, topic root after the parent was deleted")

    async def test_copies_wait_for_dialogs_then_proceed(self):
        ready = asyncio.Event()
        ns = load_functions("wait_dialogs_ready", DIALOGS_READY=ready, DIALOGS_READY_WAIT_SECONDS=5)
        waiter = asyncio.create_task(ns["wait_dialogs_ready"]())
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        ready.set()
        await asyncio.wait_for(waiter, 1)

    async def test_dialog_wait_is_bounded(self):
        ns = load_functions(
            "wait_dialogs_ready", DIALOGS_READY=asyncio.Event(), DIALOGS_READY_WAIT_SECONDS=0.1
        )
        await asyncio.wait_for(ns["wait_dialogs_ready"](), 1)


if __name__ == "__main__":
    unittest.main()

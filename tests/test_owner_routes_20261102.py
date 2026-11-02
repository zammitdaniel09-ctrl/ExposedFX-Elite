"""Owner route regression tests: order, exact topics, restart resume and stop policy."""
import asyncio
import importlib.util
import json
import logging
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Load pure modules without executing a production worker or loading sessions.
package = sys.modules.setdefault("telegram_worker", types.ModuleType("telegram_worker"))
package.__path__ = [str(ROOT / "telegram_worker")]
from telegram_worker import owner_forwarding_policy as policy
from telegram_worker import owner_requested_routes_20261102 as owner
from telethon.errors import ChannelPrivateError, FloodWaitError

MessageActionTopicCreate = type("MessageActionTopicCreate", (), {})


def message(mid, topic, grouped=None):
    return SimpleNamespace(id=mid, message="synthetic message", media=None,
                           grouped_id=grouped, date=datetime.now(timezone.utc)-timedelta(seconds=10),
                           reply_to=SimpleNamespace(reply_to_top_id=topic, reply_to_msg_id=topic))


class FakeClient:
    def __init__(self):
        self.source = {}
        self.destination = {}
        self.calls = []
        self.fail_first = False
        self.root_action = MessageActionTopicCreate()
        for index, contract in enumerate(owner.TARGETS):
            chat, topic, _, _, count = contract
            self.source[chat, topic] = [message((index+1)*100000+i, topic) for i in range(1,count+11)]

    async def get_messages(self, chat, ids=None, reply_to=None, **kwargs):
        self.calls.append((chat, ids, reply_to))
        if ids is not None:
            if isinstance(ids, int):
                if self.fail_first and chat == owner.TARGETS[0][0]:
                    raise ChannelPrivateError(request=None)
                return SimpleNamespace(id=ids, action=self.root_action)
            source = {m.id:m for (c,t),rows in self.source.items() if c==chat for m in rows}
            return [self.destination.get((chat,i)) or source.get(i) for i in ids]
        return list(reversed(self.source.get((chat,reply_to),[])))

    async def iter_messages(self, chat, reply_to=None, min_id=0, reverse=False, limit=50):
        rows = [m for m in self.source.get((chat,reply_to),[]) if m.id > min_id]
        for m in sorted(rows,key=lambda m:m.id)[:limit]:
            yield m


class Harness:
    def __init__(self,path):
        self.DATA_DIR = path
        self.ROUTES = [dict(zip(("source_chat","source_topic","dest_chat","dest_topic"),c[:4]),name=str(i),owner_managed=True)
                       for i,c in enumerate(owner.TARGETS)]
        self.GLOBAL_REPLY_HARDENING = True
        self.client = FakeClient()
        self.maps = {}
        self.sent = []
        self.stop_after = None

    def text_of(self,m):
        return m.message
    def is_real_media(self,m):
        return bool(m.media)
    def existing_destination_ids(self,m,route):
        return self.maps.get((policy.identity(route),m.id),[])

    async def copy_one(self,m,route,**kwargs):
        if self.stop_after is not None and len(self.sent) == self.stop_after:
            raise ConnectionError("interrupted")
        mid=900000+len(self.sent)
        self.maps[policy.identity(route),m.id]=[mid]
        self.client.destination[route["dest_chat"],mid]=message(mid,route["dest_topic"])
        self.sent.append((route["dest_topic"],m.id))
        return SimpleNamespace(id=mid)

    async def copy_album(self,unit,route):
        for m in unit:
            await self.copy_one(m,route)


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        policy.configure(self.path)
        self.worker=Harness(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    async def bootstrap(self,index,entry,state):
        route=self.worker.ROUTES[index]
        await owner.bootstrap_route(self.worker,route,owner.TARGETS[index][4],
                                    entry,state,self.path/owner.STATE_FILE)

    async def test_exact_50_20_20_in_owner_order_and_no_second_run_duplicates(self):
        state={}
        with patch.object(owner.asyncio,"sleep",return_value=None):
            for index,route in enumerate(self.worker.ROUTES):
                entry=state.setdefault(policy.identity(route),{})
                await self.bootstrap(index,entry,state)
        self.assertEqual([t for t,_ in self.worker.sent], [12161]*50+[28]*20+[8]*20)
        self.assertEqual([e["status"] for e in state.values()],["done"]*3)
        for e,n in zip(state.values(),(50,20,20)):
            self.assertEqual(e["sent"],n)
            self.assertEqual(e["verified_destination_messages"],n)
        before=list(self.worker.sent)
        reloaded=owner.load_state(self.path/owner.STATE_FILE)
        with patch.object(owner.asyncio,"sleep",return_value=None):
            for index,route in enumerate(self.worker.ROUTES):
                await self.bootstrap(index,reloaded[policy.identity(route)],reloaded)
        self.assertEqual(self.worker.sent,before)

    async def test_resume_interruption_uses_frozen_selection_and_mappings(self):
        route=self.worker.ROUTES[0]
        state={policy.identity(route):{}}
        entry=state[policy.identity(route)]
        self.worker.stop_after=7
        with patch.object(owner.asyncio,"sleep",return_value=None):
            with self.assertRaises(ConnectionError):
                await self.bootstrap(0,entry,state)
        selection=entry["selected_units"]
        self.assertEqual(len(self.worker.sent),7)
        self.worker.client.source[route["source_chat"],route["source_topic"]].append(message(199999,7250))
        self.worker.stop_after=None
        with patch.object(owner.asyncio,"sleep",return_value=None):
            await self.bootstrap(0,entry,state)
        self.assertEqual(entry["selected_units"],selection)
        self.assertEqual(len(self.worker.sent),50)
        self.assertEqual(len(set(self.worker.sent)),50)

    async def test_wrong_source_topic_never_copied(self):
        route=self.worker.ROUTES[0]
        self.worker.client.source[route["source_chat"],route["source_topic"]].append(message(199999,7248))
        entry={}
        state={policy.identity(route):entry}
        with patch.object(owner.asyncio,"sleep",return_value=None):
            await self.bootstrap(0,entry,state)
        self.assertNotIn((12161,199999),self.worker.sent)

    async def test_missing_topic_root_fails_without_send(self):
        self.worker.client.root_action=None
        with self.assertRaisesRegex(RuntimeError,"Owner topic unavailable"):
            await self.bootstrap(0,{}, {})
        self.assertEqual(self.worker.sent,[])

    async def test_live_checkpoint_copies_oldest_unseen_without_history_replay(self):
        route=self.worker.ROUTES[0]
        row=self.worker.client.source[route["source_chat"],route["source_topic"]]
        entry={"last_source_id":row[-4].id}
        state={policy.identity(route):entry}
        await owner.live_cycle(self.worker,route,entry,state,self.path/owner.STATE_FILE)
        self.assertEqual([m for _,m in self.worker.sent],[m.id for m in row[-3:]])
        await owner.live_cycle(self.worker,route,entry,state,self.path/owner.STATE_FILE)
        self.assertEqual(len(self.worker.sent),3)

    async def test_stop_survives_reload_and_does_not_touch_message_map(self):
        route=self.worker.ROUTES[0]
        mapfile=self.path/"message_map.json"
        mapfile.write_text('{"old":123}')
        policy.stop_route(route,"PaidMessagesRequired")
        self.assertTrue(policy.is_stopped(route))
        policy.configure(self.path/"other")
        policy.configure(self.path)
        self.assertTrue(policy.is_stopped(route))
        self.assertEqual(mapfile.read_text(),'{"old":123}')

    async def test_live_album_page_boundary_is_not_split(self):
        route=self.worker.ROUTES[0]
        rows=[message(100000+i,7250,77 if i>=49 else None) for i in range(1,55)]
        self.worker.client.source[route["source_chat"],7250]=rows
        entry={"last_source_id":100000}
        state={policy.identity(route):entry}
        await owner.live_cycle(self.worker,route,entry,state,self.path/owner.STATE_FILE)
        self.assertEqual(len(self.worker.sent),54)
        self.assertEqual(entry["last_source_id"],100054)

    async def test_stopped_first_route_allows_other_jobs_without_sending_there(self):
        self.worker.client.fail_first=True
        original=owner.live_cycle
        reached=[]
        async def end_live(worker,route,entry,state,path):
            reached.append(route["dest_topic"])
            raise asyncio.CancelledError
        with patch.object(owner.asyncio,"sleep",return_value=None), patch.object(owner,"live_cycle",end_live):
            with self.assertRaises(asyncio.CancelledError):
                await owner.run_owner_requested_routes(self.worker)
        self.assertEqual([t for t,_ in self.worker.sent],[28]*20+[8]*20)
        self.assertTrue(policy.is_stopped(self.worker.ROUTES[0]))
        self.assertEqual(reached,[28])


class PolicyTests(unittest.TestCase):
    def test_paid_error_stops_but_floodwait_and_network_do_not(self):
        error=Exception("RPCError 403: ALLOW_PAYMENT_REQUIRED_10")
        self.assertEqual(policy.permanent_access_error(error),"PaidMessagesRequired")
        for exc in (FloodWaitError(request=None,capture=30),ConnectionError("network down")):
            self.assertIsNone(policy.permanent_access_error(exc))

    def test_7250_only_new_destination_and_retirements_retained(self):
        base={"source_chat":-1004367822325,"source_topic":7250,"dest_chat":-1003726286301}
        self.assertTrue(policy.relay7250_allowed(dict(base,dest_topic=12161)))
        self.assertFalse(policy.relay7250_allowed(dict(base,dest_topic=10877)))
        self.assertTrue(policy.known_stopped(dict(base,source_chat=-1003252087470,dest_topic=12161)))
        self.assertFalse(policy.known_stopped(dict(base,dest_topic=12161)))


if __name__=="__main__":
    unittest.main()


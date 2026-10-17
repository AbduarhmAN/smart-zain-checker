import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from decimal import Decimal

from services.codec import dumps,loads
from services.events import EventStore
from services.ipc import CommandStore,RpcClient,RpcServer,RequestConflict,RemoteError
from services.locking import ServiceLock


class IpcTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(); self.root=Path(self.directory.name)
        (self.root/'.commerce').mkdir()
        import sqlite3
        sqlite3.connect(self.root/'.commerce'/'outbox.sqlite').close()
    def tearDown(self): self.directory.cleanup()

    def test_money_and_paths_roundtrip_without_rounding(self):
        source={'money':Decimal('0.00001'),'path':self.root/'input.xlsx'}
        self.assertEqual(loads(dumps(source)),source)

    def test_command_replay_conflict_and_uncertain_restart(self):
        store=CommandStore(self.root/'commands.sqlite')
        store.accept('one','mutation',{'amount':Decimal('0.00001')})
        self.assertEqual(store.claim()[0],'one')
        store.recover(())
        self.assertEqual(store.get('one')['state'],'uncertain')
        self.assertIsNone(store.claim())
        with self.assertRaises(RequestConflict): store.accept('one','mutation',{'amount':Decimal('0.1')})
        store.accept('read','read',{})
        store.claim();store.recover(('read',))
        self.assertEqual(store.claim()[0],'read')

    def test_events_unacknowledged_survive_process_reconstruction(self):
        events=EventStore(self.root)
        events.publish('session_completed',{'commerce_records':[{'status':'cancelled','row':2}],'total':1})
        replay=EventStore(self.root).list({'consumer':'telegram'})
        self.assertEqual(len(replay),1)
        events.ack({'consumer':'telegram','event_id':replay[0]['id']})
        events.ack({'consumer':'telegram','event_id':replay[0]['id']})
        self.assertEqual(EventStore(self.root).list({'consumer':'telegram'}),[])

    def test_kernel_lock_excludes_second_owner_and_recovers(self):
        first=ServiceLock(self.root/'role.lock').acquire()
        with self.assertRaises(RuntimeError): ServiceLock(self.root/'role.lock').acquire()
        first.close(); second=ServiceLock(self.root/'role.lock').acquire();second.close()

    def test_authenticated_rpc_lost_reply_reuses_result_and_health_is_independent(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); selected=probe.getsockname()[1]
        calls=[]; gate=threading.Event()
        def mutate(payload): calls.append(payload);gate.wait(3);return {'money':Decimal('0.00001')}
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'test-only-token-'+'x'*32,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{'mutate':mutate,'snapshot':lambda _:{}},workers=1,read_only={'snapshot'})
            server.start(); client=RpcClient('engine',self.root)
            try:
                # A disconnect after durable acceptance must not create a second action.
                from services.ipc import ServiceUnavailable
                with self.assertRaises(ServiceUnavailable): client.call('mutate',{'n':1},request_id='same',timeout=.1)
                self.assertEqual(client.health()['service'],'engine')
                self.assertEqual(client.call('snapshot',{},timeout=.3),{})
                gate.set()
                self.assertEqual(client.call('mutate',{'n':1},request_id='same',timeout=2),{'money':Decimal('0.00001')})
                self.assertEqual(len(calls),1)
                with self.assertRaises(RequestConflict): client.call('mutate',{'n':2},request_id='same')
                with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'incorrect-'+'z'*32}):
                    with self.assertRaises(ServiceUnavailable):client.health()
                with self.assertRaises(ServiceUnavailable):RpcClient('engine',self.root/'other').call('snapshot',{})
            finally: gate.set();server.stop()


if __name__=='__main__': unittest.main()

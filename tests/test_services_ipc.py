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
from services.locking import ServiceLock,TokenLease


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

    def test_token_consumer_exclusion_is_independent_of_checkout_directory(self):
        import hashlib
        fingerprint=hashlib.sha256(str(self.root).encode()).hexdigest()
        first=TokenLease(fingerprint).acquire()
        try:
            with self.assertRaises(RuntimeError):TokenLease(fingerprint).acquire()
        finally:first.close()
        TokenLease(fingerprint).acquire().close()

    def test_storage_failure_after_effect_retries_ack_without_repeating_handler(self):
        import sqlite3
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));selected=probe.getsockname()[1]
        calls=[]
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'fixture-'+'z'*40,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{'effect':lambda payload:calls.append(payload) or {'saved':True}},workers=1)
            original=server.store.finish;attempts=[]
            def persist(*args,**kwargs):
                attempts.append(1)
                if len(attempts)==1:raise sqlite3.OperationalError('fixture storage outage')
                return original(*args,**kwargs)
            with patch.object(server.store,'finish',side_effect=persist):
                server.start()
                try:
                    result=RpcClient('engine',self.root).call('effect',{'n':1},request_id='one-effect',timeout=3)
                    self.assertEqual(result,{'saved':True});self.assertEqual(calls,[{'n':1}]);self.assertEqual(len(attempts),2)
                finally:server.stop()

    def test_failed_component_start_can_close_unstarted_listener(self):
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));selected=probe.getsockname()[1]
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'fixture-'+'z'*40,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{})
            server.stop()
            with socket.socket() as probe:probe.bind(('127.0.0.1',selected))

    def test_capacity_rejection_precedes_body_read_and_controls_keep_reserved_lane(self):
        import hashlib,http.client
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));selected=probe.getsockname()[1]
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'fixture-'+'z'*40,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{'snapshot':lambda _: {},'query_cancel':lambda _: {'stopped':True}},read_only={'snapshot'})
            server.start()
            held=[];connection=None
            try:
                for _ in range(32):
                    self.assertTrue(server.admission.acquire(False));held.append(1)
                # Send headers only: rejection must not wait for an absent body.
                connection=http.client.HTTPConnection('127.0.0.1',selected,timeout=1)
                connection.putrequest('POST','/rpc')
                connection.putheader('Authorization',server.auth)
                connection.putheader('X-Zain-Root',hashlib.sha256(str(server.root).encode()).hexdigest())
                connection.putheader('Content-Length','4096');connection.endheaders()
                response=connection.getresponse();self.assertEqual(response.status,503);response.read()
                self.assertTrue(RpcClient('engine',self.root).call('query_cancel',{},timeout=2)['stopped'])
                self.assertEqual(RpcClient('engine',self.root).health()['service'],'engine')
            finally:
                if connection:connection.close()
                for _ in held:server.admission.release()
                server.stop()

    def test_control_header_cannot_grant_bulk_operation_reserved_capacity(self):
        import hashlib,http.client
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));selected=probe.getsockname()[1]
        calls=[]
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'fixture-'+'z'*40,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{'bulk':lambda p:calls.append(p)})
            server.start();connection=http.client.HTTPConnection('127.0.0.1',selected,timeout=2)
            try:
                connection.request('POST','/rpc',dumps({'id':'forged','operation':'bulk','payload':{}}),
                    {'Authorization':server.auth,'X-Zain-Root':hashlib.sha256(str(server.root).encode()).hexdigest(),'X-Zain-Control':'1'})
                response=connection.getresponse();self.assertEqual(response.status,400);response.read()
                self.assertIsNone(server.store.get('forged'));self.assertEqual(calls,[])
            finally:connection.close();server.stop()

    def test_connection_saturation_rejects_before_creating_another_handler_thread(self):
        import http.client
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));selected=probe.getsockname()[1]
        with patch.dict(os.environ,{'ZAIN_IPC_TOKEN':'fixture-'+'z'*40,'ZAIN_ENGINE_PORT':str(selected)}):
            server=RpcServer('engine',self.root,{})
            server.start();held=[];connection=http.client.HTTPConnection('127.0.0.1',selected,timeout=2)
            try:
                for _ in range(64):
                    self.assertTrue(server.http.connections.acquire(False));held.append(1)
                connection.request('GET','/health',headers={'Authorization':server.auth})
                response=connection.getresponse();self.assertEqual(response.status,503);response.read()
            finally:
                connection.close()
                for _ in held:server.http.connections.release()
                self.assertTrue(RpcClient('engine',self.root).health()['service']=='engine')
                server.stop()

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
                with self.assertRaises(RemoteError):RpcClient('engine',self.root/'other').call('snapshot',{})
                for _ in range(3):
                    with self.assertRaises(RemoteError):client.call('not-registered',{})
                self.assertEqual(client.call('snapshot',{},timeout=.3),{})
            finally: gate.set();server.stop()


if __name__=='__main__': unittest.main()

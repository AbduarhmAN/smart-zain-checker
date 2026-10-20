"""Explicit isolated-process transport fixture; never used by production.

Keeps the real bot, dispatch/inbox, authorization, menus and service adapters.
Only external Telegram/payment transports are replaced.
"""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
import threading
import time


class UnavailableCommerceFixture:
    def rpc(self, name, arguments=None):
        from telegram_bot.commerce.client import CommerceUnavailable
        raise CommerceUnavailable('Isolated fixture has no commerce backend')


class TestTelegramTransport:
    def __init__(self, root):
        root = Path(root).resolve()
        actual = Path(__file__).resolve().parents[1]
        marker = root/'.services'/'TEST_FIXTURE'
        if (os.environ.get('ZAIN_SERVICE_TESTING') != '1' or root == actual
                or not marker.is_file() or marker.read_text(encoding='utf-8').strip() != 'smart-zain-isolated-tests'):
            raise ValueError('Isolated marked fixture root required')
        self.calls = []
        self.lock = threading.Lock()
        self.fingerprint=hashlib.sha256(str(root).encode()).hexdigest()

    def integration_options(self):
        return {'bot_token': 'isolated-telegram-fixture-'+self.fingerprint, 'chat_id': 9, 'authorized_id': 9,
                'commerce_client': UnavailableCommerceFixture()}

    def install(self, integration):
        runner = integration.runner
        def api(method, data=None, timeout=25):
            if method == 'getUpdates':
                runner._stop_event.wait(.05)
                return {'ok': True, 'result': []}
            with self.lock:self.calls.append({'method': method, 'data': data or {},'at':time.perf_counter()})
            return {'ok': True, 'result': {'message_id': 1, 'id': 9, 'username': 'isolated_fixture'}}
        runner._api_call = api
        # Legacy watcher configurations are module globals in the old runtime.
        # A fixture must never inspect/use their real provider credentials.
        runner.payment_watcher._check_binance_pay = lambda *_: None
        runner.payment_watcher.seen_file = integration.runner.project_root/'seen_transactions.json'
        integration.notifier.check_connection = lambda: {'connected': True, 'username': 'isolated_fixture'}
        integration.notifier._request_delivery = lambda *_args, **_kwargs: False
        if integration.archive_dispatcher is not None:
            integration.archive_dispatcher.transport = None

    def handle(self, runner, payload):
        if set(payload) != {'text'} or payload['text'] not in {'/start', '/help', '/status', '/queue'}:
            raise ValueError('Fixture supports only read-only command probes')
        with self.lock:before = len(self.calls)
        started = time.perf_counter()
        runner._handle_update({'message': {'from': {'id': 9}, 'chat': {'id': 9, 'type': 'private'},
                                          'text': payload['text']}})
        with self.lock:return {'responses': self.calls[before:], 'elapsed_ms': (time.perf_counter()-started)*1000}

    def batch(self,runner,payload):
        if set(payload)!={'count'} or type(payload['count']) is not int or not 1<=payload['count']<=32:
            raise ValueError('Bounded fixture batch required')
        users=list(range(9,9+payload['count']))
        runner.authorized_ids.update(users)  # Explicit fixture overrides only.
        with self.lock:before=len(self.calls)
        received=time.perf_counter();identifier=time.time_ns()//1000000
        updates=[{'update_id':identifier+index,'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},'text':'/start'}} for index,actor in enumerate(users)]
        runner._update_inbox.put(updates)
        deadline=time.monotonic()+15;latencies={}
        while time.monotonic()<deadline and len(latencies)<len(users):
            with self.lock:
                for call in self.calls[before:]:
                    actor=call['data'].get('chat_id')
                    if call['method'] in {'sendMessage','editMessageText'} and actor in users:
                        latencies.setdefault(actor,(call['at']-received)*1000)
            time.sleep(.005)
        if len(latencies)!=len(users):raise RuntimeError('Fixture batch did not receive all acknowledgements')
        ordered=sorted(latencies.values())
        return {'users':len(users),'responses':len(latencies),'p50_ms':ordered[int((len(ordered)-1)*.5)],
            'p95_ms':ordered[int((len(ordered)-1)*.95)],'max_ms':max(ordered),
            'measurement':'durable inbox intake through real dispatcher to fixture Telegram send'}

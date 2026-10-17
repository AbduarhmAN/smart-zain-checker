"""Independent Telegram/commerce runtime. Engine control crosses authenticated IPC."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import threading

from services.engine_client import RemoteEngine, RemoteQueue
from services.ipc import ServiceUnavailable, RemoteError

logger = logging.getLogger('TelegramService')


class TelegramApplication:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.engine = RemoteEngine(self.root)
        self.queue = RemoteQueue(self.engine.client, self.engine)
        self.integration = None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_event_error = None
        self.test_transport = None

    def start(self):
        from telegram_bot.integration import TelegramIntegration
        self.engine.start()
        options = {}
        if os.environ.get('ZAIN_SERVICE_TESTING') == '1':
            from services.testing_telegram import TestTelegramTransport
            self.test_transport = TestTelegramTransport(self.root)
            options = self.test_transport.integration_options()
        self.integration = TelegramIntegration(self.engine, self.queue, self.root, **options)
        if self.test_transport:self.test_transport.install(self.integration)
        self.integration.set_enabled(True)
        self.thread = threading.Thread(target=self._events, name='telegram-engine-events', daemon=True)
        self.thread.start()

    def _accept_event(self, event):
        if not isinstance(event, dict) or set(event) != {'id', 'event', 'payload'}:
            raise ValueError('Invalid engine event')
        name, payload = event['event'], event['payload']
        known = {'session_started', 'session_completed', 'network_alert', 'session_failed', 'session_paused',
                 'session_resumed', 'session_cancelled', 'service_route_verification'}
        if name not in known:
            raise ValueError('Unsupported engine event')
        if name in {'session_started', 'session_completed', 'network_alert'} and not isinstance(payload, dict):
            raise ValueError('Invalid notification payload')
        if name == 'session_completed' and payload.get('commerce_order_id'):
            # Financial/result evidence is durable before acknowledging the
            # engine event. Existing delivery claims prevent duplicate sends.
            self.integration.commerce_bridge.capture_completion(payload)
        notifier = self.integration.notifier
        callback = dict(notifier._subscriptions()).get(name)
        if callback is not None:callback(name, payload)
        self.engine.client.call('event.ack', {'consumer': 'telegram', 'event_id': event['id']},
                                request_id='telegram-event-ack:' + str(event['id']), timeout=3)

    def _events(self):
        while not self.stop_event.is_set():
            try:
                events = self.engine.client.call('event.list', {'consumer': 'telegram', 'limit': 32}, timeout=3)
                for event in events:
                    if self.stop_event.is_set():break
                    self._accept_event(event)
                if events and hasattr(self.engine, 'wake'):
                    self.engine.wake()
                self.last_event_error = None
            except (ServiceUnavailable, RemoteError, OSError, ValueError, KeyError, TypeError) as exc:
                self.last_event_error = type(exc).__name__
            self.stop_event.wait(1.0)

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=4)
        if self.integration:self.integration.stop()
        self.engine.stop()

    def health(self):
        return {'ready': bool(self.integration and self.integration.runner.status()['polling']),
                'engine_online': self.engine.get_live_status().get('engine_online', False),
                'events_error': self.last_event_error,
                'bot': self.integration.runner.status() if self.integration else {'polling': False}}

    def handlers(self):
        def status(payload):
            if payload:raise ValueError('Empty status request required')
            return self.health()
        def enabled(payload):
            if set(payload) != {'enabled'} or type(payload['enabled']) is not bool:raise ValueError('Boolean state required')
            self.integration.set_enabled(payload['enabled'])
            return self.integration.status()
        def connection(payload):
            if payload:raise ValueError('Empty request required')
            return self.integration.check_connection()
        def message(payload):
            if set(payload) - {'text', 'wait'} or not isinstance(payload.get('text'), str):raise ValueError('Message required')
            if type(payload.get('wait', False)) is not bool:raise ValueError('Boolean wait required')
            return self.integration.notifier.send_message(payload['text'], wait=payload.get('wait', False))
        result = {'telegram.status': status, 'telegram.set_enabled': enabled,
                  'telegram.check_connection': connection, 'telegram.send_message': message}
        if os.environ.get('ZAIN_SERVICE_TESTING') == '1':
            def test_update(payload):
                if self.test_transport is None:raise ValueError('Fixture transport inactive')
                return self.test_transport.handle(self.integration.runner, payload)
            result['telegram.test_update'] = test_update
        return result


def build(root):return TelegramApplication(root)

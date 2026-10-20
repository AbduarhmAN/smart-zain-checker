"""Offline service-boundary tests: durable intake, authority and failure recovery."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from services.engine_client import RemoteEngine, RemoteQueue
from services.ipc import ServiceUnavailable
from services.locking import ServiceLock
from services.telegram import TelegramApplication
from telegram_bot.update_inbox import UpdateInbox


def update(identifier, actor):
    return {'update_id': identifier, 'message': {'from': {'id': actor}, 'chat': {'id': actor}, 'text': '/start'}}


class TelegramBoundaryTests(unittest.TestCase):
    def setUp(self):self.root = Path(tempfile.mkdtemp(prefix='telegram_boundary_retained_'))

    def inbox(self, capacity=2048):return UpdateInbox(self.root/'inbox.sqlite', 'fixture', capacity=capacity)

    def test_update_is_durable_before_dispatch_and_duplicate_batch_has_one_copy(self):
        inbox = self.inbox()
        inbox.put([update(1, 10), update(2, 20)])
        inbox.put([update(1, 10)])
        recovered = self.inbox()
        self.assertEqual(recovered.counts(), {'pending': 2})
        self.assertEqual(recovered.claim()['update_id'], 1)

    def test_actor_ordering_does_not_block_other_customers(self):
        inbox = self.inbox()
        inbox.put([update(1, 10), update(2, 10), update(3, 20)])
        self.assertEqual(inbox.claim()['update_id'], 1)
        self.assertEqual(inbox.claim()['update_id'], 3)
        self.assertIsNone(inbox.claim())
        inbox.finish(1, True)
        self.assertEqual(inbox.claim()['update_id'], 2)

    def test_process_crash_replays_running_but_does_not_replay_completed_or_review(self):
        inbox = self.inbox()
        inbox.put([update(1, 10), update(2, 20), update(3, 30)])
        inbox.claim(); inbox.finish(1, True)
        inbox.claim(); inbox.finish(2, False)
        inbox.claim()
        recovered = self.inbox(); recovered.recover()
        self.assertEqual(recovered.counts(), {'done': 1, 'review': 1, 'pending': 1})
        self.assertEqual(recovered.claim()['update_id'], 3)

    def test_backpressure_rejects_whole_batch_without_partial_acceptance(self):
        inbox = self.inbox(capacity=1)
        with self.assertRaises(BufferError):inbox.put([update(1, 10), update(2, 20)])
        self.assertEqual(inbox.counts(), {})

    def test_bot_token_lock_excludes_second_consumer_and_is_reusable_after_close(self):
        first, second = ServiceLock(self.root/'consumer.lock'), ServiceLock(self.root/'consumer.lock')
        first.acquire()
        try:
            with self.assertRaises(RuntimeError):second.acquire()
        finally:first.close()
        second.acquire(); second.close()

    def test_engine_outage_is_reported_without_inventing_current_running_state(self):
        client = Mock(); client.call.side_effect = ServiceUnavailable('fixture unavailable')
        engine = RemoteEngine(self.root, client=client)
        state = engine.get_live_status()
        self.assertFalse(state['engine_online']); self.assertTrue(state['stale'])
        self.assertNotIn('running', state)
        client.call.assert_not_called()

    def test_stop_has_reserved_lane_and_does_not_compete_with_ordinary_queue(self):
        inbox = self.inbox()
        normal = update(1, 10); normal['message']['text'] = 'campaign input'
        stop = {'update_id': 2, 'callback_query': {'from': {'id': 20}, 'data': 'zc:oh:stopconfirm:fixture'}}
        inbox.put([normal, stop])
        self.assertEqual(inbox.claim(priority=True)['update_id'], 2)
        self.assertEqual(inbox.claim(priority=False)['update_id'], 1)

    def test_priority_does_not_reorder_financial_inputs_of_same_actor(self):
        inbox = self.inbox()
        normal = update(1, 10); normal['message']['text'] = 'cash receipt input'
        inbox.put([normal, update(2, 10)])
        self.assertIsNone(inbox.claim(priority=True))
        self.assertEqual(inbox.claim(priority=False)['update_id'], 1)
        self.assertIsNone(inbox.claim(priority=True))
        inbox.finish(1, True)
        self.assertEqual(inbox.claim(priority=True)['update_id'], 2)

    def test_batched_claim_uses_distinct_actors_and_preserves_each_actor_order(self):
        inbox=self.inbox()
        inbox.put([update(1,21),update(2,21),update(3,22),update(4,23)])
        claimed=inbox.claim_batch(limit=16)
        self.assertEqual([item['update_id'] for item in claimed],[1,3,4])
        self.assertIsNone(inbox.claim())
        inbox.finish(1,True)
        self.assertEqual(inbox.claim_batch(limit=2)[0]['update_id'],2)

    def test_saturated_normal_handlers_preserve_control_capacity_without_unbounded_executor_backlog(self):
        from telegram_bot.bot import TelegramBotRunner
        bot = TelegramBotRunner(Mock(), Mock(), self.root, bot_token='offline-bounded-fixture', authorized_id=9)
        release, control = threading.Event(), threading.Event()
        active, peak = [0], [0]
        lock = threading.Lock()
        def handle(item):
            if item['message']['text'] == '/help':control.set(); return True
            with lock:
                active[0] += 1; peak[0] = max(peak[0], active[0])
            release.wait(3)
            with lock:active[0] -= 1
            return True
        bot._safe_handle_update = lambda item, _:handle(item)
        normal = [update(i, 100+i) for i in range(1, 40)]
        for item in normal:item['message']['text'] = 'slow backend input'
        bot._update_inbox.put(normal)
        dispatcher = threading.Thread(target=bot._dispatch_inbox); dispatcher.start()
        try:
            deadline = time.monotonic()+2
            while peak[0] < bot._handler_workers-2 and time.monotonic() < deadline:time.sleep(.01)
            self.assertEqual(peak[0], bot._handler_workers-2)
            fast = update(100, 900); fast['message']['text'] = '/help'
            bot._update_inbox.put([fast])
            self.assertTrue(control.wait(1), 'Reserved control dispatch was starved by backend handlers')
            self.assertLessEqual(bot._executor._work_queue.qsize(), bot._handler_workers-2)
        finally:
            bot._stop_event.set(); release.set(); dispatcher.join(2)
            bot._executor.shutdown(wait=True); bot._priority_executor.shutdown(wait=True)
            bot._session.close()

    def test_records_use_separate_operation_and_guarded_actions_preserve_identity(self):
        client = Mock(); client.call.return_value = {1: {'row': 2}}
        engine = RemoteEngine(self.root, client=client)
        self.assertEqual(engine.all_completed_records, {1: {'row': 2}})
        self.assertEqual(client.call.call_args.args[0], 'engine.records')
        engine.session_action('pause', 'reviewed-session', 'fixture.xlsx')
        self.assertEqual(client.call.call_args.args, ('engine.session_action', {
            'action': 'pause', 'expected_session': 'reviewed-session', 'expected_workbook': 'fixture.xlsx'}))

    def test_owned_stop_uses_stable_receipt_key(self):
        client = Mock(); queue = RemoteQueue(client)
        queue.request_customer_stop('job-fixture', 30, 'native-callback-fixture')
        self.assertEqual(client.call.call_args.kwargs['request_id'], 'customer-stop:native-callback-fixture')

    def test_financial_completion_must_be_captured_before_engine_event_ack(self):
        app = TelegramApplication(self.root)
        app.engine.client = Mock()
        app.integration = Mock()
        app.integration.commerce_bridge.capture_completion.side_effect = OSError('fixture unavailable')
        event = {'id': 7, 'event': 'session_completed', 'payload': {'commerce_order_id': 'fixture'}}
        with self.assertRaises(OSError):app._accept_event(event)
        app.engine.client.call.assert_not_called()
        app.integration.notifier._subscriptions.assert_not_called()

    def test_completion_ack_follows_durable_capture_and_notifier(self):
        app = TelegramApplication(self.root)
        calls = []
        app.engine.client = Mock()
        app.engine.client.call.side_effect = lambda *a, **k:calls.append('ack')
        app.integration = Mock()
        app.integration.commerce_bridge.capture_completion.side_effect = lambda _:calls.append('capture')
        app.integration.notifier._subscriptions.return_value = [('session_completed', lambda *a:calls.append('notify'))]
        app._accept_event({'id': 7, 'event': 'session_completed', 'payload': {'commerce_order_id': 'fixture'}})
        self.assertEqual(calls, ['capture', 'notify', 'ack'])

    def test_crashed_running_wallet_update_replays_original_receipt_after_lost_reserve_ack(self):
        from test_commerce_checkout import CheckoutTests, TOKEN
        fixture = CheckoutTests('test_lost_reserve_ack_then_retry_enqueues_once_without_second_spend')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.prepare(); fixture.lose_reserve = True
        inbox = self.inbox()
        item = {'update_id': 15, 'callback_query': {'id': 'stable-native-receipt', 'from': {'id': 31},
                'message': {'chat': {'id': 31}, 'message_id': 4}, 'data': f'zc:co:wallet:{TOKEN}'}}
        inbox.put([item]); claimed = inbox.claim()
        fixture.bot._handle_callback(claimed['callback_query'])
        self.assertEqual(fixture.bot.queue_service.get_jobs(), [])
        # The money hold committed, its acknowledgement was lost, and intake
        # remained running when the consumer crashed.
        recovered = self.inbox(); recovered.recover()
        replay = recovered.claim()
        self.assertEqual(replay, item)
        fixture.bot._handle_callback(replay['callback_query'])
        recovered.finish(15, True)
        self.assertEqual(len(fixture.bot.queue_service.get_jobs()), 1)
        names = [call.args[0] for call in fixture.client.rpc.call_args_list]
        self.assertEqual(names.count('reserve_order'), 1)
        self.assertIn('stable-native-receipt', fixture.state['refund_consent_reference'])

    def test_payout_update_replay_keeps_business_key_and_expired_review_cannot_write(self):
        from test_admin_partner_payouts import AdminPartnerPayoutTests
        from telegram_bot.commerce.client import CommerceUnavailable
        fixture = AdminPartnerPayoutTests('test_confirmation_records_stable_key_and_queues_delivery_without_direct_send')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        token = fixture.issue()
        committed, attempts = {}, []
        original = fixture.rpc
        def rpc(name, arguments):
            if name != 'record_partner_payout':return original(name, arguments)
            attempts.append(dict(arguments))
            key = arguments['p_key']
            if key not in committed:
                committed[key] = original(name, arguments)
                raise CommerceUnavailable('Fixture lost payout acknowledgment')
            return committed[key]
        fixture.client.rpc.side_effect = rpc
        inbox = self.inbox()
        item = {'update_id': 17, 'callback_query': {'id': 'stable-payout-click', 'from': {'id': 9},
                'data': 'zc:wa:payconfirm:' + token}}
        inbox.put([item]); inbox.claim()
        fixture.callback('wa:payconfirm:' + token, callback=item['callback_query'])
        inbox.recover(); replay = inbox.claim()
        fixture.callback('wa:payconfirm:' + token, callback=replay['callback_query'])
        self.assertEqual(len(committed), 1)
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(len(fixture.writes), 1)
        # After restart the transient review is absent. Replaying its native
        # update must require fresh review rather than invent a new payout.
        fixture.bot._admin_partner_payout_reviews = {}
        before = len(attempts)
        fixture.callback('wa:payconfirm:' + token, callback=replay['callback_query'])
        self.assertEqual(len(attempts), before)


if __name__ == '__main__':unittest.main()

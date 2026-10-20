"""Real durable support + native bot routing; all network is synthetic."""
import concurrent.futures
import hashlib
import json
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock,patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.commerce.support import SupportService,reply_text,support_record
from telegram_bot import views


class SupportTests(unittest.TestCase):
    def setUp(self):
        # Historical delivery/storage regressions remain covered independently
        # of the currently selected channel-only UI policy.
        legacy=patch('telegram_bot.commerce.support_ui.CHANNEL_ONLY',False)
        legacy.start();self.addCleanup(legacy.stop)
        for target in ('telegram_bot.bot.support_route','telegram_bot.ui.support_route'):
            routing=patch(target,return_value=False)
            routing.start();self.addCleanup(routing.stop)
        self.root=Path(tempfile.mkdtemp(prefix='support-retained-'));self.order=str(uuid4())
        self.records={};self.rpc_calls=[];self.client=Mock();self.client.rpc.side_effect=self.rpc
        self.service=SupportService(self.root,client=self.client,bot_fingerprint='synthetic-fingerprint')
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot._subscription_status=Mock(return_value='member')
        self.bot.project_root=self.root;self.bot._bot_fingerprint='synthetic-fingerprint';self.bot._ui_lock=threading.RLock()
        self.bot._waiting_user_inputs={};self.bot._confirmations={};self.bot.authorized_ids={9};self.bot.authorized_id=9
        self.bot._authorized_override=True;self.bot._commerce_client=self.client
        self.bot._support_service=self.service;self.service.start=Mock()
        self.bot._show_panel=Mock();self.bot._reply=Mock();self.bot._api_call=Mock(side_effect=self.api)
        self.bot._update_inbox=queue.Queue()
        self.bot._start_payout_notifications=Mock()
        self.bot._start_partner_lifecycle_notifications=Mock()
        self.bot._start_partner_sheet_notifications=Mock()
        self.bot._sync_partner_menus=Mock()
        self.sent=[];self.next_message=500

    def rpc(self,name,args):
        self.rpc_calls.append((name,args))
        if name=='support_order':
            if args['p_role']=='customer' and args['p_actor_telegram_id']!=123:raise ValueError('unowned')
            if args['p_role']=='admin' and args['p_actor_telegram_id']!=9:raise ValueError('unauthorized')
            return {'order_id':args['p_order_id'],'owner_telegram_id':123,'thread_state':'open'}
        if name=='record_support_message':
            self.records[args['p_event_key']]=args
            return {'state':'stored','event_key':args['p_event_key']}
        if name=='confirm_support_delivery':return {'state':'confirmed'}
        if name=='support_history':return {'order_id':args['p_order_id'],'messages':[],'thread_state':'open'}
        if name=='support_inbox':return []
        if name=='set_support_state':return {'state':args['p_state']}
        raise AssertionError(name)

    def api(self,method,data):
        if method=='getMe':return {'ok':True,'result':{'id':77,'is_bot':True}}
        if method=='sendMessage':
            self.sent.append(data);self.next_message+=1
            return {'ok':True,'result':{'chat':{'id':data['chat_id']},'from':{'id':77,'is_bot':True},
                'message_id':self.next_message,'date':1700001000,'text':data['text']}}
        return {'ok':True,'result':{}}

    def message(self,actor=123,body='راجع نتيجة السطر',message_id=101,**extra):
        return {'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},
            'message_id':message_id,'date':1700000000,'text':body,**extra},'update_id':message_id}

    def drain(self,service=None):
        service=service or self.service
        for _ in range(20):
            if not service.worker.run_once():return
        raise AssertionError('Support outbox loop did not drain')

    def capture(self,actor=123,body='راجع نتيجة السطر',message_id=101):
        self.service.begin(actor,self.order,'admin' if actor==9 else 'customer',77)
        return self.service.capture(self.message(actor,body,message_id),{9})

    def test_normal_bot_messages_are_not_archived(self):
        self.assertIsNone(self.service.capture(self.message(),{9}));self.assertEqual(self.service.store.status(),{})

    def test_owned_message_durable_before_any_network_and_restart(self):
        self.service.begin(123,self.order,'customer',77);self.client.reset_mock()
        key=self.service.capture(self.message(),{9});self.client.rpc.assert_not_called()
        restarted=SupportService(self.root,client=self.client,bot_fingerprint='synthetic-fingerprint')
        self.assertEqual(restarted.store.event(key)['p_record']['body'],'راجع نتيجة السطر')
        self.drain(restarted);self.assertIn(key,self.records)

    def test_context_survives_restart_and_is_used_for_one_message(self):
        self.service.begin(123,self.order,'customer',77)
        restored=SupportService(self.root,client=self.client,bot_fingerprint='synthetic-fingerprint')
        self.assertIsNotNone(restored.capture(self.message(),{9}))
        self.assertIsNone(restored.capture(self.message(message_id=102),{9}))

    def test_edit_and_replay_preserve_original_and_dependency(self):
        key=self.capture();self.assertEqual(self.service.capture(self.message(),{9}),key)
        edited=self.message(body='تصحيح مهم')['message'];edited['edit_date']=1700000010
        edit=self.service.capture({'edited_message':edited},{9})
        self.assertNotEqual(edit,key);self.assertEqual(self.service.store.event(key)['p_record']['body'],'راجع نتيجة السطر')
        self.drain();self.assertEqual(len(self.records),2)
        self.assertEqual(self.records[edit]['p_record']['kind'],'customer_edit')

    def test_foreign_chat_bot_sender_and_unknown_edit_are_not_captured(self):
        self.service.begin(123,self.order,'customer',77)
        for update in [self.message(actor=456),self.message()]:
            if update['message']['from']['id']==123:update['message']['chat']['id']=-10001;update['message']['chat']['type']='group'
            self.assertIsNone(self.service.capture(update,{9}))
        edit=self.message()['message'];edit['edit_date']=1700000010
        self.assertIsNone(self.service.capture({'edited_message':edit},{9}))
        bot=self.message()['message'];bot['from']['is_bot']=True
        self.assertIsNone(self.service.capture({'message':bot},{9}))

    def test_revoked_staff_context_cannot_capture_reply(self):
        self.service.begin(9,self.order,'admin',77)
        self.assertIsNone(self.service.capture(self.message(actor=9),set()))

    def test_namespace_isolated_between_bots(self):
        key=self.capture()
        other=SupportService(self.root,client=self.client,bot_fingerprint='other-bot')
        edit=self.message()['message'];edit['edit_date']=1700000010
        self.assertIsNone(other.capture({'edited_message':edit},{9}))
        self.assertEqual(self.service.store.event(key)['p_record']['source']['bot_id'],77)

    def test_attachment_references_captured_without_download_or_unrelated_metadata(self):
        self.service.begin(123,self.order,'customer',77)
        update=self.message(body='');update['message'].pop('text')
        update['message']['caption']='دليل الخطأ';update['message']['document']={'file_id':'file','file_unique_id':'u',
            'file_name':'evidence.xlsx','file_size':19,'mime_type':'application/xlsx','unexpected_secret':'must-not-record'}
        key=self.service.capture(update,{9});record=self.service.store.event(key)['p_record']
        self.assertEqual(record['body'],'دليل الخطأ');self.assertEqual(record['attachments'][0]['file_id'],'file')
        self.assertNotIn('unexpected_secret',json.dumps(record));self.bot._api_call.assert_not_called()

    def test_staff_text_limit_counts_utf16_units(self):
        with self.assertRaises(ValueError):support_record(self.message(9,'🙂'*1600)['message'],77,'admin')

    def test_staff_proposal_does_not_send_until_explicit_click(self):
        self.service.begin(9,self.order,'admin',77);update=self.message(9,'سنراجع هذا السطر')
        self.bot._capture_support_arrival(update);self.bot._handle_update(update)
        self.assertEqual(self.sent,[]);self.drain()
        key=update['_support_event'];self.bot._support_send(key,9)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.sent[0]['chat_id'],123)
        self.assertEqual(self.service.store.delivery_state(key),'confirmed')
        self.bot._support_send(key,9);self.assertEqual(len(self.sent),1)
        self.drain();self.assertEqual(sum(n=='confirm_support_delivery' for n,_ in self.rpc_calls),1)

    def test_pending_server_capture_prevents_send(self):
        key=self.capture(9,'رد للمراجعة');self.bot._support_send(key,9)
        self.assertEqual(self.sent,[]);self.assertIsNone(self.service.store.delivery_state(key))

    def test_staff_source_edit_cancels_old_unsent_proposal(self):
        key=self.capture(9,'النص القديم');self.drain()
        edited=self.message(9,'النص الجديد')['message'];edited['edit_date']=1700000010
        self.service.capture({'edited_message':edited},{9});self.drain()
        self.bot._support_send(key,9)
        self.assertEqual(self.sent,[]);self.assertEqual(self.service.store.delivery_state(key),'superseded')
        self.assertEqual(self.service.store.event(key)['p_record']['body'],'النص القديم')

    def test_ambiguous_send_is_not_repeated(self):
        key=self.capture(9,'رد');self.drain()
        self.bot._api_call.side_effect=lambda method,data:self.api(method,data) if method!='sendMessage' else None
        self.bot._support_send(key,9);self.assertEqual(self.service.store.delivery_state(key),'ambiguous')
        count=self.bot._api_call.call_count;self.bot._support_send(key,9)
        self.assertEqual(self.bot._api_call.call_count,count+1) # getMe is read-only; no second send.

    def test_known_rejection_can_be_retried_after_repair(self):
        key=self.capture(9,'رد');self.drain()
        self.bot._api_call.side_effect=lambda method,data:self.api(method,data) if method!='sendMessage' else {'ok':False,'error_code':403}
        self.bot._support_send(key,9);self.assertEqual(self.service.store.delivery_state(key),'rejected')
        self.bot._api_call.side_effect=self.api;self.bot._support_send(key,9)
        self.assertEqual(len(self.sent),1)

    def test_confirmed_receipt_survives_enqueue_failure_without_another_send(self):
        key=self.capture(9,'رد');self.drain()
        with patch.object(self.service,'_enqueue_receipt',side_effect=sqlite3.OperationalError('synthetic-disk-failure')):
            with self.assertRaises(sqlite3.Error):self.bot._support_send(key,9)
        restarted=SupportService(self.root,client=self.client,bot_fingerprint='synthetic-fingerprint')
        restarted.recover();self.drain(restarted)
        self.assertEqual(len(self.sent),1);self.assertEqual(restarted.store.delivery_state(key),'confirmed')

    def test_receipt_capture_has_no_network_dependency(self):
        key=self.capture(9,'رد');self.drain();self.service.prepare_delivery(key,9,123)
        self.service.store.claim_delivery(key);self.client.reset_mock()
        self.service.capture_delivery(key,9,{'chat':{'id':123},'from':{'id':77,'is_bot':True},
            'message_id':600,'date':1700000100,'text':reply_text(self.order,'رد')})
        self.client.rpc.assert_not_called()

    def test_wrong_delivery_recipient_or_text_is_rejected(self):
        key=self.capture(9,'رد');self.service.prepare_delivery(key,9,123)
        with self.assertRaises(ValueError):self.service.capture_delivery(key,9,{'chat':{'id':456},'message_id':8})

    def test_many_claims_allow_only_one_staff_send(self):
        key=self.capture(9,'رد');self.drain()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            claims=list(executor.map(lambda _:self.service.store.claim_delivery(key),range(16)))
        self.assertEqual(sum(value is not None for value in claims),1)

    def test_forged_customer_admin_and_group_callbacks_never_send(self):
        key=self.capture(9,'رد');self.drain()
        for actor,chat in [(123,123),(9,-10001)]:
            self.bot._handle_callback({'id':'synthetic','from':{'id':actor},'message':{'chat':{'id':chat},'message_id':8},'data':'zc:sp:send:'+key})
        self.assertEqual(self.sent,[])

    def test_archive_customer_buttons_pass_public_gate_to_ownership_checks(self):
        self.bot._archive_callback=Mock();self.bot._archive_prompt=Mock()
        for action in ('ar:result:'+self.order,'archive'):
            self.bot._handle_callback({'id':'synthetic','from':{'id':123},'message':{'chat':{'id':123},'message_id':8},'data':'zc:'+action})
        self.bot._archive_callback.assert_called_once_with('ar:result:'+self.order,123,8)
        self.bot._archive_prompt.assert_called_once_with(123,8)

    def test_customer_buttons_filter_staff_sends_and_inbox(self):
        rows=[[views.button('سجل',f'sp:history:{self.order}:0'),views.button('كتابة',f'sp:compose:{self.order}')],
            [views.button('إرسال',f'sp:send:{uuid4()}'),views.button('الطلبات','support_inbox')]]
        kept=views.customer_rows(rows)
        self.assertEqual(len(kept),1);self.assertEqual(len(kept[0]),2)
        self.assertTrue(all(len(item['callback_data'].encode())<=64 for item in kept[0]))

    def test_arrival_storage_failure_prevents_cursor_advancement(self):
        self.bot._sync_partner_menus=Mock()
        self.bot._start_partner_sheet_notifications=Mock()
        self.service.begin(123,self.order,'customer',77)
        self.bot._stop_event=threading.Event();self.bot._last_update_id=0;self.bot._cursor_saved_at=time.time()
        self.bot._checkpoint_update=Mock();self.bot._executor=Mock();self.bot.last_poll_error=None
        update=self.message()
        def api(method,data,**kwargs):return {'ok':True,'result':[update]} if method=='getUpdates' else {'ok':True}
        self.bot._api_call=Mock(side_effect=api)
        with patch.object(self.service.store,'enqueue',side_effect=OSError('synthetic-storage-failure')),\
             patch.object(self.bot._stop_event,'wait',side_effect=lambda delay:self.bot._stop_event.set()):
            self.bot._poll_loop()
        self.bot._checkpoint_update.assert_not_called();self.bot._executor.submit.assert_not_called()
        self.assertEqual(self.bot._last_update_id,0)

    def test_arrival_capture_precedes_poll_cursor_and_has_no_rpc(self):
        self.bot._sync_partner_menus=Mock()
        self.bot._start_partner_sheet_notifications=Mock()
        self.service.begin(123,self.order,'customer',77);self.client.reset_mock()
        self.bot._stop_event=threading.Event();self.bot._last_update_id=0;self.bot._cursor_saved_at=time.time();self.bot._executor=Mock()
        update=self.message()
        def checkpoint(value):
            self.assertEqual(self.service.store.status(),{'pending':1});self.client.rpc.assert_not_called()
            self.bot._last_update_id=value;self.bot._stop_event.set()
        self.bot._checkpoint_update=checkpoint
        self.bot._api_call=Mock(side_effect=lambda method,data,**kwargs:{'ok':True,'result':[update]} if method=='getUpdates' else {'ok':True})
        self.bot._poll_loop();self.assertEqual(self.bot._last_update_id,101)


if __name__=='__main__':unittest.main()

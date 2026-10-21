import copy
import time
import unittest
from unittest.mock import Mock, patch

import test_commerce_checkout as fixture
from telegram_bot import views
from telegram_bot.commerce.client import CommerceUnavailable
from telegram_bot.commerce.vodafone_cash import RECEIVER, RECEIVER_NAME, phone_number, telegram_proof
from telegram_bot.ui import TelegramUiMixin


class VodafoneTests(unittest.TestCase):
    def setUp(self):
        # Preserve historical complaint-record integrity coverage; the default
        # channel-only entry points are verified in test_official_support.py.
        legacy=patch('telegram_bot.commerce.vodafone_cash.CHANNEL_ONLY',False)
        legacy.start();self.addCleanup(legacy.stop)
        routing=patch('telegram_bot.ui.support_route',return_value=False)
        routing.start();self.addCleanup(routing.stop)
        self.fixture=fixture.CheckoutTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.bot=self.fixture.bot;self.client=self.fixture.client
        self.records={};self.balance=0;self.lose_approval=False
        self.client.rpc.side_effect=self.rpc
        self.bot._api_call.return_value={'ok':True,'result':{'message_id':800}}

    def rpc(self,name,args):
        if name=='create_vodafone_request':
            key=args['p_request_id']
            self.records.setdefault(key,{'id':key,'amount_minor':args['p_amount_minor'],'receiver':RECEIVER,
                'receiver_name':RECEIVER_NAME,'telegram_user_id':args['p_telegram_user_id'],'sender_phone':None,
                'proof':None,'state':'awaiting_evidence','messages':[]})
            if self.records[key]['amount_minor']!=args['p_amount_minor']:raise ValueError('Conflicting replay')
            return copy.deepcopy(self.records[key])
        if name=='list_vodafone_requests':return list(copy.deepcopy(self.records).values())
        if name in {'vodafone_request','submit_vodafone_request','review_vodafone_request','vodafone_message'}:
            record=self.records[args['p_request_id']]
            admin=args.get('p_admin') or name=='review_vodafone_request'
            actor=args.get('p_actor_telegram_id',args.get('p_telegram_user_id'))
            if (admin and actor!=9) or (not admin and actor!=record['telegram_user_id']):raise ValueError('Unowned request')
            if name=='submit_vodafone_request':record.update(state='pending',sender_phone=args['p_sender_phone'],proof=args['p_proof'])
            if name=='review_vodafone_request':
                if record['state']=='pending':
                    record['state']='approved' if args['p_action']=='approve' else 'rejected'
                    if record['state']=='approved':self.balance+=record['amount_minor']
                    else:record['rejection_reason']=args['p_value']
                if self.lose_approval:
                    self.lose_approval=False;raise CommerceUnavailable('Synthetic lost acknowledgement')
            if name=='vodafone_message':record['messages'].append({'actor_role':'admin' if args['p_admin'] else 'customer','body':args['p_body']})
            return copy.deepcopy(record)
        return self.fixture.rpc(name,args)

    def callback(self,action,value='0',actor=31,callback_id='test:callback'):
        self.bot._handle_callback({'id':callback_id,'from':{'id':actor},'message':{'chat':{'id':actor,'type':'private'},'message_id':4},
            'data':f'zc:vc:{action}:{value}'})

    def text(self,text,actor=31,message_id=50):
        waiting=self.bot._waiting_user_inputs.pop(actor)
        waiting['_telegram_input_message_id']=message_id
        waiting['_telegram_message']={'message_id':message_id,'chat':{'id':actor,'type':'private'},'from':{'id':actor},'text':text}
        self.bot._commerce_text_input(actor,text,waiting)

    def create(self):
        self.callback('start');self.text('60.50')
        return next(iter(self.records))

    def submit(self,photo=True):
        key=self.create();self.callback('phone',key);self.text('+20 100 000 0000',message_id=51)
        if photo:
            message={'message_id':52,'chat':{'id':31,'type':'private'},'from':{'id':31},
                'photo':[{'file_id':'photo-fixture','file_unique_id':'unique-fixture','file_size':3000}]}
            self.assertTrue(self.bot._vodafone_handle_media(message,31))
        else:self.text('تم تحويل 60.50 جنيه؛ رقم العملية 123456',message_id=52)
        return key

    def prepare_approval(self,key):
        self.callback('review',key,9);self.callback('transaction',key,9);self.text('TX123456',9,60)

    def test_exact_amount_and_given_receiver_are_saved_without_credit(self):
        key=self.create();record=self.records[key]
        self.assertEqual(record['amount_minor'],6050)
        self.assertEqual((record['receiver'],record['receiver_name']),('01050740996','عبدالرحمن'))
        self.assertEqual(self.balance,0);self.assertEqual(self.bot.queue_service.get_jobs(),[])

    def test_payment_entry_uploads_bundled_official_logo_and_caches_file_id(self):
        self.bot.api_url='https://api.telegram.org/botTEST_ONLY'
        self.bot._session=Mock()
        self.bot._show_panel=TelegramUiMixin._show_panel.__get__(self.bot)
        self.bot._session.post.return_value.json.return_value={'ok':True,'result':{'message_id':4,'photo':[{'file_id':'vodafone-logo-fixture'}]}}
        self.callback('start')
        self.assertEqual(self.bot._waiting_user_inputs[31]['action'],'commerce_vodafone_amount')
        call=self.bot._session.post.call_args
        self.assertEqual(call.args[0],self.bot.api_url+'/editMessageMedia')
        self.assertEqual(call.kwargs['data']['chat_id'],31)
        self.assertEqual(call.kwargs['files']['photo'][0],'vodafone_cash_logo.jpg')
        self.callback('start')
        self.assertEqual(self.bot._session.post.call_count,1)
        self.bot._api_call.assert_called_with('editMessageMedia',unittest.mock.ANY)
        payload=self.bot._api_call.call_args.args[1]
        self.assertEqual(payload['media']['media'],'vodafone-logo-fixture')
        self.assertFalse(payload['media']['show_caption_above_media'])
        self.assertIn('أرسل مبلغ الشحن',payload['media']['caption'])
        self.assertIn('reply_markup',payload)
        self.assertFalse(any(c.args[0]=='sendMessage' for c in self.bot._api_call.call_args_list))
        self.assertEqual(self.records,{})

    def test_failed_logo_does_not_block_amount_entry_or_create_credit(self):
        self.bot.api_url='https://api.telegram.org/botTEST_ONLY'
        self.bot._session=Mock()
        self.bot._show_panel=TelegramUiMixin._show_panel.__get__(self.bot)
        self.bot._session.post.side_effect=TimeoutError('synthetic timeout')
        self.callback('start')
        self.assertEqual(self.bot._waiting_user_inputs[31]['action'],'commerce_vodafone_amount')
        self.assertEqual(self.balance,0)
        self.assertEqual(self.records,{})
        self.assertEqual(self.bot._session.post.call_count,1)

    def test_amount_replay_uses_same_request_identity(self):
        self.create();self.callback('start');self.text('60.50')
        self.assertEqual(len(self.records),1)

    def test_invalid_amount_does_not_create_request(self):
        for value in ['0','-1','1.001','NaN','Infinity','abc']:
            self.callback('start');self.text(value)
        self.assertEqual(self.records,{})

    def test_photo_evidence_is_pending_and_not_paid(self):
        key=self.submit();r=self.records[key]
        self.assertEqual(r['state'],'pending');self.assertEqual(r['sender_phone'],'01000000000')
        self.assertEqual(r['proof']['file_id'],'photo-fixture');self.assertEqual(self.balance,0)
        self.assertTrue(any(call.args[1].get('chat_id')==9 for call in self.bot._api_call.call_args_list))

    def test_text_notification_is_accepted_as_evidence_without_automatic_acceptance(self):
        key=self.submit(photo=False)
        self.assertEqual(self.records[key]['proof']['kind'],'text');self.assertEqual(self.balance,0)

    def test_preview_transaction_and_explicit_click_credit_once(self):
        key=self.submit();self.prepare_approval(key)
        self.assertEqual(self.balance,0)
        self.callback('approve',key,9);self.callback('approve',key,9)
        self.assertEqual(self.balance,6050)
        self.assertEqual(self.bot.queue_service.get_jobs(),[])

    def test_lost_approval_ack_is_recovered_by_same_request(self):
        key=self.submit();self.prepare_approval(key);self.lose_approval=True
        self.callback('approve',key,9);self.callback('approve',key,9)
        self.assertEqual(self.balance,6050)

    def test_customer_cannot_review_or_approve_and_another_admin_cannot_reuse_review(self):
        key=self.submit();self.prepare_approval(key)
        self.client.rpc.reset_mock();self.callback('approve',key,31)
        self.assertFalse(any(c.args[0]=='review_vodafone_request' for c in self.client.rpc.call_args_list))
        self.bot.authorized_ids.add(18);self.callback('approve',key,18)
        self.assertEqual(self.balance,0)

    def test_missing_expired_or_revoked_review_cannot_credit(self):
        key=self.submit();self.callback('approve',key,9)
        self.assertEqual(self.balance,0)
        self.prepare_approval(key);self.bot._vodafone_reviews[(9,key)]['expires']=time.monotonic()-1
        self.callback('approve',key,9);self.assertEqual(self.balance,0)
        self.prepare_approval(key);self.bot.authorized_ids.clear();self.bot.authorized_id=17
        self.callback('approve',key,9);self.assertEqual(self.balance,0)

    def test_receipt_preview_failure_cannot_create_approval_review(self):
        key=self.submit();self.bot._api_call.return_value={'ok':False}
        self.callback('review',key,9)
        self.assertNotIn((9,key),getattr(self.bot,'_vodafone_reviews',{}))

    def test_rejection_reason_is_visible_and_does_not_credit(self):
        key=self.submit();self.callback('review',key,9);self.callback('reject',key,9)
        self.text('لم يصل التحويل إلى المحفظة',9,61);self.callback('decline',key,9)
        self.assertEqual(self.balance,0);self.assertEqual(self.records[key]['state'],'rejected')
        self.callback('open',key)
        self.assertIn('لم يصل التحويل',self.bot._show_panel.call_args.args[1].text)

    def test_complaint_and_reviewed_reply_are_linked_to_request(self):
        key=self.submit();self.callback('complaint',key);self.text('لماذا تأخر طلبي؟',message_id=62)
        self.callback('reply',key,9);self.text('نراجع التحويل الآن',9,63)
        self.assertEqual(len(self.records[key]['messages']),1)
        self.callback('send',key,9)
        self.assertEqual([r['actor_role'] for r in self.records[key]['messages']],['customer','admin'])
        self.assertTrue(any('نراجع التحويل الآن' in c.args[1].get('text','') for c in self.bot._api_call.call_args_list))

    def test_back_cancels_receipt_prompt_and_media_hook_accepts_real_update(self):
        key=self.create();self.callback('phone',key);self.text('01000000000',message_id=51)
        self.callback('open',key)
        self.assertNotIn(31,self.bot._waiting_user_inputs)
        self.callback('phone',key);self.text('01000000000',message_id=52)
        with patch.object(self.bot,'_refresh_partner_menu_for_chat'):
            self.bot._handle_update({'message':{'message_id':53,'chat':{'id':31,'type':'private'},'from':{'id':31},
                'photo':[{'file_id':'photo-fixture','file_unique_id':'unique-fixture'}]}})
        self.assertEqual(self.records[key]['state'],'pending')

    def test_pdf_and_private_text_update_reach_receipt_handler(self):
        key=self.create();self.callback('phone',key);self.text('01000000000',message_id=51)
        with patch.object(self.bot,'_refresh_partner_menu_for_chat'):
            self.bot._handle_update({'message':{'message_id':52,'chat':{'id':31,'type':'private'},'from':{'id':31},
                'text':'تم التحويل؛ إشعار اختبار'}})
        self.assertEqual(self.records[key]['proof']['kind'],'text')
        proof=telegram_proof({'message_id':1,'chat':{'id':31,'type':'private'},'from':{'id':31},
            'document':{'mime_type':'application/pdf','file_id':'pdf-fixture','file_unique_id':'pdf-unique'}},31)
        self.assertEqual(proof['kind'],'document')

    def test_receipt_cannot_be_changed_after_admin_preview(self):
        key=self.submit();self.prepare_approval(key)
        self.records[key]['sender_phone']='01100000000'
        self.callback('approve',key,9);self.assertEqual(self.balance,0)

    def test_other_customer_cannot_open_or_complain_about_request(self):
        key=self.submit();self.callback('complaint',key,44)
        self.assertNotIn(44,self.bot._waiting_user_inputs)

    def test_callback_and_customer_menu_preserve_public_buttons_and_hide_admin_actions(self):
        rows=views.customer_rows([[views.button('فودافون','vc:start:0')],[views.button('إدارة','vc:inbox:0')]])
        self.assertEqual([b['callback_data'] for row in rows for b in row],['zc:vc:start:0'])

    def test_phone_normalization_and_receipt_source_checks(self):
        self.assertEqual(phone_number('٠١٠٥٠٧٤٠٩٩٦'),'01050740996')
        self.assertEqual(phone_number('00201050740996'),'01050740996')
        with self.assertRaises(ValueError):phone_number('123')
        with self.assertRaises(ValueError):telegram_proof({'message_id':1,'chat':{'id':31},'from':{'id':44},'text':'receipt'},31)
        with self.assertRaises(ValueError):telegram_proof({'message_id':1,'chat':{'id':31},'from':{'id':31},
            'document':{'mime_type':'application/x-msdownload','file_id':'x','file_unique_id':'y'}},31)

    def test_crypto_visibility_does_not_hide_vodafone(self):
        with patch('telegram_bot.config.show_crypto_payments',return_value=False):
            self.bot._wallet_callback('cw:topup:0',31,4,{})
        panel=self.bot._show_panel.call_args.args[1]
        self.assertTrue(any(b.get('callback_data')=='zc:vc:start:0' for row in panel.rows for b in row))

    def test_checkout_links_vodafone_and_only_official_channel_support(self):
        self.fixture.prepare()
        actions={b.get('callback_data') for row in self.bot._show_panel.call_args.args[1].rows for b in row}
        self.assertIn('zc:vc:start:0',actions)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertTrue(any(b.get('url')=='https://t.me/zainchecker?direct' for row in panel.rows for b in row))
        self.assertNotIn('zc:sp:compose:'+fixture.ORDER,actions)


if __name__=='__main__':unittest.main()

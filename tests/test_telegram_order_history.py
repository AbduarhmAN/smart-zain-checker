"""Retained fixtures: visibility and explicit consent before a duplicate payment."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
from openpyxl import Workbook
from telegram_bot.bot import TelegramBotRunner
from telegram_bot.payment_manager import PaymentManager
from telegram_bot.sheet_workflow import file_version

OLD='abcdef012345'
NEW='012345abcdef'


class OrderHistoryTests(unittest.TestCase):
    def legacy_promo(self,amount=0):
        self.manager.add_promo_code('ONE_SHEET','free',10,100)
        self.manager.apply_promo_code('ONE_SHEET',31,claim_id=OLD)
        pending=json.loads(self.manager.pending_path.read_text(encoding='utf-8'))
        pending[OLD]['kind']='promo'
        pending[OLD]['draft_info'].update(egp_amount=amount,promo_applied=True,promo_code='ONE_SHEET')
        self.manager.pending_path.write_text(json.dumps(pending),encoding='utf-8')
        self.jobs[0]['column_mapping']['telegram_chat_id']=31
        self.bot._commerce_client=Mock()
        self.bot.orchestrator.apply_customer_stop=Mock()
        def stop(job_id,owner,reference):
            if job_id!=self.jobs[0]['id'] or owner!=31:raise ValueError('Wrong owner')
            self.jobs[0]['column_mapping']['customer_stop']={'actor':owner,'reference':reference}
            return True
        self.bot.queue_service.request_customer_stop=Mock(side_effect=stop)

    def test_free_sheet_promotion_can_stop_without_a_commerce_order_or_refund(self):
        self.legacy_promo()
        files=self.financial_bytes();promo=self.manager.config_path.read_bytes()
        self.bot._history_order(31,OLD)
        self.assertTrue(any(b['callback_data']==f'zc:oh:stop:{OLD}' for row in self.panel().rows for b in row))
        self.bot._history_callback(f'oh:stop:{OLD}',31)
        self.assertIn('مجاني',self.panel().text);self.assertIn('يبقى مستهلكًا',self.panel().text)
        self.bot.queue_service.request_customer_stop.assert_not_called()
        nonce=next(iter(self.bot._customer_stop_reviews))
        self.bot._history_callback('oh:stopconfirm:'+nonce,31,callback={'id':'native-stop'})
        self.bot.queue_service.request_customer_stop.assert_called_once()
        self.bot.orchestrator.apply_customer_stop.assert_called_once_with('job-old')
        self.bot._commerce_client.rpc.assert_not_called()
        self.assertEqual(files,self.financial_bytes());self.assertEqual(promo,self.manager.config_path.read_bytes())
        self.assertFalse(self.manager.validate_promo_code('ONE_SHEET',31)[0])

    def test_discounted_legacy_promotion_can_stop_without_fabricating_a_cash_refund(self):
        self.legacy_promo(amount=15)
        self.bot._history_callback(f'oh:stop:{OLD}',31)
        self.assertIn('تحتاج مراجعة الإدارة',self.panel().text)
        nonce=next(iter(self.bot._customer_stop_reviews))
        self.bot._history_callback('oh:stopconfirm:'+nonce,31,callback={'id':'native-discount-stop'})
        self.bot.orchestrator.apply_customer_stop.assert_called_once_with('job-old')
        self.bot._commerce_client.rpc.assert_not_called()

    def test_free_stop_revalidates_owner_and_confirmation(self):
        self.legacy_promo()
        self.bot._history_callback(f'oh:stop:{OLD}',32)
        self.assertFalse(getattr(self.bot,'_customer_stop_reviews',{}))
        self.bot._history_callback(f'oh:stop:{OLD}',31)
        nonce=next(iter(self.bot._customer_stop_reviews))
        self.bot._history_callback('oh:stopconfirm:'+nonce,31,callback={})
        self.bot.queue_service.request_customer_stop.assert_not_called()
        self.jobs[0]['column_mapping']['telegram_chat_id']=32
        self.bot._history_callback('oh:stopconfirm:'+nonce,31,callback={'id':'native-stop'})
        self.bot.queue_service.request_customer_stop.assert_not_called()

    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='order-history-retained-'))
        source=self.root/'old.xlsx';book=Workbook();book.active.append(['number','amount']);book.active.append(['1000000001',0]);book.save(source);book.close()
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot._subscription_status=Mock(return_value='member')
        self.bot.project_root=self.root;self.bot._ui_lock=threading.RLock();self.bot._sheet_drafts={}
        self.bot.authorized_id=9;self.bot.authorized_ids={9};self.bot._authorized_override=True
        self.bot._show_panel=Mock();self.bot._api_call=Mock();self.bot._waiting_user_inputs={}
        self.bot.orchestrator=SimpleNamespace(lock=threading.RLock())
        self.bot._commerce_client=Mock()
        self.bot._commerce_client.rpc.side_effect=lambda name,args: (
            {'is_partner':False} if name=='partner_sheet_context' else self.fail('Unexpected commerce RPC'))
        self.jobs=[{'id':'job-old','status':'pending','sheet_name':'Sheet','total_records':1,
                    'column_mapping':{'payment_token':f'telegram:31:{OLD}','telegram_chat_id':31}}]
        self.bot.queue_service=Mock();self.bot.queue_service.get_jobs.side_effect=lambda:copy.deepcopy(self.jobs)
        self.old={'path':source,'version':file_version(source),'chat':31,'expires':time.monotonic()+1800,
            'sheet_index':0,'sheet_name':'Sheet','sheets':['Sheet'],'columns':[],
            'mapping':{'lookup_col':'A','amount_col':'B','has_headers':True},'metadata':{},
            'mode':'smart_hybrid','amount_target':'remaining','validation':{'ok':True,'rows':1},
            'egp_amount':30,'usdt_amount':.6,'live_rate':50,'operation_id':'261014-0004'}
        self.bot._history_capture_input(self.old)
        self.manager=PaymentManager(self.root);self.manager.credit_wallet(31,100)
        self.manager.record_payment_draft(OLD,31,self.old,kind='wallet')
        self.manager.debit_wallet(31,30,idempotency_key=f'wallet:31:{OLD}')
        self.manager.complete_payment_draft(OLD,'job-old')
        replacement=self.root/'renamed.xlsx';replacement.write_bytes(source.read_bytes())
        self.new={**copy.deepcopy(self.old),'path':replacement,'version':file_version(replacement),'operation_id':'261014-0005'}
        self.new.pop('payment_input_source',None);self.bot._sheet_drafts[NEW]=self.new
        self.network=patch('urllib.request.urlopen',side_effect=AssertionError('External network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)

    def panel(self):return self.bot._show_panel.call_args.args[1]

    def financial_bytes(self):
        return {name:(self.root/name).read_bytes() for name in ('pending_payments.json','user_wallets.json')}

    def test_paid_order_survives_removed_review_and_history_is_read_only(self):
        before=self.financial_bytes();self.bot._history_list(31,0);self.bot._history_order(31,OLD)
        self.assertIn('261014-0004',self.panel().text);self.assertIn('بانتظار بدء الفحص',self.panel().text)
        self.assertIn('خصم المحفظة مؤكد: 30.0',self.panel().text);self.assertEqual(before,self.financial_bytes())

    def test_other_customer_and_forged_admin_cannot_read_order(self):
        for action in (f'oh:open:{OLD}',f'oh:inspect:{OLD}','oh:admin:0'):
            self.bot._history_callback(action,32)
            self.assertNotIn('261014-0004',self.panel().text);self.assertIn('تعذر عرض السجل',self.panel().text)

    def test_native_customer_history_button_reaches_owned_order(self):
        self.bot._handle_callback({'id':'native-history','from':{'id':31},
            'message':{'chat':{'id':31,'type':'private'},'message_id':4},'data':f'zc:oh:open:{OLD}'})
        self.assertIn('261014-0004',self.panel().text)

    def test_native_forged_admin_history_button_cannot_disclose_orders(self):
        self.bot._handle_callback({'id':'foreign-history','from':{'id':32},
            'message':{'chat':{'id':32,'type':'private'},'message_id':4},'data':'zc:oh:admin:0'})
        self.assertNotIn('261014-0004',self.panel().text);self.assertIn('تعذر عرض السجل',self.panel().text)

    def test_same_hash_renamed_upload_asks_without_charging(self):
        before=self.financial_bytes()
        with patch('telegram_bot.payment_manager.PAYMENT_MGR',self.manager):
            self.bot._handle_payment_method_callback(f'pay:wallet:{NEW}',31,4,{'id':'native'})
        self.assertIn('SHA-256',self.panel().text);self.assertIn('دفع جديد',self.panel().text)
        self.assertEqual(before,self.financial_bytes());self.bot.queue_service.add_job.assert_not_called()

    def test_same_name_different_bytes_does_not_match_previous_hash(self):
        self.new['path'].write_bytes(b'different file content');self.new['version']=file_version(self.new['path'])
        self.assertFalse(self.bot._history_existing_paid_input(NEW,self.new,31))

    def test_confirming_duplicate_requires_owner_receipt_and_unchanged_input(self):
        self.bot._history_existing_paid_input(NEW,self.new,31)
        self.bot._history_callback(f'oh:repeat:{NEW}',32,callback={'id':'foreign'})
        self.assertFalse(self.bot._duplicate_payment_reviews[(31,NEW)]['confirmed'])
        self.bot._history_callback(f'oh:repeat:{NEW}',31)
        self.assertFalse(self.bot._duplicate_payment_reviews[(31,NEW)]['confirmed'])
        self.new['path'].write_bytes(b'changed');self.new['version']=file_version(self.new['path'])
        self.bot._history_callback(f'oh:repeat:{NEW}',31,callback={'id':'native'})
        self.assertFalse(self.bot._duplicate_payment_reviews[(31,NEW)]['confirmed'])

    def test_expired_consent_cannot_allow_second_payment(self):
        self.bot._history_existing_paid_input(NEW,self.new,31)
        self.bot._duplicate_payment_reviews[(31,NEW)]['expires']=time.monotonic()-1
        self.bot._history_callback(f'oh:repeat:{NEW}',31,callback={'id':'native'})
        self.assertFalse(self.bot._duplicate_payment_reviews[(31,NEW)]['confirmed'])

    def test_explicit_duplicate_consent_returns_to_fee_review_without_debit(self):
        self.bot._history_existing_paid_input(NEW,self.new,31);before=self.financial_bytes()
        self.bot._history_callback(f'oh:repeat:{NEW}',31,callback={'id':'native'})
        self.assertFalse(self.bot._history_existing_paid_input(NEW,self.new,31))
        self.assertEqual(before,self.financial_bytes());self.assertIn('لم نخصم مبلغًا بعد',self.panel().text)

    def test_real_new_wallet_charge_only_after_explicit_duplicate_choice(self):
        self.bot._history_existing_paid_input(NEW,self.new,31)
        self.bot._history_callback(f'oh:repeat:{NEW}',31,callback={'id':'native'})
        def enqueue(**kwargs):
            self.jobs.append({'id':'job-new','status':'pending',**kwargs})
            return SimpleNamespace(id='job-new')
        self.bot.queue_service.add_job.side_effect=enqueue
        with patch('telegram_bot.payment_manager.PAYMENT_MGR',self.manager):
            self.bot._handle_payment_method_callback(f'pay:wallet:{NEW}',31,4,{'id':'pay-native'})
        self.assertEqual(self.manager.get_wallet_balance(31),40)
        self.assertEqual(self.manager.get_pending_payment(NEW)['job_id'],'job-new')
        self.assertEqual(len(self.jobs),2)

    def test_completed_job_also_requires_a_choice_before_a_new_charge(self):
        self.jobs[0]['status']='completed'
        self.assertTrue(self.bot._history_existing_paid_input(NEW,self.new,31))

    def test_ledger_shows_only_actor_movements(self):
        self.manager.credit_wallet(32,999,reason='other-customer-secret');before=self.financial_bytes()
        self.bot._history_ledger(31,0)
        self.assertIn('30.0',self.panel().text);self.assertNotIn('other-customer-secret',self.panel().text)
        self.assertEqual(before,self.financial_bytes())

    def server_order(self):
        return {'order_id':'836aeb46-e43c-49cf-809e-81171b9ac2ec','owner_telegram_id':31,
            'requested_checks':3,'quoted_minor':3,'payment_state':'reserved','execution_state':'queued',
            'delivery_state':'not_ready','refund_consent_reference':'native','pricing_snapshot':{'policy_version':2}}

    def test_authoritative_order_survives_missing_local_draft_and_payment_json(self):
        item=self.server_order();self.bot._sheet_drafts={};self.bot._commerce_client=Mock()
        self.bot._commerce_client.rpc.return_value=item
        self.bot._history_callback('oh:copen:'+item['order_id'],31)
        self.assertIn('0.03 جنيه',self.panel().text)
        self.bot._commerce_client.rpc.assert_called_once_with('customer_order_detail',
            {'p_actor_telegram_id':31,'p_order_id':item['order_id'],'p_admin':False})

    def test_authoritative_order_shows_account_and_service_counts(self):
        item=self.server_order();item.update(account_count=1,service_count=2)
        self.bot._commerce_client=Mock();self.bot._commerce_client.rpc.return_value=item
        self.bot._history_callback('oh:copen:'+item['order_id'],31)
        self.assertIn('عدد أرقام الحسابات: 1',self.panel().text)
        self.assertIn('عدد أرقام الخدمة/المحافظ: 2',self.panel().text)

    def test_stopped_queued_order_is_shown_as_cancellation_not_waiting_for_checking(self):
        item=self.server_order();item['customer_stop_reference']='native-stop'
        self.bot._commerce_client=Mock();self.bot._commerce_client.rpc.return_value=item
        self.bot._history_callback('oh:copen:'+item['order_id'],31)
        self.assertIn('قيد تنفيذ الإيقاف وتسوية الحجز',self.panel().text)
        self.assertNotIn('الفحص: بانتظار الفحص',self.panel().text)
        self.assertIn('حتى لو كانت متابعة الطلبات الجديدة معطّلة',self.panel().text)

    def test_settled_stop_shows_refund_instead_of_telling_customer_to_wait_again(self):
        item=self.server_order();item.update(customer_stop_reference='native-stop',payment_state='refunded',
            execution_state='terminal',delivery_state='confirmed',charged_minor=0,refunded_minor=3)
        self.bot._commerce_client=Mock();self.bot._commerce_client.rpc.return_value=item
        self.bot._history_callback('oh:copen:'+item['order_id'],31)
        self.assertIn('اكتملت تسوية طلب الإيقاف',self.panel().text)
        self.assertIn('0.03 جنيه',self.panel().text)
        self.assertNotIn('ستظهر التسوية',self.panel().text)

    def test_server_response_for_another_owner_is_not_displayed(self):
        item=self.server_order();self.bot._commerce_client=Mock();self.bot._commerce_client.rpc.return_value=item
        self.bot._history_callback('oh:copen:'+item['order_id'],32)
        self.assertIn('تعذر عرض السجل',self.panel().text);self.assertNotIn('0.03 جنيه',self.panel().text)

    def test_authoritative_customer_ledger_uses_actor_and_no_legacy_balance(self):
        self.bot._commerce_client=Mock();self.bot._uses_commerce_wallet=Mock(return_value=True)
        self.bot._commerce_client.rpc.return_value={'total':1,'items':[{'delta_minor':3,'kind':'refund',
            'bucket_deltas':{'available':3,'reserved':-3},'created_at':'2026-10-14T18:00:00Z','wallet_kind':'customer'}]}
        before=self.financial_bytes();self.bot._history_ledger(31,0)
        self.assertIn('+0.03 جنيه',self.panel().text);self.assertNotIn('30.0',self.panel().text)
        self.bot._commerce_client.rpc.assert_called_once_with('customer_wallet_activity',
            {'p_telegram_user_id':31,'p_offset':0,'p_limit':8})
        self.assertEqual(before,self.financial_bytes())

    def test_authoritative_admin_page_requires_current_administrator_before_rpc(self):
        self.bot._commerce_client=Mock()
        self.bot._history_callback('oh:cadmin:0',32)
        self.bot._commerce_client.rpc.assert_not_called();self.assertIn('تعذر عرض السجل',self.panel().text)

    def test_customer_render_keeps_owned_history_and_note_controls_but_hides_admin_controls(self):
        from telegram_bot import views
        actions=['oh:copen:owned','oh:clist:0','oh:cstop:owned','oh:cconsent:owned','bn:issue:owned','bn:check:owned',
                 'oh:cadmin:0','oh:cinspect:other']
        kept=views.customer_rows([[views.button(action,action)] for action in actions])
        self.assertEqual([b['callback_data'][3:] for row in kept for b in row],actions[:6])

    def test_existing_input_hash_survives_missing_original_file(self):
        self.old['path'].rename(self.root/'retained-original.xlsx')
        self.assertTrue(self.bot._history_existing_paid_input(NEW,self.new,31))

    def test_lost_enqueue_acknowledgement_also_prevents_an_unconfirmed_new_charge(self):
        pending=json.loads(self.manager.pending_path.read_text(encoding='utf-8'))
        pending[OLD]['status']='ready';pending[OLD].pop('job_id',None)
        self.manager.pending_path.write_text(json.dumps(pending),encoding='utf-8');self.jobs.clear()
        before=self.financial_bytes()
        self.assertTrue(self.bot._history_existing_paid_input(NEW,self.new,31))
        self.assertEqual(before,self.financial_bytes())

    def test_duplicate_callback_is_customer_visible_but_admin_history_is_hidden(self):
        from telegram_bot import views
        rows=views.customer_rows([[views.button('confirm',f'oh:repeat:{NEW}'),views.button('admin','oh:admin:0')]])
        self.assertEqual([b['callback_data'] for row in rows for b in row],[f'zc:oh:repeat:{NEW}'])

if __name__=='__main__':unittest.main()

"""Approval, immutable review and Telegram delivery boundaries; offline only."""
import copy
import hashlib
import os
from decimal import Decimal
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import Mock,patch
from uuid import uuid4
from telegram_bot.commerce.partner_sheets import PartnerSheetMixin
from telegram_bot.commerce.checkout import CommerceCheckoutMixin
from telegram_bot import views

ORDER='3c7a5bd6-6015-4a15-ad72-410dafee2d57'


class PartnerSheetTests(unittest.TestCase):
    def setUp(self):
        self.bot=PartnerSheetMixin()
        self.bot.current_authorized_ids={9};self.bot._ui_lock=threading.RLock()
        self.bot._waiting_user_inputs={};self.bot._sheet_drafts={}
        self.bot._commerce=Mock();self.bot._show_panel=Mock();self.bot._commerce_notice=Mock();self.bot._api_call=Mock()
        self.client=self.bot._commerce.return_value
        self.request={'order_id':ORDER,'telegram_user_id':31,'state':'pending','order_state':'draft','requested_checks':2840,
                      'source':{'original_filename':'<partner>.xlsx','sheet_index':0,'file_sha256':'a'*64}}
        self.page={'items':[self.request],'price':{'version':3,'amount_minor':1500}}

    def panel_actions(self):
        return [b['callback_data'] for row in self.bot._show_panel.call_args.args[1].rows for b in row]

    def test_partner_cannot_edit_price_or_approve(self):
        self.bot._partner_sheet_admin('ps:settings:0',31,4,{'id':'foreign'})
        self.bot._partner_sheet_admin('ps:approve:'+ORDER,31,4,{'id':'foreign'})
        self.bot._commerce.assert_not_called();self.bot._show_panel.assert_not_called()

    def test_review_displays_flat_price_for_2840_checks_and_requires_new_native_click(self):
        self.client.rpc.return_value=self.page
        self.bot._partner_sheet_admin('ps:review:'+ORDER,9,4,{})
        text=self.bot._show_panel.call_args.args[1].text
        self.assertIn('15.00 جنيه',text);self.assertIn('2840',text);self.assertIn('&lt;partner&gt;',text)
        self.bot._partner_sheet_admin('ps:approve:'+ORDER,9,4,{})
        self.assertNotIn('decide_partner_sheet',[c.args[0] for c in self.client.rpc.call_args_list])
        self.bot._partner_sheet_admin('ps:approve:'+ORDER,9,4,{'id':'native-owner'})
        decisions=[c for c in self.client.rpc.call_args_list if c.args[0]=='decide_partner_sheet']
        self.assertEqual(len(decisions),1)
        self.assertEqual(decisions[0].args[1],{'p_actor_telegram_id':9,'p_order_id':ORDER,'p_approve':True,
                         'p_price_version':3,'p_reference':'telegram-sheet-decision:native-owner'})

    def test_price_not_configured_shows_no_approval_button(self):
        self.client.rpc.return_value={**self.page,'price':None}
        self.bot._partner_sheet_admin('ps:review:'+ORDER,9,4,{})
        self.assertNotIn('zc:ps:approve:'+ORDER,self.panel_actions())

    def test_pending_and_rejected_sheets_expose_no_payment(self):
        for state in ('pending','rejected'):
            with self.subTest(state=state):
                self.assertTrue(self.bot._partner_sheet_blocked({'order_id':ORDER,'partner_sheet':{'state':state}},'abc',31,4))
                self.assertFalse(any(a.startswith(('zc:co:wallet:','zc:co:invoice:')) for a in self.panel_actions()))
        self.assertFalse(self.bot._partner_sheet_blocked({'partner_sheet':{'state':'approved'}},'abc',31,4))

    def test_backend_state_cannot_bypass_enqueue_approval(self):
        bot=CommerceCheckoutMixin()
        with self.assertRaisesRegex(ValueError,'Owner approval'):
            bot._checkout_enqueue('abc',{},31,{'partner_sheet':{'state':'pending'},'reservation_state':'held','payment_state':'reserved'})

    def test_new_price_is_reviewed_and_retry_uses_same_idempotency_reference(self):
        self.assertTrue(self.bot._commerce_text_input(9,'15.125',{'action':'partner_sheet_price','expires':time.monotonic()+600}))
        token=next(iter(self.bot._partner_price_reviews))
        self.assertEqual(self.bot._partner_price_reviews[token]['amount_minor'],Decimal('1512.500'))
        self.client.rpc.assert_not_called();self.bot._api_call.assert_not_called()
        self.client.rpc.return_value={'amount_minor':Decimal('1512.5'),'recipients':3}
        self.bot._partner_sheet_admin('ps:save:'+token,9,4,{'id':'native-one'})
        self.bot._partner_sheet_admin('ps:save:'+token,9,4,{'id':'native-retry'})
        calls=self.client.rpc.call_args_list
        self.assertEqual(calls[0],calls[1]);self.assertIn('أُدرجت إشعارات',self.bot._commerce_notice.call_args.args[2])

    def notice(self,kind='price'):
        record={'id':'notice-id','token':'claim-token','recipient_telegram_id':31,
                'payload':{'kind':kind,'version':2,'amount_minor':2300}}
        self.client.rpc.side_effect=lambda name,*args:copy.deepcopy(record) if name=='claim_partner_sheet_notification' else {'saved':True}
        return record

    def test_uncertain_telegram_send_is_saved_without_second_send(self):
        self.notice();self.bot._api_call.return_value=None
        self.bot._partner_sheet_delivery_once()
        self.bot._api_call.assert_called_once()
        self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],'uncertain')

    def test_only_native_telegram_message_receipt_counts_as_delivered(self):
        for response,expected in (({'ok':True,'result':{'message_id':77,'chat':{'id':31}}},'sent'),
                                  ({'ok':True,'result':{'message_id':77,'chat':{'id':32}}},'uncertain'),
                                  ({'ok':True},'uncertain'),({'ok':False,'error_code':403},'rejected')):
            with self.subTest(expected=expected):
                self.notice();self.bot._api_call.return_value=response
                self.bot._partner_sheet_delivery_once()
                self.assertEqual(self.client.rpc.call_args.args[1]['p_state'],expected)
                payload=self.bot._api_call.call_args.args[1]
                self.assertIn('23.00 جنيه للملف كاملاً',payload['text']);self.assertEqual(payload['chat_id'],31)

    def test_partner_navigation_kept_and_admin_controls_filtered(self):
        rows=[[views.button('السعر','pc:home:0'),views.button('فتح','pc:open:'+ORDER),views.button('إدارة','ps:settings:0')]]
        safe=views.customer_rows(rows)
        self.assertEqual([b['callback_data'] for row in safe for b in row],['zc:pc:home:0','zc:pc:open:'+ORDER])

    def test_invalid_price_input_keeps_review_open_without_saving(self):
        waiting={'action':'partner_sheet_price','expires':time.monotonic()+600}
        self.assertTrue(self.bot._commerce_text_input(9,'ليس مبلغاً',waiting))
        self.assertEqual(self.bot._waiting_user_inputs[9],waiting)
        self.client.rpc.assert_not_called()

    def test_admin_can_download_only_the_frozen_file_bound_to_the_order(self):
        root=Path.cwd()/'scratch'/('partner-sheet-download-'+uuid4().hex)
        folder=root/'.commerce/inputs';folder.mkdir(parents=True)
        content=b'isolated frozen file bytes';digest=hashlib.sha256(content).hexdigest()
        path=folder/(digest+'.xlsx');path.write_bytes(content)
        self.bot.project_root=root
        self.request['source']['file_sha256']=digest;self.client.rpc.return_value=self.page
        self.bot._archive_transport=Mock()
        self.bot._archive_transport.return_value.api.return_value={'message_id':77,'chat':{'id':9},
            'document':{'file_id':'fixture-file','file_unique_id':'fixture-unique','file_size':len(content)}}
        self.bot._partner_sheet_admin('ps:file:'+ORDER,9,4,{'id':'native'})
        delivery=self.bot._archive_transport.return_value.api
        self.assertEqual(delivery.call_count,1);self.assertEqual(delivery.call_args.args[1]['chat_id'],9)
        path.write_bytes(b'changed after approval request')
        self.bot._partner_sheet_admin('ps:file:'+ORDER,9,4,{'id':'native-again'})
        self.assertEqual(delivery.call_count,1)

    def test_active_partner_cannot_fall_back_to_legacy_checkout_with_missing_or_invalid_policy(self):
        root=Path.cwd()/'scratch'/('partner-sheet-routing-'+uuid4().hex)
        root.mkdir(parents=True)
        bot=CommerceCheckoutMixin();bot.project_root=root;bot._commerce=Mock()
        bot._commerce.return_value.rpc.return_value={'is_partner':True}
        with patch.dict(os.environ,{'ZAIN_COMMERCE_CHECKOUT':'0','ZAIN_COMMERCE_PILOT_IDS':''}):
            self.assertTrue(bot._commerce_checkout_enabled(31))
            (root/'.commerce').mkdir();(root/'.commerce/runtime-policy.json').write_text('{broken',encoding='utf-8')
            self.assertTrue(bot._commerce_checkout_enabled(31))
        self.assertEqual(bot._commerce.return_value.rpc.call_count,2)


if __name__=='__main__':unittest.main()

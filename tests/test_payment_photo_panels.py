import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views
from telegram_bot import photo_panels as media
import test_commerce_vodafone_cash as vodafone_fixture


class PhotoPanelTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.bot=TelegramBotRunner.__new__(TelegramBotRunner)
        self.bot.project_root=Path(temp.name);self.bot._ui_lock=threading.RLock()
        self.bot.authorized_id=9;self.bot.authorized_ids={9};self.bot._authorized_override=True
        self.bot._api_call=Mock(return_value={'ok':True,'result':{'message_id':7,'photo':[{'file_id':'logo-cache'}]}})
        self.bot.api_url='https://api.telegram.org/botTEST_ONLY'
        self.bot._session=Mock()
        self.bot._session.post.return_value.json.return_value={'ok':True,'result':{'message_id':7,'photo':[{'file_id':'logo-cache'}]}}
        self.panel=views.Panel('<b>فودافون كاش</b>\nأرسل إلى <code>01050740996</code>',
            [[views.button('أرسلت المبلغ','vc:phone:order')]],photo='vodafone_cash')

    def test_first_payment_panel_is_one_photo_with_details_and_buttons_below_it(self):
        self.bot._show_panel(31,self.panel)
        self.bot._session.post.assert_called_once()
        request=self.bot._session.post.call_args
        self.assertEqual(request.args[0],self.bot.api_url+'/sendPhoto')
        fields=request.kwargs['data']
        self.assertIn('01050740996',fields['caption'])
        self.assertEqual(fields['show_caption_above_media'],'false')
        self.assertEqual(json.loads(fields['reply_markup'])['inline_keyboard'],self.panel.rows)
        self.bot._api_call.assert_not_called()

    def test_existing_menu_converts_in_place_and_subsequent_steps_reuse_same_message(self):
        self.bot._show_panel(31,self.panel,7)
        self.assertEqual(self.bot._session.post.call_args.args[0],self.bot.api_url+'/editMessageMedia')
        fields=self.bot._session.post.call_args.kwargs['data']
        self.assertEqual(json.loads(fields['media'])['media'],'attach://photo')
        self.bot._show_panel(31,self.panel,7)
        self.assertEqual(self.bot._session.post.call_count,1)
        self.bot._api_call.assert_called_once()
        method,payload=self.bot._api_call.call_args.args
        self.assertEqual(method,'editMessageMedia');self.assertEqual(payload['message_id'],7)
        self.assertEqual(payload['media']['media'],'logo-cache')
        self.assertFalse(payload['media']['show_caption_above_media'])

    def test_normal_navigation_from_photo_preserves_image_and_retires_old_buttons(self):
        media.remember_media(self.bot,31,{'message_id':7,'photo':[{'file_id':'logo'}]})
        self.bot._show_panel(31,views.Panel('المحفظة',[[views.button('رجوع','home')]]),7)
        self.assertEqual([c.args[0] for c in self.bot._api_call.call_args_list],['sendMessage','editMessageReplyMarkup'])
        self.assertEqual(self.bot._api_call.call_args.args[1]['reply_markup'],{'inline_keyboard':[]})

    def test_ambiguous_upload_does_not_send_a_second_message(self):
        self.bot._session.post.side_effect=TimeoutError('synthetic')
        self.bot._show_panel(31,self.panel)
        self.bot._session.post.assert_called_once();self.bot._api_call.assert_not_called()

    def test_ambiguous_edit_and_server_error_do_not_duplicate_panel(self):
        self.bot._vodafone_logo_file_id='cached'
        for result in (None,{'ok':False,'error_code':500},{'ok':False,'error_code':429}):
            self.bot._api_call.reset_mock();self.bot._api_call.return_value=result
            self.bot._show_panel(31,self.panel,7)
            self.bot._api_call.assert_called_once()
            self.assertEqual(self.bot._api_call.call_args.args[0],'editMessageMedia')

    def test_confirmed_uneditable_message_falls_back_to_one_combined_photo(self):
        self.bot._vodafone_logo_file_id='cached'
        self.bot._api_call.side_effect=[{'ok':False,'error_code':400,'description':"message can't be edited"},
            {'ok':True,'result':{'message_id':8,'photo':[{'file_id':'cached'}]}},{'ok':True}]
        self.bot._show_panel(31,self.panel,7)
        self.assertEqual([c.args[0] for c in self.bot._api_call.call_args_list],
            ['editMessageMedia','sendPhoto','editMessageReplyMarkup'])
        caption=self.bot._api_call.call_args_list[1].args[1]['caption']
        visible=caption.translate(str.maketrans('', '', '\u200f\u2066\u2069'))
        self.assertEqual(visible,'فودافون كاش\nأرسل إلى 01050740996')
        self.assertIn('01050740996',caption)

    def test_expired_file_id_reuploads_only_after_confirmed_file_rejection(self):
        self.bot._vodafone_logo_file_id='expired'
        self.bot._api_call.return_value={'ok':False,'error_code':400,'description':'wrong file identifier'}
        self.bot._show_panel(31,self.panel,7)
        self.bot._api_call.assert_called_once();self.bot._session.post.assert_called_once()
        self.assertEqual(self.bot._vodafone_logo_file_id,'logo-cache')

    def test_missing_local_logo_keeps_payment_text_and_buttons_available(self):
        with patch.object(media,'VODAFONE_LOGO',self.bot.project_root/'missing.jpg'):
            self.bot._show_panel(31,self.panel)
        self.assertEqual(self.bot._api_call.call_args.args[0],'sendMessage')
        self.assertIn('01050740996',self.bot._api_call.call_args.args[1]['text'])

    def test_not_modified_never_creates_duplicate_photo(self):
        self.bot._vodafone_logo_file_id='cached'
        self.bot._api_call.return_value={'ok':False,'error_code':400,'description':'message is not modified'}
        self.bot._show_panel(31,self.panel,7)
        self.bot._api_call.assert_called_once()

    def test_long_details_are_paginated_without_losing_html_entities_or_emoji(self):
        plain='💳 فودافون & كاش\n'+'سبب مفصل للرفض 😃 '*160+'\n01050740996'
        html='<b>💳 فودافون &amp; كاش</b>\n'+('سبب مفصل للرفض 😃 '*160)+'\n<code>01050740996</code>'
        pages=media.caption_pages(html)
        self.assertGreater(len(pages),1)
        self.assertEqual(''.join(p['caption'] for p in pages),plain)
        for page in pages:
            self.assertLessEqual(media.units(page['caption']),900)
            for entity in page['caption_entities']:
                self.assertGreater(entity['length'],0)
                self.assertLessEqual(entity['offset']+entity['length'],media.units(page['caption']))
        self.assertEqual(pages[-1]['caption_entities'][-1]['type'],'code')

    def test_pagination_is_bound_to_owner_and_message_without_consuming_waiting_input(self):
        self.bot._vodafone_logo_file_id='cached'
        self.bot._waiting_user_inputs={31:{'action':'commerce_vodafone_proof'}}
        long=views.Panel('<b>تفاصيل</b>\n'+'تفصيل '*450,self.panel.rows,photo='vodafone_cash')
        self.bot._show_panel(31,long,7)
        rows=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        callback={'id':'page','data':rows[-1][0]['callback_data'],'message':{'message_id':7}}
        self.bot._api_call.reset_mock();media.show_photo_page(self.bot,31,callback)
        self.assertEqual([c.args[0] for c in self.bot._api_call.call_args_list],['answerCallbackQuery','editMessageMedia'])
        self.assertEqual(self.bot._waiting_user_inputs[31]['action'],'commerce_vodafone_proof')
        for actor,mid in ((32,7),(31,999)):
            self.bot._api_call.reset_mock();media.show_photo_page(self.bot,actor,{**callback,'message':{'message_id':mid}})
            self.bot._api_call.assert_called_once();self.assertTrue(self.bot._api_call.call_args.args[1]['show_alert'])
        next(iter(self.bot._photo_panel_pages.values()))['expires']=time.monotonic()-1
        self.bot._api_call.reset_mock();media.show_photo_page(self.bot,31,callback)
        self.bot._api_call.assert_called_once()

    def test_real_page_callback_route_keeps_waiting_input_and_respects_subscription(self):
        self.bot._vodafone_logo_file_id='cached';self.bot._subscription_status=Mock(return_value='member')
        self.bot._waiting_user_inputs={31:{'action':'commerce_vodafone_proof'}}
        self.bot._commerce_client=Mock()
        self.bot._show_panel(31,views.Panel('تفصيل '*450,self.panel.rows,photo='vodafone_cash'),7)
        button=self.bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard'][-1][0]
        callback={'id':'page','data':button['callback_data'],'from':{'id':31},
            'message':{'message_id':7,'chat':{'id':31,'type':'private'},'photo':[{'file_id':'cached'}]}}
        self.bot._api_call.reset_mock();self.bot._handle_callback(callback)
        self.assertEqual([c.args[0] for c in self.bot._api_call.call_args_list],['answerCallbackQuery','editMessageMedia'])
        self.assertEqual(self.bot._waiting_user_inputs[31]['action'],'commerce_vodafone_proof')
        self.bot._commerce_client.rpc.assert_not_called()
        self.bot._subscription_status.return_value='missing';self.bot._api_call.reset_mock()
        self.bot._handle_callback(callback)
        self.assertNotIn('editMessageMedia',[c.args[0] for c in self.bot._api_call.call_args_list])

    def test_replacing_panel_invalidates_earlier_details_pages(self):
        self.bot._vodafone_logo_file_id='cached'
        self.bot._show_panel(31,views.Panel('تفصيل '*450,self.panel.rows,photo='vodafone_cash'),7)
        old_token=next(iter(self.bot._photo_panel_pages))
        self.bot._show_panel(31,self.panel,7)
        self.assertNotIn(old_token,self.bot._photo_panel_pages)

    def test_long_panel_fallback_rebinds_page_buttons_to_replacement_message(self):
        self.bot._vodafone_logo_file_id='cached'
        self.bot._api_call.side_effect=[{'ok':False,'error_code':400,'description':"message can't be edited"},
            {'ok':True,'result':{'message_id':8,'photo':[{'file_id':'cached'}]}},{'ok':True}]
        self.bot._show_panel(31,views.Panel('تفصيل '*450,self.panel.rows,photo='vodafone_cash'),7)
        entry=next(iter(self.bot._photo_panel_pages.values()))
        self.assertEqual(entry['message_id'],8)


class VodafoneLayoutTests(unittest.TestCase):
    def setUp(self):
        self.f=vodafone_fixture.VodafoneTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.bot=self.f.bot

    def test_vodafone_actions_share_one_row_with_crypto_visible_or_hidden(self):
        for visible in (True,False):
            with patch('telegram_bot.config.show_crypto_payments',return_value=visible):
                self.bot._wallet_callback('cw:topup:0',31,4,{})
            rows=self.bot._show_panel.call_args.args[1].rows
            self.assertEqual([b['callback_data'] for b in rows[0]],['zc:vc:start:0','zc:vc:list:0'])
            self.assertEqual([b['text'] for b in rows[0]],['دفع فودافون كاش','طلباتي فودافون كاش'])

    def test_wallet_groups_funding_history_and_offers_without_hiding_partner_actions(self):
        with patch.object(self.bot,'_wallet_state',return_value={'customer':{'available_minor':6000},'is_partner':False}):
            self.bot._show_wallet_panel(31,4)
        panel=self.bot._show_panel.call_args.args[1]
        self.assertEqual([len(row) for row in panel.rows],[2,2,2,1])
        routes={b['callback_data'] for row in panel.rows for b in row}
        self.assertTrue({'zc:cw:topup:0','zc:cc:redeem:0','zc:cw:list:0','zc:oh:ledger:0','zc:of:list:0','zc:home'}<=routes)
        self.assertNotIn('zc:pf:home:0',routes)
        with patch.object(self.bot,'_wallet_state',return_value={'customer':{'available_minor':6000},'is_partner':True,'partner':{}}):
            self.bot._show_wallet_panel(31,4)
        rows=self.bot._show_panel.call_args.args[1].rows
        self.assertEqual([b['callback_data'] for b in rows[-2]],['zc:pf:home:0','zc:po:list:0'])
        self.assertEqual(rows[-1][0]['callback_data'],'zc:home')

    def test_legacy_wallet_does_not_repeat_the_same_topup_action(self):
        panel=views.wallet_panel(31,60,show_crypto=True)
        routes=[b['callback_data'] for row in panel.rows for b in row]
        self.assertEqual(routes.count('zc:wallet:topup_menu'),1)
        self.assertIn('zc:wallet:redeem',routes)
        self.assertTrue(all(len(row)<=2 for row in panel.rows))

    def test_customer_payment_screens_keep_photo_and_real_receipt_review_uses_original_proof(self):
        key=self.f.create();self.assertEqual(self.bot._show_panel.call_args.args[1].photo,'vodafone_cash')
        self.f.callback('phone',key);self.assertEqual(self.bot._show_panel.call_args.args[1].photo,'vodafone_cash')
        self.f.text('01000000000');self.assertEqual(self.bot._show_panel.call_args.args[1].photo,'vodafone_cash')
        self.f.text('إشعار اختبار')
        self.f.callback('review',key,9)
        payload=self.bot._api_call.call_args.args[1]
        self.assertIn('نص الإشعار',payload['text'])
        self.f.prepare_approval(key);self.f.callback('approve',key,9)
        self.assertEqual(self.f.balance,6050)

    def test_new_photo_message_id_becomes_the_payment_input_target(self):
        self.bot._show_panel.return_value={'ok':True,'result':{'message_id':888}}
        self.f.callback('start')
        self.assertEqual(self.bot._waiting_user_inputs[31]['message_id'],888)

    def test_admin_receipt_preview_preserves_customer_uploaded_image(self):
        key=self.f.submit(photo=True);self.f.callback('review',key,9)
        method,payload=self.bot._api_call.call_args.args
        self.assertEqual(method,'sendPhoto');self.assertEqual(payload['photo'],'photo-fixture')


if __name__=='__main__':unittest.main()

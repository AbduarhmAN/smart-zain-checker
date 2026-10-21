"""Native button contracts: height, role isolation, navigation and safe decisions."""
import copy
import json
import unittest
from unittest.mock import Mock
from telegram_bot import views
from telegram_bot.button_layout import (reply_keyboard, REPLY_ACTIONS, inline_rows,
    normalize_markup, ADMIN_SHORTCUTS, CUSTOMER_SHORTCUTS)


def flatten(rows):return [item for row in rows for item in row]


class ButtonLayoutTests(unittest.TestCase):
    def test_both_persistent_roles_have_two_columns_and_every_action(self):
        for admin, definition in ((True, ADMIN_SHORTCUTS), (False, CUSTOMER_SHORTCUTS)):
            with self.subTest(admin=admin):
                markup=reply_keyboard(admin)
                self.assertEqual([len(row) for row in markup['keyboard']], [2,2,2,1])
                labels=[item['text'] for item in flatten(markup['keyboard'])]
                self.assertEqual(labels,[text for row in definition for text,_,_ in row])
                self.assertTrue(all(text in REPLY_ACTIONS for text in labels))
                self.assertTrue(markup['resize_keyboard']);self.assertTrue(markup['is_persistent'])

    def test_custom_icons_survive_and_only_the_main_action_has_accent(self):
        buttons=flatten(reply_keyboard(True, {'upload':'12345','wallet':'54321'})['keyboard'])
        self.assertEqual(buttons[0]['icon_custom_emoji_id'],'12345')
        self.assertEqual(next(b for b in buttons if 'محفظتي' in b['text'])['icon_custom_emoji_id'],'54321')
        self.assertEqual([b['style'] for b in buttons if 'style' in b],['primary'])

    def test_customer_shortcuts_do_not_expose_global_controls(self):
        actions={REPLY_ACTIONS[b['text']] for b in flatten(reply_keyboard(False)['keyboard'])}
        self.assertEqual(actions,{'/upload','/status','/drafts','/wallet','/redeem','/help','/start'})
        self.assertFalse({'/queue','/partners','/pause','/resume'} & actions)

    def test_pagination_and_footer_compact_without_changing_callbacks(self):
        rows=[[views.button('السابق','page:0')],[views.button('التالي','page:2')],
              [views.button('تحديث','page:1')],[views.button('الرئيسية','home')]]
        before=copy.deepcopy(rows);result=inline_rows(rows)
        self.assertEqual([len(row) for row in result],[2,2])
        self.assertEqual(flatten(result),flatten(before));self.assertEqual(rows,before)
        self.assertEqual(inline_rows(result),result)

    def test_financial_and_stop_confirmations_keep_full_rows(self):
        rows=[[views.button('أؤكد الاستلام وأشحن المحفظة','approve:nonce','success')],
              [views.button('رفض مع ذكر السبب','reject:nonce')],
              [views.button('أؤكد إيقاف طلبي','oh:stopconfirm:nonce')],
              [views.button('إلغاء','home')]]
        self.assertEqual(inline_rows(rows),rows)

    def test_existing_vodafone_pair_and_photo_panel_stay_intact(self):
        pair=[views.button('دفع فودافون كاش','vc:start:0'),views.button('طلباتي فودافون كاش','vc:list:0')]
        panel=views.Panel('تفاصيل الدفع',[pair,[views.button('المحفظة','wallet')]],photo='vodafone_cash')
        self.assertEqual(panel.rows[0],pair);self.assertEqual(panel.photo,'vodafone_cash')

    def test_record_titles_and_long_navigation_are_not_squeezed(self):
        rows=[[views.button('طلب 123456 · 150 جنيه · بانتظار المراجعة','oh:open:abcdef')],
              [views.button('العودة إلى تفاصيل طلب فودافون كاش الخاص بي','vc:list:0')],
              [views.button('الرئيسية','home')]]
        self.assertEqual(inline_rows(rows),rows)

    def test_wide_rows_normalize_idempotently_and_keep_custom_fields(self):
        rows=[[{**views.button('تحديث','page:'+str(i)), 'icon_custom_emoji_id':str(i)} for i in range(4)],
              [views.url_button('التواصل مع الدعم','https://t.me/zainchecker?direct')]]
        result=inline_rows(rows)
        self.assertTrue(all(len(row)<=2 for row in result))
        self.assertEqual(flatten(result),flatten(rows));self.assertEqual(inline_rows(result),result)

    def test_json_and_empty_markup_keep_metadata_and_button_removal(self):
        markup={'inline_keyboard':[[views.button('السابق','page:0')],[views.button('التالي','page:2')]],'custom':'preserved'}
        self.assertEqual(json.loads(normalize_markup(json.dumps(markup))),normalize_markup(markup))
        self.assertEqual(normalize_markup({'inline_keyboard':[]}),{'inline_keyboard':[]})
        self.assertEqual(normalize_markup({'remove_keyboard':True}),{'remove_keyboard':True})

    def test_legacy_reply_keyboards_keep_every_button_with_two_columns(self):
        markup={'keyboard':[[{'text':str(i)}] for i in range(7)],'resize_keyboard':True}
        result=normalize_markup(markup)
        self.assertTrue(all(len(row)<=2 for row in result['keyboard']))
        self.assertEqual(flatten(result['keyboard']),flatten(markup['keyboard']))

    def test_all_shortcut_labels_exit_waiting_inputs_and_reach_the_same_command(self):
        import test_telegram_ui as fixture
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._refresh_partner_menu_for_chat=Mock();f.bot._commerce_command=Mock(return_value=True)
        f.bot._handle_user_text_input=Mock()
        for admin, actor in ((True,123),(False,456)):
            for button in flatten(reply_keyboard(admin)['keyboard']):
                f.bot._waiting_user_inputs[actor]={'action':'waiting_fixture'}
                f.bot._handle_update({'message':{'from':{'id':actor},'chat':{'id':actor,'type':'private'},'text':button['text']}})
                self.assertNotIn(actor,f.bot._waiting_user_inputs)
                self.assertEqual(f.bot._commerce_command.call_args.args[0],REPLY_ACTIONS[button['text']])
        f.bot._handle_user_text_input.assert_not_called()

    def test_real_transport_normalizes_raw_notification_markup_without_mutation(self):
        import test_telegram_ui as fixture
        from telegram_bot.bot import TelegramBotRunner
        f=fixture.TelegramUiTests();f.setUp();self.addCleanup(f.doCleanups)
        f.bot._session=Mock();f.bot._session.post.return_value.json.return_value={'ok':True}
        payload={'chat_id':123,'reply_markup':{'inline_keyboard':[
            [views.button('تحديث','page:0')],[views.button('الرئيسية','home')]]}}
        before=copy.deepcopy(payload)
        self.assertTrue(TelegramBotRunner._api_call(f.bot,'editMessageReplyMarkup',payload)['ok'])
        actual=f.bot._session.post.call_args.kwargs['json']
        self.assertEqual([len(row) for row in actual['reply_markup']['inline_keyboard']],[2])
        self.assertEqual(payload,before)


if __name__=='__main__':unittest.main()

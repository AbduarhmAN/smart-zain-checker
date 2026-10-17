"""Native copyable message text adds no buttons and never rounds money."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from telegram_bot import views
from telegram_bot.ui import TelegramUiMixin


class ClipboardControlsTests(unittest.TestCase):
    def test_tiny_money_is_inline_code_without_rounding(self):
        self.assertEqual(views.copyable_message('المبلغ: 0.00001 جنيه'),'المبلغ: <code>0.00001</code> جنيه')

    def test_bold_money_does_not_create_invalid_bold_code_nesting(self):
        actual=views.copyable_message('<b>المبلغ: 1,234.00001 جنيه</b>')
        self.assertIn('<code>1,234.00001</code>',actual)
        self.assertNotIn('<b><code>',actual)
        self.assertIn('<b>المبلغ: </b>',actual)
        self.assertEqual(views.copyable_message('<b>0.00001</b> جنيه'),'<code>0.00001</code> جنيه')

    def test_existing_code_pre_links_and_entities_are_preserved(self):
        source='<code>0.00001 جنيه</code>\n<pre>A&amp;B</pre>\n<a href="https://example.com">1 USD</a>'
        self.assertEqual(views.copyable_message(source),source)

    def test_nested_formatting_is_closed_around_code_and_is_idempotent(self):
        actual=views.copyable_message('<b><i>الرصيد 0.00001 USDT فقط</i></b>')
        self.assertIn('</i></b><code>0.00001</code><b><i> USDT',actual)
        self.assertEqual(views.copyable_message(actual),actual)

    def test_real_renderer_preserves_action_rows_and_adds_no_copy_buttons(self):
        bot=SimpleNamespace(_get_user_mode=lambda _: 'customer',_api_call=Mock(return_value={'ok':True}))
        rows=[[views.button('الرئيسية','home')]]
        with patch('telegram_bot.ui.admin_ids',return_value={9}):
            TelegramUiMixin._show_panel(bot,31,views.Panel('المبلغ: 0.00001 جنيه',rows))
        payload=bot._api_call.call_args.args[1]
        self.assertEqual(payload['reply_markup']['inline_keyboard'],rows)
        self.assertEqual(payload['text'],'المبلغ: <code>0.00001</code> جنيه')

    def test_list_keeps_code_amount_and_currency_on_the_same_line(self):
        source='<code>EXAMPLE100</code> · 30.00 جنيه لكل عميل\nالاستخدام: 1 / 6'
        actual=views.copyable_message(source)
        self.assertEqual(actual,'<code>EXAMPLE100</code> · <code>30.00</code> جنيه لكل عميل\nالاستخدام: 1 / 6')
        self.assertNotIn('<pre>',actual)
        self.assertEqual(actual.count('\n'),source.count('\n'))


if __name__=='__main__':unittest.main()

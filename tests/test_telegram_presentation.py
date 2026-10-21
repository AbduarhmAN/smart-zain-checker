"""Presentation contracts with fictional fixtures and no live Telegram requests."""
from html.parser import HTMLParser
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from telegram_bot import views
from telegram_bot.identity import ProfileNames, profile_name
from telegram_bot.presentation import RLM, LRI, PDI, DIVIDER, rtl_html
from telegram_bot.commerce.partner_views import partners_panel, partner_details
from telegram_bot.sheet_workflow import SheetWorkflowMixin
from telegram_bot.bot import TelegramBotRunner

PARTNER='11111111-1111-4111-8111-111111111111'
ENDED='22222222-2222-4222-8222-222222222222'


def actions(panel):
    return [item.get('callback_data','')[3:] for row in panel.rows for item in row]


class PresentationTests(unittest.TestCase):
    def test_profile_names_preserve_registered_first_and_last_names(self):
        store=ProfileNames()
        store.remember({'id':10,'first_name':'اسم','last_name':'تجريبي','username':'NOT_A_DISPLAY_NAME'})
        self.assertEqual(store.resolve(10),'اسم تجريبي')
        self.assertNotIn('NOT_A_DISPLAY_NAME',store.resolve(10))
        self.assertEqual(profile_name('اسم\u202e\nمخادع','آخر'),'اسم مخادع آخر')

    def test_identity_uses_exact_private_chat_and_caches_success(self):
        store=ProfileNames()
        fetch=Mock(return_value={'ok':True,'result':{'id':10,'type':'private','first_name':'اسم مسجل'}})
        self.assertEqual(store.resolve(10,fetch),'اسم مسجل')
        self.assertEqual(store.resolve(10,fetch),'اسم مسجل')
        fetch.assert_called_once_with('getChat',{'chat_id':10},timeout=3)

    def test_unknown_or_mismatched_identity_does_not_invent_a_name(self):
        store=ProfileNames()
        fetch=Mock(return_value={'ok':True,'result':{'id':11,'type':'private','first_name':'حساب آخر'}})
        self.assertEqual(store.resolve(10,fetch),'الاسم غير متاح')
        self.assertEqual(store.resolve(10,fetch),'الاسم غير متاح')
        fetch.assert_called_once()
        self.assertEqual(store.resolve(True,fetch),'الاسم غير متاح')

    def test_profile_names_persist_without_usernames_or_financial_ids(self):
        root=Path(tempfile.mkdtemp(prefix='profile-names-retained-'))
        store=ProfileNames(root)
        store.remember({'id':10,'first_name':'اسم مسجل','username':'NOT_A_DISPLAY_NAME'})
        self.assertEqual(ProfileNames(root).resolve(10),'اسم مسجل')
        self.assertNotIn('NOT_A_DISPLAY_NAME',(root/'.commerce/telegram-profiles.json').read_text(encoding='utf-8'))

    def test_rtl_values_remain_copyable_and_formatting_is_idempotent(self):
        text='<b>عنوان</b>\nرقم الحساب: <code>123456789</code>\nالمبلغ: <code>10.25</code> جنيه'
        formatted=rtl_html(text)
        self.assertTrue(all(line.startswith(RLM) for line in formatted.splitlines()))
        self.assertIn(LRI+'<code>123456789</code>'+PDI,formatted)
        self.assertNotIn('<code>'+LRI,formatted)
        self.assertEqual(rtl_html(formatted),formatted)

    def test_multiline_code_and_preformatted_content_remain_byte_identical(self):
        text='تعليمات\n<code>123\n456</code>\n<pre>value\nnext</pre>\nالتالي'
        formatted=rtl_html(text)
        self.assertIn('<code>123\n456</code>',formatted)
        self.assertIn('<pre>value\nnext</pre>',formatted)
        self.assertEqual(rtl_html(formatted),formatted)

    def test_file_cards_have_owner_names_dividers_and_original_routes(self):
        jobs=[{'id':'job-'+str(i),'filename':'file-'+str(i)+'.xlsx','sheet_name':'Sheet',
               'column_mapping':{'telegram_chat_id':i},'status':'active','completed':2,'total_records':10} for i in (1,2)]
        names={1:'اسم أول',2:'<اسم ثان>'}
        panel=views.queue(jobs,person_name=names.get)
        self.assertEqual(panel.text.count(DIVIDER),2)
        self.assertIn('اسم أول',panel.text);self.assertIn('&lt;اسم ثان&gt;',panel.text)
        self.assertIn('رقم حساب تيليجرام: <code>1</code>',panel.text)
        self.assertIn('job:job-1',actions(panel));self.assertNotIn('طابور',panel.text)

    def records(self):
        return [{'id':PARTNER,'telegram_user_id':10,'code':'_ZAIN','state':'active','commission_basis_points':3000},
                {'id':ENDED,'telegram_user_id':11,'code':'AHMED','state':'suspended','ended_at':'2026-10-21T10:00:00Z'}]

    def test_current_partner_list_has_only_name_and_payout_for_each_person(self):
        panel=partners_panel(self.records(),lambda _: 'اسم مسجل','FixtureBot')
        self.assertNotIn('AHMED',panel.text)
        self.assertIn('اسم مسجل',panel.text);self.assertIn('ref__ZAIN',panel.text)
        self.assertEqual(actions(panel)[:2],['cp:profile:'+PARTNER,'wa:paypartner:10'])
        self.assertNotIn('cp:terms:'+PARTNER,actions(panel))
        self.assertEqual(len(panel.rows[0]),2)

    def test_partner_details_keep_terms_termination_earnings_and_access(self):
        record=self.records()[0]
        panel=partner_details(record,'اسم مسجل','FixtureBot')
        wanted={'cp:terms:'+PARTNER,'pl:remove:'+PARTNER,'ps:access:'+PARTNER,'pf:partner:10','wa:paypartner:10'}
        self.assertTrue(wanted<=set(actions(panel)))
        self.assertIn('_ZAIN',panel.text);self.assertIn('اسم مسجل',panel.text)
        self.assertTrue(all(len(row)<=2 for row in panel.rows))

    def test_ended_partner_details_retain_the_financial_history(self):
        panel=partner_details(self.records()[1],'اسم سابق')
        self.assertIn('AHMED',panel.text);self.assertIn('pf:partner:11',actions(panel))
        self.assertNotIn('pl:remove:'+ENDED,actions(panel))

    def test_suspended_current_partner_is_not_confused_with_an_ended_partner(self):
        record=dict(self.records()[0],state='suspended')
        panel=partners_panel([record],lambda _: 'اسم مسجل')
        self.assertIn('موقوفة مؤقتًا',panel.text)
        self.assertIn('cp:profile:'+PARTNER,actions(panel))

    def test_review_files_are_newest_first_before_pagination(self):
        class Harness(SheetWorkflowMixin):pass
        h=Harness();h._ui_lock=threading.RLock();h.authorized_ids={1};h.authorized_id=1
        h._sheet_drafts={str(i):{'chat':1,'path':Path('fixture-'+str(i)+'.xlsx'),
            'created_at':i,'expires':time.monotonic()+3600} for i in range(12)}
        panel=h._drafts_panel(1)
        self.assertEqual(actions(panel)[:8],['map:'+str(i)+':review' for i in range(11,3,-1)])

    def test_review_creation_times_and_newest_order_survive_disk_restore(self):
        from telegram_bot.sheet_workflow import file_version
        class Harness(SheetWorkflowMixin):pass
        h=Harness();h.project_root=Path(tempfile.mkdtemp(prefix='ui_draft_order_retained_'))
        h._ui_lock=threading.RLock();h._sheet_drafts={};h.authorized_ids={1};h.authorized_id=1
        h._restore_sheet_drafts()
        tokens=[]
        for i in range(12):
            path=h.project_root/('fixture-'+str(i)+'.xlsx');path.write_bytes(b'retained offline fixture')
            token=format(i,'012x');tokens.append(token)
            h._sheet_drafts[token]={'path':path,'chat':1,'created_at':1000+i,'expires':time.monotonic()+3600,
                'version':file_version(path),'mapping':{},'metadata':{},'validation':{},'columns':[],
                'sheets':[],'mode':'fast','amount_target':'remaining'}
        # Use the application's actual mode to avoid coupling this test to display labels.
        from telegram_bot.sheet_workflow import MODES
        for draft in h._sheet_drafts.values():draft['mode']=next(iter(MODES))
        self.assertTrue(h._persist_sheet_drafts())
        restored=Harness();restored.project_root=h.project_root;restored._ui_lock=threading.RLock()
        restored._sheet_drafts={};restored.authorized_ids={1};restored.authorized_id=1
        restored._restore_sheet_drafts()
        self.assertEqual(len(restored._sheet_drafts),12)
        self.assertEqual(restored._sheet_drafts[tokens[-1]]['created_at'],1011)
        self.assertEqual(actions(restored._drafts_panel(1))[:8],['map:'+token+':review' for token in tokens[:3:-1]])

    def test_start_dispatches_one_home_without_an_extra_welcome_message(self):
        bot=TelegramBotRunner.__new__(TelegramBotRunner)
        bot.authorized_id=123;bot.authorized_ids={123};bot._authorized_override=True
        bot._ui_lock=threading.RLock();bot._waiting_user_inputs={}
        bot._maintenance_gate=Mock(return_value=False);bot._support_handle_message=Mock(return_value=False)
        bot._subscription_gate=Mock(return_value=True);bot._refresh_partner_menu_for_chat=Mock()
        bot._vodafone_handle_media=Mock(return_value=False);bot._commerce_command=Mock(return_value=False)
        bot._api_call=Mock();bot._reply=Mock();bot._dispatch_ui=Mock()
        bot._handle_update({'message':{'from':{'id':123},'chat':{'id':123,'type':'private'},'text':'/start'}})
        bot._dispatch_ui.assert_called_once_with('home',123)
        bot._reply.assert_not_called()


if __name__=='__main__':unittest.main()

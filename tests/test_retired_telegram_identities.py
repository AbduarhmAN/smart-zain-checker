"""Merged actors cannot recreate accounts or inherit another actor's authority."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from telegram_bot.identity import ProfileNames,TelegramIdentityMixin,retired_telegram_identity
from telegram_bot.bot import TelegramBotRunner


class RetiredTelegramIdentityTests(unittest.TestCase):
    def setUp(self):
        # Keep all fixtures; no files or directories are deleted.
        self.root=Path(tempfile.mkdtemp(prefix='retired-identity-retained-'))
        (self.root/'.commerce').mkdir()
        self.path=self.root/'.commerce/retired-telegram-identities.json'
        self.path.write_text(json.dumps({'version':1,'retired':{'111111111':{'canonical_telegram_user_id':222222222}}}),encoding='utf-8')

    def message(self,actor=111111111):
        return {'message':{'from':{'id':actor,'is_bot':False},'chat':{'id':actor,'type':'private'},'text':'/start'}}

    def test_exact_retired_identity_only(self):
        self.assertTrue(retired_telegram_identity(self.root,111111111))
        for actor in (222222222,333333333,None,True,'invalid'):
            self.assertFalse(retired_telegram_identity(self.root,actor))

    def test_absent_registry_preserves_other_installations(self):
        self.assertFalse(retired_telegram_identity(self.root/'other',111111111))
        self.assertFalse(retired_telegram_identity(None,111111111))

    def test_corrupt_registry_cannot_silently_reenable_retired_accounts(self):
        self.path.write_text('{',encoding='utf-8')
        with self.assertRaises(RuntimeError):retired_telegram_identity(self.root,111111111)

    def test_self_alias_is_rejected(self):
        self.path.write_text(json.dumps({'version':1,'retired':{'111111111':{'canonical_telegram_user_id':111111111}}}),encoding='utf-8')
        with self.assertRaises(RuntimeError):retired_telegram_identity(self.root,111111111)

    def test_profile_cache_cannot_recreate_retired_person(self):
        store=ProfileNames(self.root)
        self.assertEqual(store.remember({'id':111111111,'first_name':'Retired'}),'')
        self.assertNotIn('111111111',store.records)
        self.assertEqual(store.remember({'id':222222222,'first_name':'Canonical'}),'Canonical')
        fetch=Mock()
        self.assertEqual(store.resolve(111111111,fetch),'حساب مدمج')
        fetch.assert_not_called()

    def test_gate_runs_before_profile_support_or_callback_side_effects(self):
        bot=TelegramBotRunner.__new__(TelegramBotRunner);bot.project_root=self.root
        bot._remember_update_identity=Mock(side_effect=AssertionError('Profile write forbidden'))
        bot._maintenance_gate=Mock(side_effect=AssertionError('Routing forbidden'))
        bot._support_handle_message=Mock(side_effect=AssertionError('Support write forbidden'))
        bot._handle_callback=Mock(side_effect=AssertionError('Callback forbidden'))
        for update in (self.message(),{'callback_query':{'from':{'id':111111111},'message':self.message()['message']}},
                       {'edited_message':self.message()['message']}):
            bot._handle_update(update)
        bot._remember_update_identity.assert_not_called()
        bot._handle_callback.assert_not_called()

    def test_canonical_actor_is_not_rewritten_or_blocked(self):
        adapter=TelegramIdentityMixin();adapter.project_root=self.root
        update=self.message(222222222)
        self.assertFalse(adapter._retired_identity_gate(update))
        self.assertEqual(update['message']['from']['id'],222222222)

    def test_retired_actor_is_not_authorized_via_canonical_chat(self):
        adapter=TelegramIdentityMixin();adapter.project_root=self.root
        update=self.message();update['message']['chat']['id']=222222222
        self.assertTrue(adapter._retired_identity_gate(update))

    def test_forwarded_old_identity_does_not_block_a_different_sender(self):
        adapter=TelegramIdentityMixin();adapter.project_root=self.root
        update=self.message(333333333);update['message']['forward_from']={'id':111111111}
        self.assertFalse(adapter._retired_identity_gate(update))


if __name__=='__main__':unittest.main()

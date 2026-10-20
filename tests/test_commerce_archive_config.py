import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from telegram_bot.commerce.archive_config import ArchiveConfig


class ArchiveConfigTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='archive-config-retained-'))
        self.config=ArchiveConfig(self.root)
        self.chat={'id':-1001234567890,'type':'channel','title':'synthetic-private-archive'}
        self.member={'status':'administrator','can_post_messages':True}
        self.api=Mock(side_effect=lambda method,args:{'getMe':{'id':1,'is_bot':True},'getChat':self.chat,'getChatMember':self.member}[method])

    def test_bind_verifies_rights_and_never_enables_uploads_by_itself(self):
        data=self.config.bind(self.api,self.chat['id'],source_reference='synthetic-user-selection')
        self.assertFalse(data['enabled']);self.assertIsNone(data['policy'])
        self.assertEqual(self.config.load(),data)
        self.assertEqual([call.args[0] for call in self.api.call_args_list],['getMe','getChat','getChatMember'])

    def test_public_channel_non_admin_and_missing_post_right_fail(self):
        for chat,member in [(dict(self.chat,username='public'),self.member),(self.chat,{'status':'member'}),
                            (self.chat,{'status':'administrator','can_post_messages':False})]:
            self.chat,self.member=chat,member
            with self.assertRaises(ValueError):self.config.bind(self.api,chat['id'],source_reference='fixture')
        self.assertIsNone(self.config.load())

    def test_policy_requires_explicit_retention_and_preserves_destination(self):
        self.config.bind(self.api,self.chat['id'],source_reference='fixture')
        data=self.config.configure_policy(original_timing='after_completion',protection='plaintext_sha256',
            original_days=15,result_days=15,reference='synthetic-policy-decision')
        self.assertEqual(data['policy']['hash_algorithm'],'sha256')
        self.assertEqual(data['policy']['original_retention_days'],15)
        self.assertFalse(data['policy']['automatic_deletion']);self.assertFalse(data['enabled'])
        with self.assertRaises(ValueError):self.config.configure_policy(original_timing='after_completion',
            protection='plaintext_sha256',original_days=0,result_days=15,reference='fixture')

    def test_different_destination_is_not_silently_substituted(self):
        self.config.bind(self.api,self.chat['id'],source_reference='fixture')
        self.chat=dict(self.chat,id=-1009876543210)
        with self.assertRaises(ValueError):self.config.bind(self.api,self.chat['id'],source_reference='different')


if __name__=='__main__':unittest.main()

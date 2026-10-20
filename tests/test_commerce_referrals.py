import unittest
from telegram_bot.commerce.referrals import referral_link, referral_arguments


class ReferralTests(unittest.TestCase):
    def test_link_uses_valid_start_payload(self):
        self.assertEqual(referral_link('@ZainDemoBot',' partner_12 '),
                         'https://t.me/ZainDemoBot?start=ref_PARTNER_12')

    def test_both_sources_use_same_code(self):
        link = referral_arguments(11,'ref_PARTNER_12',source='deep_link')
        manual = referral_arguments(11,' partner_12 ',source='manual')
        self.assertEqual(link['p_code'],manual['p_code'])
        self.assertEqual(link['p_telegram_user_id'],manual['p_telegram_user_id'])

    def test_reject_links_and_noncode_content(self):
        for code in ['../abc','a b c','x','abc?start=other','أحمد','A'*49]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                referral_arguments(11,code,source='manual')

    def test_user_identity_and_source_are_explicit(self):
        for identity in [True,0,-1,'11']:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                referral_arguments(identity,'ABC',source='manual')
        with self.assertRaises(ValueError):
            referral_arguments(11,'ABC',source='deep_link')


if __name__ == '__main__':
    unittest.main()

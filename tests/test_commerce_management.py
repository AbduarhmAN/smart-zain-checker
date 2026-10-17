"""No network or deployment: checks administrative wrapper input handling."""
import unittest
from pathlib import Path
from telegram_bot.commerce.management import ROOT, migration_body


class ManagementTests(unittest.TestCase):
    def test_source_root_is_repository(self):
        self.assertEqual(ROOT, Path(__file__).resolve().parents[1])
        self.assertGreaterEqual(len(list((ROOT/'supabase/migrations').glob('*commerce*.sql'))), 5)

    def test_outer_transaction_only_is_removed(self):
        body = migration_body('-- comment\nbegin;\ndo $$ begin perform 1; end $$;\ncommit;\n')
        self.assertIn('do $$ begin perform 1; end $$;', body)
        self.assertNotIn('\nbegin;\n',body)
        self.assertFalse(body.rstrip().endswith('commit;'))

    def test_invalid_wrappers_rejected(self):
        for content in ['select 1;', 'begin;\nselect 1;', 'select 1;\ncommit;']:
            with self.subTest(content=content), self.assertRaises(ValueError):
                migration_body(content)


if __name__ == '__main__':
    unittest.main()

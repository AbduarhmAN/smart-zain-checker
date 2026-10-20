"""Offline contract tests. No temporary directories, cleanup or live services."""
import unittest
from telegram_bot.commerce.contracts import checker_event

ORDER = '8bfe52cf-f343-4dbe-aa8a-4d676b4a269b'


class CommerceContractTests(unittest.TestCase):
    def test_replay_key_and_payload_are_stable(self):
        a = checker_event(ORDER, 'job_1', 'check_outcome', 1,
                          row_number=2, check_type='service', outcome='succeeded')
        b = checker_event(ORDER, 'job_1', 'check_outcome', 1,
                          outcome='succeeded', check_type='service', row_number=2)
        self.assertEqual(a, b)
        copy = a.rpc_arguments()
        copy['p_payload']['outcome'] = 'failed'
        self.assertEqual(a.rpc_arguments()['p_payload']['outcome'], 'succeeded')

    def test_pending_and_deferred_are_not_billable_terminal_events(self):
        for outcome in ['pending', 'deferred', 'running', 'match', 'mismatch']:
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                checker_event(ORDER, 'job_1', 'check_outcome', 1,
                              row_number=2, check_type='service', outcome=outcome)

    def test_terminal_partition_must_cover_all_rows(self):
        event = checker_event(ORDER, 'job_1', 'execution_terminal', 1,
                              total=10, succeeded=7, failed=1, review=1, not_found=1,
                              result_sha256='a'*64)
        self.assertEqual(event.rpc_arguments()['p_payload']['total'], 10)
        with self.assertRaises(ValueError):
            checker_event(ORDER, 'job_1', 'execution_terminal', 1,
                          total=10, succeeded=7, failed=1, review=0, not_found=1,
                          result_sha256='a'*64)

    def test_delivery_requires_actual_receipt(self):
        valid = dict(telegram_chat_id=5, telegram_message_id=6, telegram_file_id='file-proof')
        checker_event(ORDER, 'job_1', 'result_delivery', 1, **valid)
        for bad in [0, -1, True, 1.5, '6', None]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checker_event(ORDER, 'job_1', 'result_delivery', 1,
                              **{**valid, 'telegram_message_id': bad})
        with self.assertRaises(ValueError):
            checker_event(ORDER, 'job_1', 'result_delivery', 1, result_ready=True)

    def test_review_and_not_found_are_separate_from_success(self):
        for outcome in ['review', 'not_found', 'failed']:
            event = checker_event(ORDER, 'job_1', 'check_outcome', 1,
                                  row_number=2, check_type='account', outcome=outcome)
            self.assertEqual(event.rpc_arguments()['p_payload']['outcome'], outcome)

    def test_row_identity_not_search_number(self):
        a = checker_event(ORDER, 'job_1', 'check_outcome', 1, row_number=2, check_type='account', outcome='succeeded')
        b = checker_event(ORDER, 'job_1', 'check_outcome', 1, row_number=3, check_type='account', outcome='succeeded')
        self.assertNotEqual(a.event_key, b.event_key)

    def test_reject_invalid_identity_versions_and_extra_fields(self):
        cases = [('invalid', 'job_1', 1, {}), (ORDER, '', 1, {}), (ORDER, 'job_1', True, {}),
                 (ORDER, 'job_1', 0, {}), (ORDER, 'job_1', 1, {'amount': 100})]
        for order, job, revision, extra in cases:
            with self.subTest(case=(order, job, revision, extra)), self.assertRaises(ValueError):
                checker_event(order, job, 'check_outcome', revision,
                              row_number=2, check_type='account', outcome='succeeded', **extra)


if __name__ == '__main__':
    unittest.main()

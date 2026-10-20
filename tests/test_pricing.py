import unittest
from unittest.mock import Mock, MagicMock
from manager.pricing import PricingSettings, billing_summary, money_minor, normalize_quote
from manager.queue import QueueService
from domain.models import QueueJob
from server.web_api import WebApiHandler


def memory_path():
    path = MagicMock()
    path.is_file.return_value = False
    path.exists.return_value = False
    path.parent.__truediv__.return_value.is_file.return_value = False
    return path


def job(status='pending', completed=0, pricing=None):
    return QueueJob('one', 'demo.xlsx', 0, 'Sheet1', 'smart_hybrid', 'contract', {}, 100,
                    status=status, completed=completed, pricing=pricing)


class PricingTests(unittest.TestCase):
    def test_reject_invalid_money(self):
        for value in ('NaN', 'Infinity', '-1', '0.001', '1000001', True, None, ''):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money_minor(value)
        self.assertEqual(money_minor('0.29'), 29)

    def test_count_all_checked_rows_not_just_successful(self):
        q = normalize_quote({'mode':'customer', 'unit_price':'0.25'})
        b = billing_summary(q, 100, 80, 'active')
        self.assertEqual(b['expected_minor'], 2500)
        self.assertEqual(b['billed_minor'], 2000)
        self.assertEqual(b['partner_minor'], 600)
        self.assertEqual(b['owner_minor'], 1400)
        self.assertEqual(b['checked_rows'], 80)

    def test_fee_fixed_only_when_sheet_complete(self):
        q = normalize_quote({'mode':'sheet', 'unit_price':'20'})
        self.assertEqual(billing_summary(q, 100, 80, 'active')['billed_minor'], 0)
        self.assertEqual(billing_summary(q, 100, 80, 'completed')['billed_minor'], 0)
        self.assertEqual(billing_summary(q, 100, 100, 'completed')['billed_minor'], 2000)
        self.assertEqual(billing_summary(q, 100, 100, 'archived')['billed_minor'], 2000)
        self.assertEqual(billing_summary(q, 100, 80, 'archived')['billed_minor'], 0)
        self.assertEqual(billing_summary(q, 0, 0, 'completed')['billed_minor'], 0)

    def test_split_reconciles_to_cent(self):
        q = normalize_quote({'mode':'customer','unit_price':'0.01'})
        for n in range(1, 100):
            b = billing_summary(q, n, n, 'completed')
            self.assertEqual(b['owner_minor'] + b['partner_minor'], b['billed_minor'])

    def test_future_defaults_do_not_change_snapshot(self):
        settings = PricingSettings(memory_path())
        settings.save({'mode':'customer','customer_price':'0.10','sheet_price':'20'})
        saved = settings.quote()
        settings.save({'mode':'sheet','customer_price':'1','sheet_price':'50'})
        self.assertEqual(saved['unit_minor'], 10)
        self.assertEqual(settings.quote()['unit_minor'], 5000)

    def test_failed_save_preserves_previous_settings(self):
        path = memory_path()
        settings = PricingSettings(path)
        path.write_text.side_effect = OSError('unwritable')
        with self.assertRaises(OSError):
            settings.save({'mode':'sheet','sheet_price':'20'})
        self.assertEqual(settings.get()['sheet_price'], '0.00')

    def test_legacy_jobs_remain_unpriced(self):
        self.assertIsNone(job().pricing)
        self.assertIsNone(billing_summary(None, 100, 100, 'completed'))
        old = job().to_dict()
        old.pop('pricing')
        self.assertIsNone(QueueJob(**old).pricing)

    def test_only_untouched_pending_price_can_change(self):
        queue = QueueService(memory_path())
        queue._save_to_disk = Mock()
        for status, completed in [('active',0), ('completed',100), ('pending',1)]:
            queue._jobs = [job(status, completed)]
            self.assertFalse(queue.update_job_pricing('one', {'mode':'sheet','unit_price':'20'}))
        queue._jobs = [job()]
        self.assertTrue(queue.update_job_pricing('one', {'mode':'customer','unit_price':'0.20'}))
        self.assertEqual(queue._jobs[0].pricing['unit_minor'], 20)

    def test_api_validation_and_conflict(self):
        handler = object.__new__(WebApiHandler)
        handler.queue_service = QueueService(memory_path())
        handler.send_json = Mock()
        handler.handle_pricing('/api/pricing', {'mode':'sheet','sheet_price':'bad'})
        self.assertEqual(handler.send_json.call_args.args[0], 400)
        handler.handle_pricing('/api/queue/pricing', {'job_id':'missing','mode':'sheet','unit_price':'20'})
        self.assertEqual(handler.send_json.call_args.args[0], 409)
        handler.handle_pricing('/api/pricing', {'mode':'customer','customer_price':'0.10'})
        self.assertEqual(handler.send_json.call_args.args[0], 200)

    def test_live_billing_uses_current_counts_not_stale_queue_progress(self):
        import threading
        from types import SimpleNamespace
        handler = object.__new__(WebApiHandler)
        handler.queue_service = QueueService(memory_path())
        handler.queue_service._jobs = [job('active', 0, normalize_quote({'mode':'customer','unit_price':'0.10'}))]
        handler.orchestrator = SimpleNamespace(lock=threading.RLock(), current_job_id='one',
            get_live_status=lambda:{'running':True,'kpis':{'total':100,'completed':80}})
        handler.send_json = Mock()
        handler.handle_live_status()
        body = handler.send_json.call_args.args[1]
        self.assertEqual(body['current_job_id'], 'one')
        self.assertEqual(body['billing']['billed_minor'], 800)


if __name__ == '__main__':
    unittest.main()

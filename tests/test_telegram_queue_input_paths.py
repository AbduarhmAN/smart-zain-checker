"""Queue-start path regression checks; retain all fixtures, no external I/O."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook
from telegram_bot.ui import TelegramUiMixin


class QueueInputPathTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='queue-input-path-retained-')).resolve()
        self.content=b'retained-path-fixture'
        self.digest=hashlib.sha256(self.content).hexdigest()
        self.path=self.root/'.commerce'/'inputs'/(self.digest+'.xlsx')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.path.write_bytes(self.content)
        self.job={'id':'fixture-job','status':'pending','filename':self.path.relative_to(self.root).as_posix(),
                  'sheet_index':0,'mode':'account_only','amount_target':'remaining',
                  'column_mapping':{'lookup_col':'A','amount_col':'B','has_headers':True,
                                    'commerce_order_id':'fixture-order','commerce_input_sha256':self.digest}}
        self.bot=TelegramUiMixin()
        self.bot.project_root=self.root
        self.bot.orchestrator=SimpleNamespace(get_live_status=lambda:{'running':False})
        self.bot.queue_service=Mock()
        self.bot.queue_service.get_jobs.side_effect=lambda:[copy.deepcopy(self.job)]
        self.book=Workbook()
        self.book.active.append(['number','amount'])
        self.book.active.append(['1000000001',0])
        loader=patch('domain.workbook.load_fast_workbook',return_value=self.book)
        self.loader=loader.start();self.addCleanup(loader.stop)
        network=patch('urllib.request.urlopen',side_effect=AssertionError('External I/O forbidden'))
        network.start();self.addCleanup(network.stop)

    def test_frozen_customer_input_passes_queue_start_review(self):
        result=self.bot._prepare_queue_start()
        self.assertEqual(result['jobs'][0]['id'],self.job['id'])
        self.assertEqual(result['versions'][0][0],self.job['id'])
        self.loader.assert_called_once_with(self.path,read_only=True,data_only=True)

    def test_local_workbook_still_passes(self):
        local=self.root/'local.xlsx';local.write_bytes(self.content)
        self.job['filename']=local.name
        self.job['column_mapping'].pop('commerce_order_id')
        self.bot._prepare_queue_start()
        self.loader.assert_called_once_with(local,read_only=True,data_only=True)

    def test_arbitrary_nested_or_external_files_stay_rejected(self):
        for name in ('sub/local.xlsx','../outside.xlsx',str(self.root.parent/'outside.xlsx')):
            with self.subTest(path=name):
                self.job['filename']=name
                self.job['column_mapping'].pop('commerce_order_id',None)
                with self.assertRaisesRegex(ValueError,'مسار ملف الجولة'):
                    self.bot._prepare_queue_start()
        self.loader.assert_not_called()

    def test_commerce_metadata_does_not_allow_another_file(self):
        for name in ('local.xlsx','.commerce/other.xlsx','../outside.xlsx'):
            with self.subTest(path=name):
                self.job['filename']=name
                with self.assertRaisesRegex(ValueError,'مسار ملف الجولة'):
                    self.bot._prepare_queue_start()
        self.loader.assert_not_called()

    def test_invalid_or_traversing_digest_is_rejected(self):
        for digest in (None,'../outside','A'*64,'a'*63,123):
            with self.subTest(digest=digest):
                self.job['column_mapping']['commerce_input_sha256']=digest
                with self.assertRaisesRegex(ValueError,'بصمة ملف الطلب'):
                    self.bot._prepare_queue_start()
        self.loader.assert_not_called()

    def test_changed_customer_input_is_rejected_before_opening_excel(self):
        self.path.write_bytes(b'changed-fixture')
        with self.assertRaisesRegex(ValueError,'تغير ملف الطلب'):
            self.bot._prepare_queue_start()
        self.loader.assert_not_called()

    def test_file_changed_during_review_is_rejected(self):
        with patch('telegram_bot.sheet_workflow.file_version',side_effect=[(1,2),(2,3)]):
            with self.assertRaisesRegex(ValueError,'تغير الملف أثناء التحقق'):
                self.bot._prepare_queue_start()

    def test_unknown_sheet_is_rejected(self):
        self.job['sheet_index']=2
        with self.assertRaisesRegex(ValueError,'ورقة العمل غير موجودة'):
            self.bot._prepare_queue_start()


if __name__=='__main__':unittest.main()

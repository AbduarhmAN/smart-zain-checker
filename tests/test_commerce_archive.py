"""Archive durability/privacy tests with retained fixtures and synthetic I/O."""
import concurrent.futures
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from uuid import uuid4
from telegram_bot.commerce.archive import ArchiveDispatcher,archive_caption
from telegram_bot.commerce.archive_config import ArchiveConfig
from telegram_bot.commerce.archive_store import ArchiveStore
from telegram_bot.commerce.archive_transport import ArchiveTransport,ArchiveAmbiguous,ArchiveRejected,document_receipt,UPLOAD_LIMIT
from telegram_bot.commerce.archive_ui import ArchiveUiMixin
from telegram_bot.commerce.bridge import file_sha256
from telegram_bot.commerce.client import CommerceUnavailable


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='archive-retained-'));self.order=str(uuid4())
        self.config=ArchiveConfig(self.root)
        me={'id':77,'is_bot':True};chat={'id':-1001234567890,'type':'channel','title':'fixture'}
        self.config.bind(lambda method,data:{'getMe':me,'getChat':chat,'getChatMember':{'status':'administrator','can_post_messages':True}}[method],chat['id'],source_reference='fixture')
        self.config.configure_policy(original_timing='after_completion',protection='plaintext_sha256',original_days=15,result_days=15,reference='fixture')
        self.config.enable(verification_reference='synthetic-offline-proof')
        self.original=self.root/'.commerce'/'inputs'/'pending.xlsx';self.original.parent.mkdir(parents=True)
        self.original.write_bytes(b'PK\x03\x04fixture-original');digest=file_sha256(self.original)
        target=self.original.with_name(digest+'.xlsx');target.write_bytes(self.original.read_bytes());self.original=target
        self.result=self.root/'.commerce'/('result_'+self.order+'.xlsx');self.result.write_bytes(b'PK\x03\x04fixture-result')
        self.plan={'state':'ready','order_id':self.order,'telegram_user_id':123,'requested_checks':3,'valid_checks':2,
            'technical_failures':1,'charged_minor':6000,'refunded_minor':3000,'currency':'EGP','original_sha256':digest,
            'result_sha256':file_sha256(self.result),'original_file_id':'original-id','result_file_id':'result-id'}
        self.client=Mock();self.client.rpc.side_effect=lambda name,args:self.plan if name=='archive_plan' else {'state':'confirmed'}
        self.transport=Mock()
        self.transport.send.side_effect=lambda config,**kw:{'chat_id':config['chat_id'],'message_id':80,
            'file_id':kw['file_id'] or 'new-id','file_unique_id':'unique-id','file_size':Path(kw['path']).stat().st_size}
        self.worker=ArchiveDispatcher(self.root,client=self.client,transport=self.transport);self.worker.enqueue(self.order)

    def due(self,kind):
        with self.worker.store.connect() as db:db.execute('update archive_jobs set next_at=0 where kind=?',(kind,))

    def test_original_and_result_reused_once_and_replays_do_not_send(self):
        self.assertTrue(self.worker.run_once());self.assertTrue(self.worker.run_once());self.assertFalse(self.worker.run_once())
        self.assertEqual(self.transport.send.call_count,2)
        self.assertEqual({c.kwargs['file_id'] for c in self.transport.send.call_args_list},{'original-id','result-id'})
        self.worker.enqueue(self.order);self.assertFalse(self.worker.run_once())
        self.assertEqual(self.worker.store.counts(),{'confirmed':2})

    def test_unknown_acknowledgement_is_never_resent(self):
        self.transport.send.side_effect=ArchiveAmbiguous('synthetic-lost-ack')
        self.worker.run_once();self.worker.run_once();self.assertFalse(self.worker.run_once())
        restarted=ArchiveDispatcher(self.root,client=self.client,transport=self.transport)
        restarted.recover();self.assertFalse(restarted.run_once())
        self.assertEqual(self.transport.send.call_count,2);self.assertEqual(restarted.store.counts(),{'ambiguous':2})
        with self.assertRaises(ValueError):restarted.store.retry_blocked(self.order,'original')

    def test_index_ack_loss_uses_persisted_receipt_not_another_send(self):
        calls=[]
        def rpc(name,args):
            calls.append(name)
            if name=='archive_plan':return self.plan
            if calls.count('confirm_archive_file')==1:raise CommerceUnavailable('Commerce RPC response unavailable')
            return {'state':'confirmed'}
        self.client.rpc.side_effect=rpc;self.worker.run_once();self.due('original')
        restarted=ArchiveDispatcher(self.root,client=self.client,transport=self.transport)
        restarted.run_once()
        self.assertEqual(self.transport.send.call_count,1);self.assertEqual(restarted.store.lookup(self.order,'original')['state'],'confirmed')

    def test_settlement_wait_has_no_send(self):
        self.plan['state']='awaiting_settlement';self.worker.run_once();self.worker.run_once()
        self.transport.send.assert_not_called();self.assertEqual(self.worker.store.counts(),{'pending':2})

    def test_changed_original_is_blocked(self):
        self.original.write_bytes(b'changed');self.worker.run_once()
        self.transport.send.assert_not_called();self.assertEqual(self.worker.store.lookup(self.order,'original')['state'],'blocked')

    def test_explicit_rate_rejection_retries_but_forbidden_blocks(self):
        self.transport.send.side_effect=ArchiveRejected(429,7);self.worker.run_once()
        self.assertEqual(self.worker.store.lookup(self.order,'original')['state'],'pending')
        self.transport.send.side_effect=ArchiveRejected(403);self.worker.run_once()
        self.assertEqual(self.worker.store.lookup(self.order,'result')['state'],'blocked')
        self.worker.store.retry_blocked(self.order,'result')
        self.assertEqual(self.worker.store.lookup(self.order,'result')['state'],'pending')

    def test_failed_receipt_persistence_marks_unknown_send(self):
        with patch.object(self.worker.store,'receipt',side_effect=OSError('fixture-disk-failure')):self.worker.run_once()
        self.assertEqual(self.worker.store.lookup(self.order,'original')['state'],'ambiguous')

    def test_claim_and_index_in_progress_are_exclusive(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            claims=list(executor.map(lambda _:self.worker.store.claim(),range(16)))
        self.assertEqual(sum(c is not None for c in claims),2)
        job=next(c for c in claims if c is not None);self.worker.store.receipt(job,{'fixture':True})
        self.assertIsNone(self.worker.store.claim())

    def test_expired_send_is_ambiguous_but_expired_preparation_recovers(self):
        first=self.worker.store.claim(now=time.time()-300,lease_seconds=1)
        with patch('telegram_bot.commerce.archive_store.time.time',return_value=time.time()-300):
            self.worker.store.sending(first)
        next_job=self.worker.store.claim(now=time.time()-298,lease_seconds=1)
        self.assertEqual(self.worker.store.lookup(self.order,first['kind'])['state'],'ambiguous')
        self.assertIsNotNone(self.worker.store.claim())
        self.assertEqual(self.worker.store.lookup(self.order,next_job['kind'])['state'],'leased')

    def test_reconciliation_requires_download_sha_before_receipt(self):
        self.transport.send.side_effect=ArchiveAmbiguous('lost');self.worker.run_once()
        message={'chat':{'id':self.config.load()['chat_id']},'message_id':98,
            'document':{'file_id':'recovered','file_unique_id':'unique','file_size':self.original.stat().st_size}}
        self.transport.verify_download.side_effect=ValueError('hash mismatch')
        with self.assertRaises(ValueError):self.worker.reconcile(self.order,'original',message)
        self.assertEqual(self.worker.store.lookup(self.order,'original')['state'],'ambiguous')
        self.transport.verify_download.side_effect=None;self.worker.reconcile(self.order,'original',message)
        self.worker.run_once();self.assertEqual(self.transport.send.call_count,1)
        self.assertEqual(self.worker.store.lookup(self.order,'original')['state'],'confirmed')

    def test_caption_explains_exact_facts_without_customer_name(self):
        caption=archive_caption(self.plan,'result',self.plan['result_sha256'])
        self.assertIn('60.00',caption);self.assertIn('30.00',caption);self.assertIn(self.order,caption);self.assertLessEqual(len(caption),1024)

    def test_transport_reuses_file_id_without_multipart(self):
        api=ArchiveTransport('synthetic-token',session=Mock());api.verify=Mock()
        api.api=Mock(return_value={'chat':{'id':-1001234567890},'message_id':10,
            'document':{'file_id':'result-id','file_unique_id':'u','file_size':self.result.stat().st_size}})
        api.send(self.config.load(),file_id='result-id',path=self.result,sha256=file_sha256(self.result),caption='fixture',filename='result.xlsx')
        self.assertEqual(api.api.call_args.args[1]['document'],'result-id');self.assertNotIn('files',api.api.call_args.kwargs)

    def test_upload_limit_rejects_binary_before_api(self):
        huge=self.root/'synthetic-large.xlsx'
        with huge.open('xb') as stream:stream.seek(UPLOAD_LIMIT);stream.write(b'X')
        api=ArchiveTransport('synthetic-token',session=Mock());api.verify=Mock();api.api=Mock()
        with self.assertRaises(ValueError):api.send(self.config.load(),file_id=None,path=huge,sha256=file_sha256(huge),caption='fixture',filename='large.xlsx')
        api.api.assert_not_called()

    def test_download_path_and_hash_are_checked(self):
        api=ArchiveTransport('synthetic-token',session=Mock());api.api=Mock(return_value={'file_size':1,'file_path':'../bad'})
        with self.assertRaises(ValueError):api.verify_download('file','a'*64)
        api.session.get.assert_not_called()

    def test_ambiguous_response_has_no_retry_or_sensitive_error(self):
        session=Mock();session.post.side_effect=TimeoutError('secret-url-with-token')
        api=ArchiveTransport('secret-token',session=session)
        with self.assertRaises(ArchiveAmbiguous) as caught:api.api('sendDocument',{'chat_id':1,'document':'file'})
        self.assertNotIn('secret',str(caught.exception));self.assertEqual(session.post.call_count,1)

    def test_foreign_channel_receipt_and_missing_unique_identity_fail(self):
        for message in [{'chat':{'id':123},'message_id':1,'document':{}},
                        {'chat':{'id':-1001234567890},'message_id':1,'document':{'file_id':'file','file_size':1}}]:
            with self.assertRaises(ArchiveAmbiguous):document_receipt(message,-1001234567890)

    def test_customer_cannot_forge_original_retrieval(self):
        class Ui(ArchiveUiMixin):pass
        ui=Ui();ui._ui_lock=__import__('threading').RLock();ui.current_authorized_ids={7}
        ui._commerce=Mock();ui._commerce_notice=Mock()
        ui._archive_callback('ar:original:'+self.order,123,10)
        ui._commerce.assert_not_called();ui._commerce_notice.assert_called_once()

    def test_owned_result_retrieval_validates_document_acknowledgement(self):
        class Ui(ArchiveUiMixin):pass
        ui=Ui();ui._ui_lock=__import__('threading').RLock();ui.current_authorized_ids={7}
        client=Mock();api=Mock();ui._commerce=Mock(return_value=client);ui._archive_transport=Mock(return_value=api);ui._commerce_notice=Mock()
        client.rpc.return_value={'files':[{'kind':'result','telegram_file_id':'result-id','sha256':'a'*64,'file_size':10}]}
        api.api.return_value={'chat':{'id':123},'message_id':90,'document':{'file_id':'result-id','file_unique_id':'u','file_size':10}}
        ui._archive_callback('ar:result:'+self.order,123,10)
        self.assertEqual(client.rpc.call_args.args[0],'customer_archive_index')
        self.assertEqual(client.rpc.call_args.args[1]['p_telegram_user_id'],123)
        self.assertEqual(ui._commerce_notice.call_args.args[1],'استرجاع الملف')
        api.api.return_value={'unknown':'ack'}
        ui._archive_callback('ar:result:'+self.order,123,10)
        self.assertEqual(ui._commerce_notice.call_args.args[1],'تأكد من المحادثة')

    def test_checker_finalization_preserves_frozen_original(self):
        from manager.orchestrator import Orchestrator
        from types import SimpleNamespace
        service=Mock();job=SimpleNamespace(column_mapping={'commerce_order_id':self.order})
        service.claim_next_pending_job.return_value=None
        service.get_job.return_value=job
        service.get_jobs.return_value=[{'id':'fixture-job','status':'completed','result_file':self.result.name,
            'column_mapping':{'commerce_order_id':self.order,'telegram_chat_id':123},'total_records':1,'completed':1}]
        obj=Orchestrator(Mock(),service,self.root);obj.current_job_id='fixture-job'
        obj.source_workbook_path=self.original;obj.workbook_name=self.original.name;obj.workbook_stem=self.original.stem
        obj.result_path=self.result;obj.all_completed_records={2:{'row':2,'status':'not_found'}}
        obj.customers=[{'fixture':True}];obj.is_finalizing=True
        before=file_sha256(self.original)
        with patch('zain_checker.executive_reporter.export_executive_workbook'),\
             patch('zain_checker.sheet_updater.update_source_workbook_after_job') as updater,\
             patch('manager.orchestrator.EVENT_BUS.publish'):
            obj._finalize_job()
        updater.assert_not_called();self.assertEqual(file_sha256(self.original),before)


if __name__=='__main__':unittest.main()

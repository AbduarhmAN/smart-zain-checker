import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from services.engine import EngineApplication


class EngineBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name)
        (self.root/'.commerce').mkdir();sqlite3.connect(self.root/'.commerce/outbox.sqlite').close()
        (self.root/'config_workers.json').write_text(json.dumps({'workers':[{'worker_id':'fixture','use_proxy':False,'task_scope':'account','use_direct_api':True}]}))
        self.app=EngineApplication(self.root)

    def add(self):
        return self.app.add_job({'arguments':{'file_path':self.root/'synthetic.xlsx','sheet_index':0,'sheet_name':'Data','total_records':2}})

    def test_unfunded_customer_dispatch_rejected_before_file_load_or_worker_start(self):
        with patch('domain.workbook.load_fast_workbook',side_effect=AssertionError('Must authorize first')),patch.object(self.app.supervisor,'start_all') as start:
            with self.assertRaises(PermissionError):
                self.app.orchestrator.start_session_from_config(self.root/'synthetic.xlsx',0,{'commerce_order_id':'unfunded'},job_id='missing')
        start.assert_not_called()

    def test_execution_uses_existing_funded_consent_gate_and_preserves_owner(self):
        job=self.add();self.app.continuous=Mock();self.app.continuous.funded.return_value=False
        self.assertFalse(self.app.authorize_execution(job['id']))
        sent=self.app.continuous.funded.call_args.args[0]
        self.assertEqual(sent['id'],job['id']);self.assertEqual(sent['total_records'],2)

    def test_owned_stop_preserves_reference_and_cannot_stop_another_owner(self):
        job=self.add();native=self.app.queue.get_job(job['id']);native.column_mapping['telegram_chat_id']=21
        with self.assertRaises(ValueError):self.app.customer_stop({'job_id':job['id'],'actor':22,'reference':'stop-22'})
        self.assertNotIn('customer_stop',native.column_mapping)
        self.assertTrue(self.app.customer_stop({'job_id':job['id'],'actor':21,'reference':'stop-21'}))
        self.assertIn('customer_stop',native.column_mapping)
        self.assertIn('queue.request_customer_stop',self.app.retry_safe)

    def test_remove_rechecks_identity_and_active_state_in_engine(self):
        job=self.add()
        self.assertFalse(self.app.remove_job({'job_id':job['id'],'expected':{'filename':'other.xlsx'}}))
        native=self.app.queue.get_job(job['id']);native.status='active'
        self.assertFalse(self.app.remove_job({'job_id':job['id']}))
        native.status='pending'
        self.assertTrue(self.app.remove_job({'job_id':job['id'],'expected':{'filename':'synthetic.xlsx','sheet_index':0}}))

    def test_stale_session_and_unconfined_paths_fail_closed(self):
        with self.assertRaises(ValueError):self.app.action({'action':'resume_session','expected_session':'previous'})
        with self.assertRaises(ValueError):self.app.add_job({'arguments':{'file_path':self.root.parent/'outside.xlsx'}})

    def test_mutating_snapshot_client_cannot_change_authoritative_state(self):
        snapshot=self.app.get_snapshot({});snapshot['state']['running']=True
        self.assertFalse(self.app.get_snapshot({})['state']['running'])
        self.assertFalse(self.app.orchestrator.is_running)

    def test_file_outage_before_claim_keeps_job_pending(self):
        from services.ipc import ServiceUnavailable
        job=self.add()
        with self.assertRaises(ServiceUnavailable):self.app.orchestrator.start_queue(job['id'])
        self.assertEqual(self.app.queue.get_job(job['id']).status,'pending')

    def test_file_outage_after_claim_defers_exact_job_without_resetting_progress(self):
        from services.ipc import ServiceUnavailable
        job=self.add();native=self.app.queue.get_job(job['id']);native.completed=1;native.remaining=1
        self.app.files_available=True
        with patch.object(self.app.orchestrator,'start_session_from_config',side_effect=ServiceUnavailable('files down')):
            with self.assertRaises(ServiceUnavailable):self.app.orchestrator.start_queue(job['id'])
        self.assertEqual(native.status,'pending');self.assertEqual(native.completed,1);self.assertEqual(native.remaining,1)
        self.assertEqual(self.app.orchestrator.waiting_dependency,'files')

    def test_stop_is_recorded_while_initialization_holds_orchestrator_lock(self):
        job=self.add();self.app.queue.get_job(job['id']).column_mapping['telegram_chat_id']=21
        release=threading.Event();held=threading.Event();done=threading.Event();outcome=[]
        def initialize():
            with self.app.orchestrator.lock:held.set();release.wait(5)
        def stop():
            outcome.append(self.app.customer_stop({'job_id':job['id'],'actor':21,'reference':'during-load'}));done.set()
        initializing=threading.Thread(target=initialize);initializing.start();self.assertTrue(held.wait(2))
        stopping=threading.Thread(target=stop);stopping.start()
        try:
            self.assertTrue(done.wait(2),'Stop must not wait for the execution lock')
            self.assertEqual(outcome,[True]);self.assertTrue(self.app.apply_stop({'job_id':job['id']}))
        finally:release.set();initializing.join(3);stopping.join(3)

    def test_export_dependency_outage_keeps_terminal_rows_and_job_recoverable(self):
        from services.ipc import ServiceUnavailable
        job=self.add();native=self.app.queue.get_job(job['id']);native.status='active';native.completed=2;native.remaining=0
        engine=self.app.orchestrator;engine.current_job_id=job['id'];engine.is_finalizing=True
        engine.all_completed_records={0:{'row':2,'status':'match','expected_sar':12.34,'live_sar':12.34}}
        with patch('zain_checker.executive_reporter.export_executive_workbook',side_effect=ServiceUnavailable('files down')):
            engine._finalize_job()
        self.assertEqual(native.status,'pending');self.assertEqual(native.completed,2)
        self.assertFalse(engine.result_ready);self.assertFalse(engine.is_finalizing)
        self.assertEqual(engine.all_completed_records[0]['row'],2)


if __name__=='__main__':unittest.main()

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

SPEC=importlib.util.spec_from_file_location('telegram_maintenance',Path(__file__).resolve().parents[1]/'maintenance/telegram_maintenance.py')
m=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        (self.root/'config.json').write_text(json.dumps({'token':'test-only','bot_id':1,'initial_offset':10,'normal_cursor':str(self.root/'cursor.json')}))
        (self.root/'announcement.txt').write_text('**عنوان**\nاختبار & < >',encoding='utf-8')
        (self.root/'audience.txt').write_text('100\n200\n')
        self.api=Mock()
        self.r=m.Responder(self.root,self.api)
        self.r.stop=Mock()
        self.r.stop.wait.return_value=False
    def tearDown(self):
        self.r.db.close()
        self.temp.cleanup()
    def test_html_preserves_exact_plain_text(self):
        self.assertEqual(m.render('**عنوان**\nاختبار & < >'),'<b>عنوان</b>\nاختبار &amp; &lt; &gt;')
    def test_durable_update_dedup_and_cursor(self):
        batch=[{'update_id':11,'message':{'chat':{'id':300},'from':{'id':300},'document':{'file_id':'ignored'}}}]
        self.r.capture(batch);self.r.capture(batch)
        self.assertEqual(self.r.db.execute("select count(*) from deliveries where kind='reply'").fetchone()[0],1)
        self.assertEqual(json.loads((self.root/'cursor.json').read_text())['update_id'],11)
        self.assertNotIn('ignored',str(self.r.db.execute('select payload from deliveries').fetchall()))
    def test_callback_and_topic_route(self):
        self.r.capture([{'update_id':12,'callback_query':{'id':'abc','message':{'chat':{'id':-400},'message_thread_id':8}}}])
        kinds=dict(self.r.db.execute('select kind,count(*) from deliveries group by kind'))
        self.assertEqual(kinds,{'broadcast':2,'callback':1,'reply':1})
        self.api.call.return_value={'ok':True,'result':True}
        self.r.deliver_one()
        self.assertEqual(self.api.call.call_args.args[0],'answerCallbackQuery')
    def test_no_bot_message_loop(self):
        self.assertEqual(m.target({'message':{'chat':{'id':1},'from':{'is_bot':True}}}),{})
    def test_429_is_retryable_but_network_is_uncertain(self):
        self.api.call.return_value={'ok':False,'error_code':429,'parameters':{'retry_after':2}}
        self.r.deliver_one()
        self.assertEqual(self.r.db.execute('select status from deliveries order by rowid limit 1').fetchone()[0],'pending')
        self.api.call.return_value={'ok':False,'error_code':'transport'}
        self.r.deliver_one()
        self.assertEqual(self.r.db.execute('select status from deliveries order by rowid limit 1').fetchone()[0],'uncertain')
        self.api.call.return_value={'ok':False,'error_code':403}
        self.r.deliver_one()
        self.assertEqual(self.r.db.execute('select status from deliveries order by rowid desc limit 1').fetchone()[0],'failed')

    def test_maintenance_reply_exposes_only_official_support_url(self):
        self.api.call.return_value={'ok':True,'result':{'message_id':1}}
        self.r.deliver_one()
        payload=self.api.call.call_args.args[1]
        self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['url'],'https://t.me/zainchecker?direct')
    def test_restart_never_repeats_sent_or_ambiguous_broadcast(self):
        self.r.db.execute("update deliveries set status='sent' where rowid=1")
        self.r.db.execute("update deliveries set status='sending' where rowid=2")
        self.r.db.commit();self.r.db.close()
        self.r=m.Responder(self.root,self.api)
        self.assertEqual(sorted(r[0] for r in self.r.db.execute('select status from deliveries')),['sent','uncertain'])

    def test_sender_does_not_wait_for_long_poll(self):
        entered=threading.Event();release=threading.Event();delivered=threading.Event()
        self.r.stop=threading.Event()
        self.r.worker_failed=False
        self.r.capture([{'update_id':11,'message':{'chat':{'id':300},'from':{'id':300},'date':time.time()}}])
        calls=[]
        def api(method,payload,**kwargs):
            if method=='getChatMember':return {'ok':True,'result':{'user':{'id':payload['user_id']},'status':'member'}}
            if method=='getUpdates':
                entered.set();release.wait(3)
                return {'ok':True,'result':[]}
            calls.append(payload)
            if len(calls)==3:delivered.set()
            return {'ok':True,'result':{'message_id':len(calls)}}
        self.api.call.side_effect=api
        poller=threading.Thread(target=self.r.poll_loop)
        sender=threading.Thread(target=self.r.send_loop)
        poller.start()
        try:
            self.assertTrue(entered.wait(1))
            sender.start()
            self.assertTrue(delivered.wait(1),'Pending replies were blocked by the long poll')
            self.assertFalse(release.is_set())
            self.assertEqual(len(calls),3)
            self.assertTrue(all(not any(str(k).startswith('_') for k in p) for p in calls))
            self.assertIsNotNone(self.r.last_reply_latency_seconds)
        finally:
            self.r.stop.set();release.set();self.r.wake.set()
            poller.join(4)
            if sender.ident:sender.join(4)

if __name__=='__main__':unittest.main()

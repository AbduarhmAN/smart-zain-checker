"""Administrative boundaries using isolated local HTTP servers and retained files."""
from email.message import Message
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from http.server import ThreadingHTTPServer
from server.access import local_request,workspace_file
from server.bridge_authority import capability,provision_extension
from server.extension_bridge import ExtensionBridgeHandler
from server.web_api import WebApiHandler
from server.gateway import ServerGateway
from telegram_bot.bot import TelegramBotRunner
from telegram_bot import views
from telegram_bot.ui import TelegramUiMixin


class AccessBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='access-boundaries-retained-')).resolve()

    def handler(self,host='127.0.0.1:5050',peer='127.0.0.1',origin=None,site=None):
        headers=Message();headers['Host']=host
        if origin is not None:headers['Origin']=origin
        if site is not None:headers['Sec-Fetch-Site']=site
        return SimpleNamespace(headers=headers,client_address=(peer,123),server=SimpleNamespace(server_port=5050))

    def test_same_origin_local_requests_work_for_ipv4_and_ipv6(self):
        self.assertTrue(local_request(self.handler(origin='http://127.0.0.1:5050',site='same-origin')))
        self.assertTrue(local_request(self.handler(host='[::1]:5050',peer='::1',origin='http://[::1]:5050')))

    def test_cross_origin_remote_peer_and_rebinding_host_are_rejected(self):
        for handler in [self.handler(origin='https://external.example'),self.handler(origin='null'),
            self.handler(host='external.example:5050'),self.handler(peer='192.0.2.1'),
            self.handler(site='cross-site'),self.handler(origin='http://127.0.0.1:5050/path')]:
            self.assertFalse(local_request(handler))

    def test_duplicate_host_is_rejected(self):
        handler=self.handler();handler.headers['Host']='external.example:5050'
        self.assertFalse(local_request(handler))

    def test_workspace_file_accepts_public_files_and_rejects_escape_and_private_paths(self):
        self.assertEqual(workspace_file(self.root,'sheet.xlsx',suffixes={'.xlsx'},flat=True),self.root/'sheet.xlsx')
        for value in ['../other.xlsx','sub/../../other.xlsx','C:/other.xlsx','\\\\server\\share.xlsx',
            '.env','.commerce/result.xlsx','sub/sheet.xlsx','sheet.xlsx:stream','sheet.txt','x\x00.xlsx']:
            with self.subTest(value=value),self.assertRaises(ValueError):workspace_file(self.root,value,suffixes={'.xlsx'},flat=True)

    def test_absolute_workbook_path_requires_workspace_parent(self):
        handler=WebApiHandler.__new__(WebApiHandler);handler.project_root=self.root;handler.send_json=Mock()
        self.assertEqual(handler._workbook_path(str(self.root/'ok.xlsx')),self.root/'ok.xlsx')
        with self.assertRaises(ValueError):handler._workbook_path(str(self.root.parent/'outside.xlsx'))
        self.assertEqual(handler.send_json.call_args.args[0],403)

    def test_private_extension_copy_retains_source_and_reuses_authority(self):
        source=self.root/'source';source.mkdir();manifest={'manifest_version':3,'background':{'service_worker':'service-worker.js'}}
        (source/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
        (source/'bridge-config.js').write_text('source config',encoding='utf-8')
        first=provision_extension(self.root,source,worker_id='worker_3')
        second=provision_extension(self.root,source,worker_id='worker_4')
        self.assertNotEqual(first,second);self.assertEqual((source/'bridge-config.js').read_text(),'source config')
        token=capability(self.root);self.assertEqual(token,capability(self.root))
        self.assertIn(token,(first/'bridge-config.js').read_text());self.assertIn('worker_3',(first/'bridge-config.js').read_text())
        self.assertNotIn(token,(source/'manifest.json').read_text());self.assertTrue(first.is_relative_to(self.root/'.bridge-runtime'))

    def test_corrupt_authority_is_retained_and_never_replaced_silently(self):
        folder=self.root/'.bridge-runtime';folder.mkdir();record=folder/'authority.json';record.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):capability(self.root)
        self.assertEqual(record.read_text(),'{}')

    def test_gateway_refuses_public_binding(self):
        with self.assertRaises(ValueError):ServerGateway(Mock(),Mock(),self.root,self.root,bind_host='0.0.0.0')

    def test_worker_launch_uses_managed_copy_without_starting_a_browser(self):
        from workers import launcher
        source=self.root/'source';source.mkdir()
        (source/'manifest.json').write_text('{"manifest_version":3}',encoding='utf-8')
        with patch.object(launcher,'PROJECT_ROOT',self.root),patch.object(launcher,'find_chrome_executable',return_value=Path('fixture-chrome.exe')),\
             patch.object(launcher.subprocess,'Popen',return_value=SimpleNamespace(pid=123)) as spawn:
            self.assertEqual(launcher.launch_worker_chrome('worker_3',self.root/'profile',source),123)
        args=spawn.call_args.args[0];loaded=Path(next(v.split('=',1)[1] for v in args if v.startswith('--load-extension=')))
        self.assertNotEqual(loaded,source);self.assertTrue(loaded.is_relative_to(self.root/'.bridge-runtime'))
        self.assertIn('worker_3',(loaded/'bridge-config.js').read_text());self.assertEqual((self.root/'profile'/'.pid').read_text(),'123')

    def bot(self):
        bot=TelegramBotRunner.__new__(TelegramBotRunner);bot._authorized_override=False
        bot.authorized_ids={9};bot.authorized_id=9;bot._ui_lock=threading.RLock();bot._user_modes={'9':'admin'}
        bot.project_root=self.root;bot._api_call=Mock(return_value={'ok':True});bot._commerce_client=Mock()
        return bot

    def test_removed_administrator_is_not_restored_from_startup_cache(self):
        bot=self.bot()
        with patch('telegram_bot.bot.telegram_settings',return_value={'authorized_ids':[10],'authorized_id':10}):
            self.assertEqual(bot.current_authorized_ids,{10})
            bot._handle_callback({'id':'fixture','from':{'id':9},'message':{'chat':{'id':9},'message_id':2},'data':'zc:oa:list:0'})
            bot._commerce_client.rpc.assert_not_called()
            self.assertTrue(bot._api_call.call_args.args[1]['show_alert'])

    def test_cached_admin_mode_does_not_expose_admin_buttons_after_revocation(self):
        bot=self.bot()
        with patch('telegram_bot.bot.telegram_settings',return_value={'authorized_ids':[10],'authorized_id':10}):
            TelegramUiMixin._show_panel(bot,9,views.Panel('test',[[views.button('admin','oa:list:0'),views.button('home','home')]]))
        rows=bot._api_call.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual([button['callback_data'] for row in rows for button in row],['zc:home'])


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(tempfile.mkdtemp(prefix='http-boundaries-retained-')).resolve();static=self.root/'static';static.mkdir()
        (static/'ok.txt').write_text('public fixture',encoding='utf-8')
        (self.root/'private.txt').write_text('private fixture',encoding='utf-8')
        parent=self
        class Dashboard(WebApiHandler):
            project_root=parent.root
            web_ui_dir=parent.root
            orchestrator=Mock()
        class Bridge(ExtensionBridgeHandler):
            bridge_token='a'*64
            orchestrator=Mock(is_running=False)
        Bridge.orchestrator.lease_next_task_for_worker.return_value=None
        self.bridge_handler=Bridge;self.dashboard_handler=Dashboard;self.servers=[]
        for cls in (Dashboard,Bridge):
            server=ThreadingHTTPServer(('127.0.0.1',0),cls);server.daemon_threads=True
            thread=threading.Thread(target=lambda s=server:s.serve_forever(poll_interval=0.01),daemon=True);thread.start()
            self.servers.append(server);self.addCleanup(server.server_close);self.addCleanup(server.shutdown)

    def request(self,server,path,method='GET',headers=None,body=None):
        conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2);self.addCleanup(conn.close)
        conn.request(method,path,body=body,headers=headers or {});response=conn.getresponse()
        return response.status,response.read(),dict(response.getheaders())

    def test_real_dashboard_serves_local_assets_and_rejects_remote_origin(self):
        server=self.servers[0];status,body,_=self.request(server,'/static/ok.txt')
        self.assertEqual((status,body),(200,b'public fixture'))
        status,body,_=self.request(server,'/static/ok.txt',headers={'Origin':'https://external.example'})
        self.assertEqual(status,403);self.assertNotIn(b'public fixture',body)

    def test_real_dashboard_rejects_encoded_traversal(self):
        status,body,_=self.request(self.servers[0],'/static/%2e%2e/private.txt')
        self.assertEqual(status,403);self.assertNotIn(b'private fixture',body)

    def test_dashboard_cross_origin_post_cannot_pause_and_same_origin_can(self):
        server=self.servers[0];headers={'Content-Type':'application/json','Origin':'https://external.example'}
        status,_,_=self.request(server,'/api/pause-session','POST',headers,'{}')
        self.assertEqual(status,403);self.dashboard_handler.orchestrator.pause_session.assert_not_called()
        headers['Origin']='http://127.0.0.1:'+str(server.server_port)
        status,_,_=self.request(server,'/api/pause-session','POST',headers,'{}')
        self.assertEqual(status,200);self.dashboard_handler.orchestrator.pause_session.assert_called_once()

    def test_bridge_unauthorized_request_never_leases_work(self):
        status,_,_=self.request(self.servers[1],'/task?worker=worker_3')
        self.assertEqual(status,401);self.bridge_handler.orchestrator.lease_next_task_for_worker.assert_not_called()

    def test_bridge_valid_private_capability_and_extension_origin_accepts_work(self):
        status,body,headers=self.request(self.servers[1],'/task?worker=worker_3',headers={
            'X-Zain-Bridge-Token':'a'*64,'Origin':'chrome-extension://'+'b'*32})
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['status'],'wait')
        self.bridge_handler.orchestrator.lease_next_task_for_worker.assert_called_once_with('worker_3')
        self.assertEqual(headers['Access-Control-Allow-Origin'],'chrome-extension://'+'b'*32)

    def test_bridge_rejects_web_origin_even_with_key(self):
        status,_,headers=self.request(self.servers[1],'/task',headers={'X-Zain-Bridge-Token':'a'*64,'Origin':'https://external.example'})
        self.assertEqual(status,401);self.assertNotIn('Access-Control-Allow-Origin',headers)
        self.bridge_handler.orchestrator.lease_next_task_for_worker.assert_not_called()

    def test_bridge_non_ascii_key_is_rejected_without_crashing_connection(self):
        status,_,_=self.request(self.servers[1],'/task',headers={'X-Zain-Bridge-Token':'é'*64})
        self.assertEqual(status,401);self.bridge_handler.orchestrator.lease_next_task_for_worker.assert_not_called()

    def test_bridge_rejects_non_object_json_without_leasing_work(self):
        status,_,_=self.request(self.servers[1],'/task','POST',{'X-Zain-Bridge-Token':'a'*64,'Content-Type':'application/json'},'[]')
        self.assertEqual(status,400);self.bridge_handler.orchestrator.lease_next_task_for_worker.assert_not_called()


if __name__=='__main__':unittest.main()

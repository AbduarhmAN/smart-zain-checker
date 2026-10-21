"""Opt-in, isolated subprocess fixture. This is not a real Chromium benchmark."""
from pathlib import Path
import os
import threading
import time


class TestBrowserTransport:
    def __init__(self, root):
        root = Path(root).resolve()
        repository = Path(__file__).resolve().parent.parent
        marker = root / '.services' / 'TEST_FIXTURE'
        if root == repository or not marker.is_file() or marker.read_text(encoding='utf-8').strip() != 'smart-zain-isolated-tests':
            raise RuntimeError('Browser test transport requires an explicitly marked isolated root')
        self.root=root
        self.stopping = threading.Event()

    def query_service_stealth(self, number, *, cancel_event, **kwargs):
        delay = 2.0 if number == '2999999999' else .01
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self.stopping.is_set() or cancel_event.wait(.01):
                return 'network_error', None, 'synthetic cancellation'
        return 'ok', 12.34, 'synthetic browser fixture; no Chromium or external request'

    def pending_verifications(self):
        return []

    def verification_image(self, challenge_id):
        return None

    def submit_verification(self, challenge_id, answer):
        return False

    def cancel_pending_verifications(self):
        pass

    def stop_service_browsers(self):
        self.stopping.set()


class OfflineChromiumTransport(TestBrowserTransport):
    """Existing installed Chromium, local HTML only; no service endpoint queried."""
    def query_service_stealth(self,number,*,cancel_event,**kwargs):
        from playwright.sync_api import sync_playwright
        executable=Path(os.environ['ZAIN_TEST_CHROME_EXECUTABLE']).resolve()
        if not executable.is_file():raise RuntimeError('Installed test Chromium required')
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(executable_path=str(executable),headless=True,
                args=['--disable-background-networking','--disable-component-update'])
            try:
                context=browser.new_context()
                context.route('**/*',lambda route:route.abort())
                page=context.new_page()
                page.set_content('<html><body>Isolated Chromium process fixture</body></html>')
                (self.root/'.services'/'chromium-ready').write_text(str(os.getpid()),encoding='utf-8')
                deadline=time.monotonic()+(20 if number=='2999999999' else .1)
                while time.monotonic()<deadline:
                    if self.stopping.is_set() or cancel_event.is_set():return 'network_error',None,'offline Chromium cancellation'
                    page.evaluate('() => {const until=performance.now()+50; while(performance.now()<until){}; return true;}')
                return 'ok',12.34,'offline Chromium fixture; no customer endpoint'
            finally:browser.close()

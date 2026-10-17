"""Opt-in, isolated subprocess fixture. This is not a real Chromium benchmark."""
from pathlib import Path
import threading
import time


class TestBrowserTransport:
    def __init__(self, root):
        root = Path(root).resolve()
        repository = Path(__file__).resolve().parent.parent
        marker = root / '.services' / 'TEST_FIXTURE'
        if root == repository or not marker.is_file() or marker.read_text(encoding='utf-8').strip() != 'smart-zain-isolated-tests':
            raise RuntimeError('Browser test transport requires an explicitly marked isolated root')
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

"""Real process/port exclusion; fixtures are retained for inspection."""
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from server.runtime_guard import AppRuntimeGuard
from server.gateway import ServerGateway


class RuntimeGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='runtime-guard-retained-'))

    def test_second_process_cannot_start_for_same_workspace(self):
        guard = AppRuntimeGuard(self.root).acquire('127.0.0.1', ())
        self.addCleanup(guard.close)
        script = ('from server.runtime_guard import AppRuntimeGuard; '
                  'import sys; AppRuntimeGuard(sys.argv[1]).acquire("127.0.0.1", ())')
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.root)],
                                capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        guard.close()
        replacement = AppRuntimeGuard(self.root).acquire('127.0.0.1', ())
        replacement.close()

    def test_old_process_port_blocks_start_and_releases_lock(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            with self.assertRaises(RuntimeError):
                AppRuntimeGuard(self.root).acquire('127.0.0.1', (listener.getsockname()[1],))
        replacement = AppRuntimeGuard(self.root).acquire('127.0.0.1', ())
        replacement.close()

    def test_gateway_bridge_failure_releases_web_port(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); web_port = probe.getsockname()[1]
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0)); occupied.listen()
            gateway = ServerGateway(Mock(), Mock(), self.root, self.root,
                                    web_port=web_port, bridge_port=occupied.getsockname()[1])
            with self.assertRaises(RuntimeError): gateway.start()
            self.assertIsNone(gateway.web_server)
            self.assertIsNone(gateway.bridge_server)
            with socket.socket() as reusable:
                reusable.bind(('127.0.0.1', web_port))


if __name__ == '__main__': unittest.main()

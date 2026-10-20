"""Network Gateway Runner for Smart Zain Checker.
Hosts the Web API server (Port 5050) and Extension Bridge (Port 8766) concurrently.
"""
from __future__ import annotations

import logging
import threading
import os
import socket
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from manager.orchestrator import Orchestrator
from manager.queue import QueueService
from server.extension_bridge import ExtensionBridgeHandler
from server.web_api import WebApiHandler
from server.bridge_authority import capability

logger = logging.getLogger("ServerGateway")


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class ServerGateway:
    def __init__(
        self,
        orchestrator: Orchestrator,
        queue_service: QueueService,
        web_ui_dir: Path,
        project_root: Path,
        web_port: int = 5050,
        bridge_port: int = 8766,
        bind_host: str = "127.0.0.1",
    ) -> None:
        if bind_host not in {'127.0.0.1','localhost','::1'}:
            raise ValueError('The local admin dashboard requires a loopback bind; public hosting needs an authenticated gateway')
        self.orchestrator = orchestrator
        self.queue_service = queue_service
        self.web_ui_dir = web_ui_dir
        self.project_root = project_root
        self.web_port = web_port
        self.bridge_port = bridge_port
        self.bind_host = bind_host

        self.web_server: Optional[ThreadingHTTPServer] = None
        self.bridge_server: Optional[ThreadingHTTPServer] = None

    def start(self) -> None:
        bridge_secret=capability(self.project_root)
        # 1. Start Web Dashboard API Server (Port 5050)
        class ConfiguredWebHandler(WebApiHandler):
            orchestrator = self.orchestrator
            queue_service = self.queue_service
            web_ui_dir = self.web_ui_dir
            project_root = self.project_root

        # 2. Start Extension Bridge Server (Port 8766)
        class ConfiguredBridgeHandler(ExtensionBridgeHandler):
            orchestrator = self.orchestrator
            bridge_token = bridge_secret

        try:
            self.web_server = ExclusiveHTTPServer((self.bind_host, self.web_port), ConfiguredWebHandler)
            self.bridge_server = ExclusiveHTTPServer((self.bind_host, self.bridge_port), ConfiguredBridgeHandler)
        except OSError:
            for server in (self.web_server, self.bridge_server):
                if server is not None: server.server_close()
            self.web_server = self.bridge_server = None
            raise RuntimeError('Application ports are occupied; gateway startup stopped') from None
        for server in (self.web_server, self.bridge_server):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        logger.info(f"Dashboard Web Server active at http://{self.bind_host}:{self.web_port}")
        logger.info(f"Chrome Extension Bridge active at http://{self.bind_host}:{self.bridge_port}")

    def stop(self) -> None:
        if self.web_server:
            try:
                self.web_server.shutdown()
                self.web_server.server_close()
            except Exception:
                pass
        if self.bridge_server:
            try:
                self.bridge_server.shutdown()
                self.bridge_server.server_close()
            except Exception:
                pass

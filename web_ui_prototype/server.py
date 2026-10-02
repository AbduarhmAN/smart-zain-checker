import http.server
import socketserver
import os
import sys
import io
from pathlib import Path

# Setup UTF-8 stdout to avoid crash with Arabic output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Enforce Multi-Factor Hardware Lock & Anti-Debug Check
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from zain_checker.security import enforce_hardware_lock
enforce_hardware_lock()

PORT = 5050
STATIC_DIR = Path(__file__).parent / "static"

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
        super().end_headers()

def run_isolated_server():
    os.chdir(STATIC_DIR)
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        print(f"\n=======================================================")
        print(f" Isolated UI Prototype running at: http://localhost:{PORT}")
        print(f" Open http://localhost:{PORT} in Chrome or Edge to review!")
        print(f" Press Ctrl+C in this terminal when you want to stop.")
        print(f"=======================================================\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")

if __name__ == '__main__':
    run_isolated_server()

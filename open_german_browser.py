"""Launch Google Chrome with German SOCKS5 Proxy.
Connects via local HTTP-to-SOCKS5 bridge to 166.0.39.88:7596 (Frankfurt, Germany).
"""
import sys
import io
import time
import socket
import threading
from pathlib import Path
import socks
from playwright.sync_api import sync_playwright

# Python Windows Encoding Rule
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

PROXY_HOST = "166.0.39.88"
PROXY_PORT = 7596
PROXY_USER = "svmavlhd"
PROXY_PASS = "s26hg8fpprfa"
LOCAL_BRIDGE_PORT = 8891

def start_socks5_bridge(local_port, remote_host, remote_port, username, password):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(50)

    def handle_client(client_sock):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = client_sock.recv(4096)
                if not chunk:
                    break
                req += chunk
            first_line = req.split(b"\r\n")[0].decode("latin1", errors="ignore")
            parts = first_line.split(" ")
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]
            if method.upper() == "CONNECT":
                host, port_str = target.split(":")
                target_port = int(port_str)
                remote = socks.socksocket()
                remote.set_proxy(socks.SOCKS5, remote_host, remote_port, username=username, password=password)
                remote.settimeout(20)
                remote.connect((host, target_port))
                client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

                def forward(src, dst):
                    try:
                        while True:
                            data = src.recv(8192)
                            if not data:
                                break
                            dst.sendall(data)
                    except Exception:
                        pass
                    finally:
                        try: src.close()
                        except Exception: pass
                        try: dst.close()
                        except Exception: pass

                threading.Thread(target=forward, args=(client_sock, remote), daemon=True).start()
                threading.Thread(target=forward, args=(remote, client_sock), daemon=True).start()
        except Exception:
            try: client_sock.close()
            except Exception: pass

    def listen_loop():
        while True:
            try:
                c, _ = server.accept()
                threading.Thread(target=handle_client, args=(c,), daemon=True).start()
            except Exception:
                break

    t = threading.Thread(target=listen_loop, daemon=True)
    t.start()
    return server

def main():
    print("=" * 60)
    print("🇩🇪 تشغيل متصفح كروم عبر البروكسي الألماني (Frankfurt, Germany)")
    print(f"[*] البروكسي: {PROXY_HOST}:{PROXY_PORT}")
    print("=" * 60)

    # 1. تشغيل الجسر المحلي
    print(f"[*] جاري ربط جسر البروكسي المحلي على المنفذ {LOCAL_BRIDGE_PORT}...")
    start_socks5_bridge(LOCAL_BRIDGE_PORT, PROXY_HOST, PROXY_PORT, PROXY_USER, PROXY_PASS)
    time.sleep(1)

    profile_dir = Path(__file__).resolve().parent / ".browser_profiles" / "german_browser"
    profile_dir.mkdir(parents=True, exist_ok=True)

    print("[*] جاري فتح نافذة متصفح Google Chrome المباشرة...")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(profile_dir),
            channel="chrome",
            headless=False,
            proxy={"server": f"http://127.0.0.1:{LOCAL_BRIDGE_PORT}"},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            locale="ar-SA",
            viewport=None,
        )

        page1 = context.pages[0] if context.pages else context.new_page()
        print("🌐 جاري فتح صفحة الدفع السريع لزين...")
        try:
            page1.goto("https://app.sa.zain.com/ar/quickpay", timeout=30000)
            print("✅ تم تحميل صفحة زين بنجاح!")
        except Exception as e:
            print(f"⚠️ ملاحظة صفحة زين: {e}")

        # صفحة التحقق من الآي بي في تبويب ثانٍ
        page2 = context.new_page()
        try:
            page2.goto("https://ipinfo.io", timeout=20000)
            print("✅ تم فتح صفحة فحص الآي بي (ipinfo.io) بنجاح!")
        except Exception:
            pass

        print("\n" + "=" * 60)
        print("🎉 المتصفح مفتوح وشغال الآن أمامك بالبروكسي الألماني.")
        print("يمكنك استخدام المتصفح بحرية كاملة.")
        print("=" * 60)

        # إبقاء المتصفح مفتوحاً طالما النافذة مفتوحة
        try:
            while len(context.pages) > 0:
                time.sleep(2)
        except Exception:
            pass
        finally:
            try:
                context.close()
            except Exception:
                pass
        print("تم إغلاق المتصفح.")

if __name__ == "__main__":
    main()

import sys
import io
import time
import socket
import threading
from pathlib import Path
import socks
from playwright.sync_api import sync_playwright

# ضبط الترميز لدعم العربية في ويندوز
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# خادم تحويل محلي (HTTP-to-SOCKS5 Bridge)
def start_socks5_bridge(local_port, remote_host, remote_port, username, password):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", local_port))
    except Exception:
        server.bind(("127.0.0.1", 0))
        local_port = server.getsockname()[1]
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
                try:
                    remote.set_proxy(socks.SOCKS5, remote_host, remote_port, username=username, password=password)
                    remote.settimeout(15)
                    remote.connect((host, target_port))
                except Exception:
                    remote = socks.socksocket()
                    remote.set_proxy(socks.SOCKS5, remote_host, 1080, username=username, password=password)
                    remote.settimeout(15)
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

    threading.Thread(target=listen_loop, daemon=True).start()
    return local_port

print("=" * 65)
print("🚀 تشغيل متصفح كروم عبر بروكسي Webshare (svmavlhdresidential-in-7)")
print("=" * 65)

LOCAL_PORT = 8883
REMOTE_HOST = "p.webshare.io"
REMOTE_PORT = 1080
PROXY_USER = "svmavlhdresidential-in-7"
PROXY_PASS = "s26hg8fpprfa"

print(f"[*] تشغيل الجسر المحلي للبروكسي ({REMOTE_HOST}:{REMOTE_PORT})...")
actual_port = start_socks5_bridge(LOCAL_PORT, REMOTE_HOST, REMOTE_PORT, PROXY_USER, PROXY_PASS)
time.sleep(1)

base_dir = Path(__file__).resolve().parent / ".browser_profiles"
prof_sa = base_dir / "webshare_proxy_profile"
prof_sa.mkdir(parents=True, exist_ok=True)

with sync_playwright() as pw:
    print(f"\n[+] جارٍ فتح متصفح كروم بالبروكسي المخصص...")
    browser = pw.chromium.launch_persistent_context(
        str(prof_sa),
        channel="chrome",
        headless=False,
        proxy={"server": f"http://127.0.0.1:{actual_port}"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--window-position=150,80",
            "--window-size=1200,900",
        ],
        locale="ar-SA"
    )
    page = browser.pages[0] if browser.pages else browser.new_page()

    print("  🌐 جارٍ فحص الآي بي والتأكد من الاتصال عبر البروكسي...")
    try:
        page.goto("https://api.ipify.org", timeout=20000)
        ip = page.inner_text("body").strip()
        print(f"  ✅ تم الاتصال بنجاح! عنوان IP المتصفح هو: {ip}")
    except Exception as e:
        print(f"  ⚠️ ملاحظة فحص IP: {e}")

    print("  🌐 فتح موقع زين في المتصفح...")
    try:
        page.goto("https://app.sa.zain.com/ar/quickpay", timeout=30000)
        print("  ✅ صفحة فحص زين مفتوحة وجاهزة الآن للاستخدام!")
    except Exception as e:
        print(f"  ⚠️ ملاحظة فتح صفحة زين: {e}")

    print("\n" + "=" * 65)
    print("🎉 متصفح كروم بالبروكسي السعودي مفتوح الآن على شاشتك!")
    print("👉 يمكنك تصفح أي موقع واستخدامه بحرية تامة.")
    print("⚠️  لإغلاق المتصفح عند الانتهاء، اضغط Enter هنا في التيرمينال...")
    print("=" * 65)
    input()

    browser.close()
    print("تم إغلاق المتصفح.")

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

# خادم تحويل محلي بسيط (HTTP-to-SOCKS5 Bridge)
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

print("=" * 65)
print("🚀 تشغيل متصفحي كروم عبر بروكسي SOCKS5 المعتمد")
print("=" * 65)

# تشغيل الجسور المحلية
print("[*] تشغيل جسر البروكسي 1 (166.0.39.88:7596)...")
start_socks5_bridge(8881, "166.0.39.88", 7596, "svmavlhd", "s26hg8fpprfa")

print("[*] تشغيل جسر البروكسي 2 (23.27.85.174:7676)...")
start_socks5_bridge(8882, "23.27.85.174", 7676, "svmavlhd", "s26hg8fpprfa")

time.sleep(1)

base_dir = Path(__file__).resolve().parent / ".browser_profiles"
prof1 = base_dir / "proxy1_profile"
prof2 = base_dir / "proxy2_profile"
prof1.mkdir(parents=True, exist_ok=True)
prof2.mkdir(parents=True, exist_ok=True)

with sync_playwright() as pw:
    # 1. فتح المتصفح الأول
    print("\n[1/2] جارٍ فتح المتصفح الأول (Proxy: 166.0.39.88:7596)...")
    browser1 = pw.chromium.launch_persistent_context(
        str(prof1),
        channel="chrome",
        headless=False,
        proxy={"server": "http://127.0.0.1:8881"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--window-position=50,50",
            "--window-size=950,850",
        ],
        locale="ar-SA"
    )
    page1 = browser1.pages[0] if browser1.pages else browser1.new_page()
    print("  🌐 فتح صفحة زين في المتصفح الأول...")
    try:
        page1.goto("https://app.sa.zain.com/ar/quickpay", timeout=30000)
        print("  ✅ المتصفح الأول جاهز ومفتوح الآن على صفحة زين!")
    except Exception as e:
        print(f"  ⚠️  ملاحظة المتصفح الأول: {e}")

    # 2. فتح المتصفح الثاني
    print("\n[2/2] جارٍ فتح المتصفح الثاني (Proxy: 23.27.85.174:7676)...")
    browser2 = pw.chromium.launch_persistent_context(
        str(prof2),
        channel="chrome",
        headless=False,
        proxy={"server": "http://127.0.0.1:8882"},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--window-position=960,50",
            "--window-size=950,850",
        ],
        locale="ar-SA"
    )
    page2 = browser2.pages[0] if browser2.pages else browser2.new_page()
    print("  🌐 فتح صفحة زين في المتصفح الثاني...")
    try:
        page2.goto("https://app.sa.zain.com/ar/quickpay", timeout=30000)
        print("  ✅ المتصفح الثاني جاهز ومفتوح الآن على صفحة زين!")
    except Exception as e:
        print(f"  ⚠️  ملاحظة المتصفح الثاني: {e}")

    print("\n" + "=" * 65)
    print("🎉 تم فتح المتصفحين بنجاح! كلاهما يعمل بكفاءة.")
    print("👉 يمكنك التصفح وإجراء الفحوصات في كلا المتصفحين بحرية.")
    print("⚠️  لإغلاق المتصفحين، اضغط Enter في هذه النافذة...")
    print("=" * 65)
    input()

    browser1.close()
    browser2.close()
    print("تم إغلاق المتصفحين.")

"""Browser checks of the app.py UI with synthetic API responses only. No workers/bot."""
import json
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'outputs' / 'ui_review_20261007_compact'
OUT.mkdir(parents=True, exist_ok=True)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    baseline = '--baseline' in sys.argv
    rows = [dict(row=i + 2, record_type='wallet' if i % 2 else 'account',
                 lookup_number=f'001234{i:04d}', customer_name=f'عميل تجريبي {i}',
                 expected_amount=100.5, live_amount=100.5,
                 status=['match', 'mismatch', 'needs_review', 'error', 'not_found'][i % 5],
                 error_message='مهلة الاتصال' if i % 5 == 3 else '') for i in range(123)]
    rows[0].update(customer_name='<img src=x onerror=alert(1)>', expected_amount=None, live_amount=None)
    rows[1].update(expected_amount=0, live_amount=0)
    state = dict(running=False, paused=False, round_pending=False, result_ready=True,
                 workbook='عينة اختبار.xlsx', workers=[dict(worker_id=f'worker_{i+1}',name=f'مسار {i+1}',status='ready',use_proxy=i>1) for i in range(9)], verifications=[], table_rows=rows,
                 kpis=dict(total=123, completed=123, remaining=0, matches=25, mismatches=25,
                           verified=50, errors=24, needs_review=25, not_found=24, deferred=0,
                           mismatch_total=180.5))
    state['session_timing']=dict(session_id='fixture1',started_at=time.time()-600,server_time=time.time(),initial_completed=0)
    finished_kpis=dict(state['kpis'])
    jobs = [dict(id='fixture1', filename='عينة اختبار.xlsx', sheet_name='العملاء',
                 total_records=123, completed=123, matches=25, mismatches=25, status='completed',pricing=dict(mode='customer',unit_minor=100))]
    requests, errors = [], []
    telegram_state=dict(enabled=False,configured=True,connected=True,polling=False,delivery={})

    def route_api(route):
        request = route.request
        path = urlsplit(request.url).path
        requests.append(dict(path=path, method=request.method))
        payload = {
            '/api/live-status': state,
            '/api/session-info': dict(workbooks=['عينة اختبار.xlsx'], default_sheets=['العملاء']),
            '/api/queue': dict(status='ok', jobs=jobs),
            '/api/pricing': dict(status='ok', settings=dict(mode='sheet', customer_price='0.00', sheet_price='0.00')),
            '/api/telegram/status': telegram_state,
        }.get(path, dict(status='ok', sheets=['العملاء'], is_results=False))
        route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'web_ui')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport=dict(width=1440, height=1000), reduced_motion='reduce',timezone_id='America/New_York')
            context.route('**/api/**', route_api)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.wait_for_function("Dashboard.connected && LiveFeed.activeTab === 'all'")
            page.wait_for_function("document.getElementById('clockBox').textContent !== '—'")
            page.screenshot(path=str(OUT / ('00_before_desktop.png' if baseline else '01_desktop.png')), full_page=True)
            if baseline:
                print(json.dumps(dict(baseline=True, errors=errors), ensure_ascii=False))
                browser.close()
                return

            checks = []
            def check(name, condition):
                assert condition, name
                checks.append(name)

            check('50 rows per page', page.locator('#auditTableBody tr[data-status]').count() == 50)
            check('missing money is dash', page.locator('#auditTableBody tr[data-status]').first.locator('td').nth(4).inner_text().strip() == '—')
            check('zero remains zero', '0.00' in page.locator('#auditTableBody tr[data-status]').nth(1).locator('td').nth(4).inner_text())
            check('HTML displayed as text', page.locator('#auditTableBody img').count() == 0)
            check('timing shown without opening anything',page.locator('.session-timing').is_visible() and page.locator('#timingElapsed').inner_text()!='—')
            check('elapsed time is compact clock',':' in page.locator('#timingElapsed').inner_text())
            page.wait_for_function("document.getElementById('clockBox').textContent.slice(0,5) === new Intl.DateTimeFormat('en-GB',{hour:'2-digit',minute:'2-digit',hourCycle:'h23',timeZone:'Asia/Baghdad'}).format(new Date())",timeout=3000)
            check('clock uses Baghdad across browser timezones',True)
            check('no expandable dashboard sections',page.locator('main details').count()==0)
            check('nine compact worker squares',page.locator('.worker-mini').count()==9 and page.locator('.worker-mini').first.bounding_box()['height']<=36)
            check('pricing summary is one line',page.locator('.billing-strip').bounding_box()['height']<=46)
            page.locator('#btnBillingVisibility').click()
            check('pricing values still correct','١٢٣' in page.locator('#billingTotal').inner_text())
            page.locator('.billing-actions button[onclick="Pricing.open()"]').click()
            check('prices configured in settings dialog',page.locator('#pricingOverlay').is_visible())
            page.keyboard.press('Escape')
            page.locator('#resultNext').click()
            check('next page', page.locator('#auditTableBody tr[data-status]').first.locator('td').first.inner_text().strip() == '52')
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            check('poll preserves page', page.locator('#resultPageLabel').inner_text().strip() == '2 / 3')
            page.locator('#resultSearch').fill('٠٠١٢٣٤٠١٢٢')
            page.wait_for_timeout(250)
            check('Arabic digits search entire loaded data', page.locator('#auditTableBody tr[data-status]').count() == 1)
            check('leading zeros retained', page.locator('#auditTableBody').inner_text().find('0012340122') >= 0)
            page.locator('#resultSearchClear').click()
            page.locator('#resultType').select_option('wallet')
            check('service filter', page.locator('#tableTabBadge').inner_text().startswith('61'))
            page.locator('button[data-tab="review"]').click()
            check('review filter includes not found', page.locator('#auditTableBody tr[data-status="not_found"]').count() > 0)
            page.locator('#resultType').select_option('all')
            page.locator('button[data-tab="all"]').click()
            page.locator('.result-detail-button').first.click()
            check('row details visible', page.locator('#resultDetailOverlay').is_visible())
            check('missing amount in details', page.locator('#resultDetailExpected').inner_text() == '—')
            page.screenshot(path=str(OUT / '02_detail.png'))
            page.keyboard.press('Escape')
            check('details close with Escape', not page.locator('#resultDetailOverlay').is_visible())
            page.keyboard.press('Control+k')
            check('search shortcut', page.locator('#resultSearch').evaluate('(el) => el === document.activeElement'))
            page.locator('button[data-view="queue"]').click()
            check('queue still works', page.locator('#queueTableContainer').is_visible())
            check('search hidden for queue', not page.locator('#resultFilters').is_visible())
            page.screenshot(path=str(OUT / '03_queue.png'), full_page=True)
            page.locator('button[data-view="telegram"]').click()
            check('Telegram tools open in dialog', page.locator('#telegramTools').is_visible())
            check('Telegram reception is separate from authentication',page.locator('#tgPollingState').inner_text()=='متوقف')
            telegram_state.update(enabled=True,polling=True,delivery=dict(pending=2,last_delivery_at=time.time(),last_error=None))
            page.evaluate('Dashboard.refreshTelegram()')
            page.wait_for_function("document.getElementById('tgPollingState').textContent==='يستقبل الأوامر'")
            check('delivery queue visible',page.locator('#tgPendingCount').inner_text()=='2')
            check('confirmed delivery displayed',page.locator('#tgDeliveryState').inner_text()=='تم التسليم')
            page.screenshot(path=str(ROOT/'outputs/telegram_audit_20261007/dashboard_telegram.png'))
            telegram_state['bot_health']={'last_error':'telegram_409'}
            page.evaluate('Dashboard.refreshTelegram()')
            page.wait_for_function("document.getElementById('tgPollingState').textContent==='تعذر استقبال الأوامر'")
            check('polling errors visible even after identity verification',page.locator('#tgStatusBadge').inner_text()=='مشكلة في الاستقبال')
            telegram_state['bot_health']={}
            telegram_state.update(connected=False,polling=False,delivery=dict(pending=0,last_error='telegram_rate_limit'))
            page.evaluate('Dashboard.refreshTelegram()')
            page.wait_for_function("document.getElementById('tgStatusBadge').textContent==='تعذر الاتصال'")
            check('delivery issue has a distinct error state',page.locator('#tgDeliveryState').inner_text()=='يحتاج متابعة')
            telegram_state.update(enabled=False,connected=True,polling=False,delivery={})
            page.keyboard.press('Escape')
            page.locator('button[data-view="results"]').click()
            page.locator('#resultSearch').fill('لا توجد هذه النتيجة')
            page.wait_for_timeout(250)
            check('empty filter guidance', page.locator('#auditTableBody').inner_text().find('لا توجد نتائج مطابقة') >= 0)
            page.locator('#resultSearchClear').click()
            preview_rows=[dict(row=900, type='wallet', number='0023456789', name='عميل المعاينة',
                               expected_sar='0.00', live_sar='', status='needs_review', issue='ملاحظة من ملف محفوظ')]
            page.evaluate('rows=>{App.detectedResultsInfo={rows,total_records:1};LiveFeed.renderTable(rows,true)}',preview_rows)
            page.locator('#resultSearch').fill('۰۰۲۳۴۵۶۷۸۹')
            page.wait_for_timeout(250)
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            check('saved preview and Persian digits', page.locator('#auditTableBody tr[data-status]').count()==1)
            check('preview count survives poll', page.locator('#tableTabBadge').inner_text()=='1 سجل')
            check('empty amount in preview', page.locator('#auditTableBody tr[data-status]').first.locator('td').nth(5).inner_text().strip()=='—')
            page.locator('.result-detail-button').first.click()
            check('preview note shown', page.locator('#resultDetailNote').inner_text()=='ملاحظة من ملف محفوظ')
            page.keyboard.press('Escape')
            page.locator('#resultSearchClear').click()
            page.evaluate('App.detectedResultsInfo=null')
            state['running'] = True
            state['kpis'].update(completed=10,remaining=113,matches=4,mismatches=2,verified=6,needs_review=1,not_found=2,errors=1,mismatch_total=25.5)
            state['workers'][0]['status']='processing'
            state['session_timing']['recent_completions']=[dict(age_seconds=10-i,kind='account') for i in range(11)]
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            check('running results gate preserved', 'النتائج بعد اكتمال الجولة' in page.locator('#auditTableBody').inner_text())
            check('active worker pulses',page.locator('.worker-mini.busy').count()==1)
            check('remaining time visible while running',page.locator('#timingRemaining').inner_text().startswith('نحو'))
            check('finish estimate visible',page.locator('#timingFinish').inner_text()!='—')
            page.evaluate('document.activeElement.blur();window.scrollTo(0,0)')
            page.screenshot(path=str(OUT / '06_running.png'),full_page=True)
            state['running'] = False
            state['kpis']=dict(finished_kpis)
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            state['workers'] = [dict(worker_id='worker_1',name='المسار المباشر',status='ready',service_verification_required=True,service_verification_pending=True)]
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            check('worker problem marked in compact strip',page.locator('.worker-mini.warn').count()==1 and 'متابعة' in page.locator('#workerSummary').inner_text())
            check('worker details never open automatically',not page.locator('#workerDetailsOverlay').is_visible())
            page.locator('.worker-mini').first.click()
            check('worker details on request',page.locator('#workerDetailsOverlay').is_visible() and 'التحقق' in page.locator('#workerDetailsState').inner_text())
            page.keyboard.press('Escape')
            state['workers'] = [dict(worker_id=f'worker_{i+1}',name=f'مسار {i+1}',status='ready',use_proxy=i>1) for i in range(9)]
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            rows[0]['customer_name']='عميل تجريبي 0'
            page.evaluate('App.poll()')
            page.wait_for_timeout(1200)
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(OUT / '01_desktop.png'), full_page=True)
            page.screenshot(path=str(OUT / '07_desktop_viewport.png'))
            for width in (1024, 768, 390):
                page.set_viewport_size(dict(width=width, height=844))
                page.evaluate('window.scrollTo(0,0)')
                page.wait_for_timeout(200)
                check(f'no page overflow at {width}', page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                check(f'clock visible at {width}',page.locator('#clockBox').is_visible())
                check(f'timing visible at {width}',page.locator('.session-timing').is_visible())
                if width == 390:
                    page.screenshot(path=str(OUT / '04_mobile.png'), full_page=True)
                    check('mobile setup stays visible',page.locator('#setupContent').is_visible())
                    page.locator('button[data-view="results"]').click()
                    check('mobile search stays usable', page.locator('#resultSearch').is_visible())
                    page.screenshot(path=str(OUT / '05_mobile_results.png'), full_page=True)
            check('no JavaScript errors', not errors)
            check('no external actions requested', all(r['method'] == 'GET' for r in requests))
            report = dict(checks=checks, page_errors=errors, api_requests=requests,
                          api_mode='synthetic responses; no app.py workers or Telegram started')
            (OUT / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(dict(passed=len(checks), errors=errors), ensure_ascii=False))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()

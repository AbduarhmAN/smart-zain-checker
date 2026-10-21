"""Generate a local Arabic review document from actual panels and fictional data."""
from html import escape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from retained_runtime import install
install()
from telegram_bot import views
from telegram_bot.presentation import rtl_html
from telegram_bot.commerce.partner_views import partners_panel,partner_details
from telegram_bot.commerce.partner_lifecycle import lifecycle_panel,PartnerLifecycleMixin
from telegram_bot.commerce.partner_finance import PartnerFinanceMixin
from telegram_bot.commerce.pricing_ui import PricingUiMixin
from telegram_bot.commerce.partner_sheets import PartnerSheetMixin

PANELS=[]
PID='11111111-1111-4111-8111-111111111111'
EVENT='22222222-2222-4222-8222-222222222222'
names={101:'اسم تجريبي أول',102:'اسم تجريبي ثانٍ'}
name=lambda user:names.get(user,'اسم تجريبي')
def collect(title,panel):PANELS.append((title,panel))
def harness(cls,title,data):
    h=cls();h._ui_lock=threading.RLock();h.current_authorized_ids={9};h._waiting_user_inputs={}
    h._telegram_person_name=name;h._show_panel=lambda actor,panel,*args:collect(title,panel)
    h._commerce_notice=lambda *args:(_ for _ in ()).throw(AssertionError('Unexpected preview failure'))
    h._cancel_admin_partner_payout=lambda *args:None
    h._commerce=lambda:SimpleNamespace(rpc=lambda method,args:data[method])
    return h


class TelegramHtml(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.stack=[];self.text=[]
    def handle_starttag(self,tag,attrs):
        assert tag in {'b','strong','i','em','code','pre','a','u','s','span','tg-spoiler'},tag
        self.stack.append(tag)
    def handle_endtag(self,tag):assert self.stack.pop()==tag,tag
    def handle_data(self,data):self.text.append(data)


def main():
    state={'running':False,'kpis':{},'workbook':''}
    jobs=[{'id':'fixture-'+str(i),'filename':'ملف تجريبي '+str(i)+'.xlsx','sheet_name':'البيانات',
        'column_mapping':{'telegram_chat_id':i},'status':'active' if i==101 else 'pending',
        'completed':50 if i==101 else 0,'total_records':1100} for i in (101,102)]
    record={'id':PID,'telegram_user_id':101,'code':'FIXTURE_ONE','state':'active',
        'commission_basis_points':3000,'entitlement_mode':'forever','enabled':True}
    collect('لوحة الإدارة',views.home(state,jobs,9,True,'admin'))
    collect('واجهة العميل',views.home(state,[],101))
    collect('قائمة الفحوص',views.queue(jobs,person_name=name))
    collect('الشركاء الحاليون',partners_panel([record],name,'PreviewFixtureBot'))
    collect('بيانات الشريك',partner_details(record,name(101),'PreviewFixtureBot'))
    former=harness(PartnerLifecycleMixin,'الشركاء السابقون',{'ended_partners':[
        {'id':PID,'telegram_user_id':102,'code':'FIXTURE_FORMER','ended_at':'2026-10-21T00:00:00Z','event_id':EVENT}]})
    former._partner_lifecycle_callback('pl:ended:0',9)
    body='نشكرك على تعاونك. السجل المالي محفوظ للمراجعة والتسوية.\nحساب Telegram: 102\nوقت الإجراء UTC: 2026-10-21 00:00:00 UTC'
    collect('عرض إشعار محفوظ',lifecycle_panel({'id':EVENT,'partner_id':PID,'recipient_telegram_id':102,
        'actor_telegram_id':9,'kind':'ended','body':body,'body_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'deliveries':[]},admin=True,person_name=name))
    summary={'gross_service_minor':100000,'partner_commission_minor':30000,'platform_revenue_minor':70000,
        'today_platform_revenue_minor':20000,'awaiting_settlement_orders':2,'partners_total':1,
        'partners':[{'telegram_user_id':101,'state':'active','earned_minor':30000,'available_minor':20000,
            'reserved_minor':0,'withdrawn_minor':10000,'pending_minor':0}]}
    h=harness(PartnerFinanceMixin,'ملخص الإيرادات والأرباح',{'admin_earnings_summary':summary})
    h._admin_earnings(9)
    collect('المحفظة',views.wallet_panel(101,150.25,account_id=101,show_crypto=False))
    collect('مراجعة الدفع',views.admin_approval_panel('101','unused_username','ملف تجريبي.xlsx',3000,
        'تحويل تجريبي','FIXTURE-TRANSACTION',0,'fixture',amount_egp=30,display_name=name(101)))
    h=harness(PricingUiMixin,'أسعار العملاء',{'customer_pricing':{'base_minor':1,'service_threshold':500,'service_excess_minor':3000}})
    h._pricing_show(9,0)
    h=harness(PartnerSheetMixin,'تسعير ملفات الشركاء',{'partner_sheet_context':{'price':{'amount_minor':3000,'version':2}},
        'partner_sheet_notification_status':{'sent':2}})
    h._partner_sheet_admin('ps:settings:0',9,None,{})
    collect('المديرون والإعدادات',views.admin_menu('settings'))
    collect('إدارة التشغيل',views.operations(state,jobs))
    # Verify the maximum financial page with long names and large balances.
    maximum=dict(summary,partners_total=4,partners=[dict(summary['partners'][0],telegram_user_id=101+i,
        available_minor=999999999999,earned_minor=999999999999,pending_minor=999999999999,
        advance_outstanding_minor=999999999999) for i in range(4)])
    worst=harness(PartnerFinanceMixin,'اختبار حد طول صفحة الأرباح',{'admin_earnings_summary':maximum})
    worst._telegram_person_name=lambda _: 'اسم طويل '+('حرف '*28)
    worst._admin_earnings(9)
    cards=[];evidence=[]
    for title,panel in PANELS:
        text=rtl_html(views.copyable_message(panel.text))
        parser=TelegramHtml();parser.feed(text);assert not parser.stack,title
        visible=''.join(parser.text)
        assert len(visible)<=4096,(title,len(visible))
        assert all(len(row)<=2 for row in panel.rows),title
        for row in panel.rows:
            for item in row:
                if item.get('callback_data'):assert len(item['callback_data'].encode())<=64,(title,item)
        keyboard=''.join('<div class="row">'+''.join('<button class="'+escape(item.get('style','neutral'))+'" title="'+
            escape(item.get('callback_data',''))+'">'+escape(item['text'])+'</button>' for item in row)+'</div>' for row in panel.rows)
        cards.append('<article><h2>'+escape(title)+'</h2><div class="message">'+text.replace('\n','<br>')+'</div><div class="keyboard">'+keyboard+'</div></article>')
        evidence.append({'panel':title,'visible_characters':len(visible),'button_rows':len(panel.rows),'valid_html':True,'callback_bytes_bounded':True})
    html='''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>مراجعة واجهة تيليجرام</title><style>
    *{box-sizing:border-box}body{margin:0;background:#edf0f3;color:#1c2835;font:16px/1.85 Tahoma,Arial,sans-serif}header{max-width:1200px;margin:auto;padding:38px 24px 20px}h1{font-size:27px;margin:0}header p{max-width:850px;color:#435363}main{max-width:1200px;margin:auto;padding:16px 24px 60px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:30px;align-items:start}article{min-width:0}h2{font-size:16px;margin:0 0 12px}.message{padding:20px;background:white;border-radius:12px;white-space:normal;overflow-wrap:anywhere;box-shadow:0 2px 6px #21374d0a}code{font:14px/1.8 Consolas,monospace;unicode-bidi:isolate;direction:ltr;background:#f1f4f7;padding:1px 3px}a{color:#1f67ab;text-decoration:none}.keyboard{padding-top:6px}.row{display:flex;gap:6px;margin-bottom:6px}.row button{flex:1;min-width:0;font:14px/1.55 Tahoma,Arial;padding:11px 8px;border:0;border-radius:6px;background:#dce5ee;color:#243e56;overflow-wrap:anywhere}.row .primary{background:#3773ac;color:white}.row .success{background:#3c8068;color:white}.row .danger{background:#ab5656;color:white}@media(max-width:390px){main{padding:12px;grid-template-columns:minmax(0,1fr)}header{padding:24px 14px}}
    </style><header><h1>مراجعة واجهة تيليجرام</h1><p>معاينة محلية من كود اللوحات الفعلي ببيانات تجريبية معلّمة. لا تمثل عملاء أو أرصدة حقيقية. الأزرار للعرض فقط؛ الخطوط والألوان النهائية يحددها تطبيق تيليجرام وإعدادات الجهاز.</p></header><main>'''+''.join(cards)+'</main></html>'
    folder=ROOT/'outputs/telegram_ui_20261021'
    (folder/'preview.html').write_text(html,encoding='utf-8')
    (folder/'preview_validation.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'panels':len(evidence),'valid':True,'preview':str(folder/'preview.html')}))


if __name__=='__main__':main()

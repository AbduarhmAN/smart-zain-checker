"""Compact partner navigation with separate identity, terms and balances."""
from uuid import UUID
from telegram_bot import views
from telegram_bot.presentation import DIVIDER
from .referrals import referral_link


def current_partner(record):
    return record.get('state') != 'ended' and not record.get('ended_at') and not record.get('partnership_ended_at')


def partnership_label(record):
    if not current_partner(record):return 'منتهية'
    return {'active':'نشطة','suspended':'موقوفة مؤقتًا','pending':'بانتظار التفعيل'}.get(record.get('state'),'تحتاج مراجعة')


def partners_panel(records, person_name, bot_name='', offset=0):
    lines = ['<b>الشركاء الحاليون</b>', 'اختر اسم الشريك لعرض بياناته وإعداداته.']
    rows = []
    for partner in records:
        if not current_partner(partner):
            continue
        pid = str(UUID(partner['id']))
        target = partner['telegram_user_id']
        name = person_name(target)
        code = partner.get('code') or ''
        lines.extend(['', DIVIDER, '<b>'+views.safe(name,80)+'</b>',
            'رقم حساب تيليجرام: <code>'+views.safe(target)+'</code>',
            'رمز الشريك: <code>'+views.safe(code)+'</code>',
            'الشراكة: '+partnership_label(partner)])
        if bot_name and code:
            lines.append('<a href="'+referral_link(bot_name,code)+'">رابط الشريك</a>')
        rows.append([views.button(name[:50], 'cp:profile:'+pid),
                     views.button('صرف الأرباح', 'wa:paypartner:'+str(target), 'primary')])
    if not rows:
        lines.append('لا توجد شراكات حالية في هذه الصفحة. السجلات السابقة متاحة من الزر أدناه.')
    navigation = []
    if offset:navigation.append(views.button('السابق', 'cp:list:'+str(max(0,offset-10))))
    if len(records) == 10:navigation.append(views.button('التالي', 'cp:list:'+str(offset+10)))
    if navigation:rows.append(navigation)
    rows.extend([[views.button('إضافة شريك', 'cp:add:0'), views.button('الشركاء السابقون', 'pl:ended:0')],
                 [views.button('ملخص الأرباح', 'pf:admin:0'), views.button('قسم الشركاء', 'menu:partners')],
                 [views.button('الرئيسية', 'home')]])
    return views.Panel('\n'.join(lines),rows)


def partner_details(record, name, bot_name=''):
    pid = str(UUID(record.get('partner_id') or record['id']))
    target = record['telegram_user_id']
    code = record.get('code') or ''
    lines = ['<b>بيانات الشريك</b>', '', '<b>'+views.safe(name,80)+'</b>',
        'رقم حساب تيليجرام: <code>'+views.safe(target)+'</code>',
        'رمز الشريك: <code>'+views.safe(code)+'</code>',
        'الشراكة: '+partnership_label(record)]
    if code and bot_name:lines.append('<a href="'+referral_link(bot_name,code)+'">فتح رابط الشريك</a>')
    if not current_partner(record):
        lines.append('الشراكة منتهية. السجل المالي السابق محفوظ.')
        return views.Panel('\n'.join(lines),[[views.button('السجل المالي', 'pf:partner:'+str(target)),
            views.button('الشركاء السابقون','pl:ended:0')],[views.button('الشركاء الحاليون','cp:list:0')]])
    rate=record.get('commission_basis_points')
    if rate is not None:lines.append('نسبة العمولة: '+str(rate/100)+'%')
    mode=record.get('entitlement_mode')
    if mode:
        limit=record.get('entitlement_checks') if mode=='checks' else record.get('entitlement_duration_seconds')
        lines.append('مدة الاستحقاق: '+('مستمرة' if mode=='forever' else str(limit)+' فحص' if mode=='checks' else str(int(limit or 0)//86400)+' يوم'))
    access=record.get('enabled',record.get('partner_sheet_enabled',True))
    lines.append('الفحص بسعر الشريك: '+('متاح' if access else 'متوقف'))
    rows=[[views.button('الأرباح والمستحقات','pf:partner:'+str(target)), views.button('صرف الأرباح','wa:paypartner:'+str(target),'primary')],
          [views.button('شروط الشراكة','cp:terms:'+pid),views.button('الفحص بسعر الشريك','ps:access:'+pid)],
          [views.button('إنهاء الشراكة','pl:remove:'+pid)],
          [views.button('الشركاء الحاليون','cp:list:0'),views.button('الرئيسية','home')]]
    return views.Panel('\n'.join(lines),rows)

"""Read-only customer code funding and genuinely free completed checks."""
from datetime import datetime, timezone, timedelta
from uuid import UUID

from telegram_bot import views
from telegram_bot.authorization import admin_ids, user_id
from .client import CommerceUnavailable
from .checkout import egp


def activity_time(value):
    if not isinstance(value, str):
        raise ValueError('Activity timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timezone required')
    return parsed.astimezone(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M')


class CustomerActivityMixin:
    def _customer_activity(self, command, actor, message_id=None):
        try:
            if actor not in admin_ids(self):
                raise ValueError('Administrator required')
            parts = command.split(':')
            if len(parts) not in (4, 5) or parts[0] != 'cr' or parts[2] not in {'codes', 'free'}:
                raise ValueError('Invalid activity route')
            action, kind = parts[1:3]
            customer = None
            if action == 'list' and len(parts) == 4:
                offset = int(parts[3])
            elif action == 'detail' and len(parts) == 5:
                customer, offset = user_id(parts[3]), int(parts[4])
                if customer is None:
                    raise ValueError('Customer identity required')
            else:
                raise ValueError('Unknown activity route')
            if not 0 <= offset <= 1000000:
                raise ValueError('Invalid activity page')
            page = self._commerce().rpc('admin_customer_activity', {
                'p_actor_telegram_id': actor, 'p_kind': kind, 'p_offset': offset, 'p_limit': 6,
                'p_customer_telegram_id': customer, 'p_excluded_admin_ids': sorted(admin_ids(self)),
            })
            if page['kind'] != kind or page['customer_telegram_id'] != customer or page['offset'] != offset:
                raise ValueError('Activity response differs from request')
            title = 'العملاء الذين شحنوا بالأكواد' if kind == 'codes' else 'العملاء المستفيدون من الفحص المجاني'
            lines = ['<b>' + title + '</b>', 'سجلات Supabase · يُستبعد الشركاء والمديرون.']
            if customer is not None:
                lines.append('العميل: <code>' + str(customer) + '</code>')
            else:
                lines.append('عدد العملاء: ' + views.count(page['customer_count']))
            if kind == 'codes':
                lines.extend(['شحن نقدي بالأكواد: ' + egp(page['cash_credit_minor']) + ' جنيه',
                              'شحن ترويجي بالأكواد: ' + egp(page['promotional_credit_minor']) + ' جنيه',
                              'القيم هي مبالغ الشحن المسجلة، وليست الرصيد المتبقي.'])
            else:
                lines.extend(['حسابات مجانية: ' + views.count(page['free_account_checks']),
                              'خدمات/محافظ مجانية: ' + views.count(page['free_service_checks']),
                              'فحوص منجزة تمت تسويتها بلا دفع نقدي من العميل، من أي مصدر.'])
            rows = []
            for item in page['items']:
                target = user_id(item['telegram_user_id'])
                if target is None or target in admin_ids(self) or customer is not None and target != customer:
                    raise ValueError('Unexpected report customer')
                if customer is None:
                    name = views.safe(item.get('telegram_username') or 'عميل', 35)
                    lines.append('\n' + name + ' · <code>' + str(target) + '</code>')
                    if kind == 'codes':
                        lines.append('نقدي: ' + egp(item['cash_credit_minor']) + ' · ترويجي: ' + egp(item['promotional_credit_minor']) + ' جنيه')
                    else:
                        lines.append('حسابات: ' + views.count(item['free_account_checks']) + ' · خدمات: ' + views.count(item['free_service_checks']))
                    lines.append('آخر نشاط: ' + activity_time(item['last_activity_at']) + ' بغداد')
                    rows.append([views.button('تفاصيل العميل ' + str(target), f'cr:detail:{kind}:{target}:0')])
                elif kind == 'codes':
                    lines.append('\nالكود: <code>' + views.safe(item['code']) + '</code> · ' + activity_time(item['occurred_at']))
                    lines.append('نقدي: ' + egp(item['cash_credit_minor']) + ' · ترويجي: ' + egp(item['promotional_credit_minor']) + ' جنيه')
                else:
                    order = str(UUID(item['event_id']))
                    lines.append('\nطلب <code>' + order + '</code> · ' + activity_time(item['occurred_at']))
                    lines.append('حسابات: ' + views.count(item['free_account_checks']) + ' · خدمات: ' + views.count(item['free_service_checks']))
                    lines.append('سعر صفر: ' + views.count(item['zero_price_checks']) + ' · خصم كامل: ' + views.count(item['full_discount_checks']) + ' · رصيد ترويجي كامل: ' + views.count(item['promotional_checks']))
                    rows.append([views.button('عرض الطلب ' + order[:8], 'oh:cinspect:' + order)])
            if not page['items']:
                lines.append('\nلا توجد نتائج في هذه الصفحة.')
            route = f'cr:list:{kind}' if customer is None else f'cr:detail:{kind}:{customer}'
            navigation = []
            if offset:
                navigation.append(views.button('السابق', f'{route}:{max(0, offset-6)}'))
            if offset + 6 < page['total']:
                navigation.append(views.button('التالي', f'{route}:{offset+6}'))
            if navigation:
                rows.append(navigation)
            rows.append([views.button('تحديث', f'{route}:{offset}')])
            if customer is not None:
                rows.append([views.button('قائمة العملاء', f'cr:list:{kind}:0')])
            rows.append([views.button('‹ تقارير العملاء', 'menu:customers'), views.button('الرئيسية', 'home')])
            self._show_panel(actor, views.Panel('\n'.join(lines), rows), message_id)
        except (CommerceUnavailable, ValueError, KeyError, TypeError, OverflowError):
            self._show_panel(actor, views.Panel('<b>تعذر عرض تقرير العملاء</b>\n\nتحقق من صلاحية المدير وتطبيق ترحيل التقارير، ثم أعد المحاولة. لم نغيّر الطلبات أو الأرصدة.',
                [[views.button('الرئيسية', 'home')]]), message_id)

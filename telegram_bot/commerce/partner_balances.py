"""Signed partner balances and the recorded reason for advance payouts."""
from telegram_bot import views
from .checkout import egp


def partner_balance_text(profile):
    signed=profile.get('account_balance_minor',profile['available_minor'])
    withdrawable=profile.get('withdrawable_minor',max(0,profile['available_minor']))
    text='رصيد الحساب: <b>'+egp(signed)+' جنيه</b>\nالمتاح للسحب: <b>'+egp(withdrawable)+' جنيه</b>'
    debt=profile.get('advance_outstanding_minor',0)
    if debt>0:
        text+='\nدفعات مقدمة لم تُغطَّ بعد: '+egp(debt)+' جنيه\nتغطي الأرباح التالية هذا المبلغ قبل أن تصبح متاحة للسحب.'
        for item in profile.get('outstanding_advances',[]):
            text+='\nالسبب: '+views.safe(item['reason'],500)+' · المتبقي '+egp(item['outstanding_minor'])+' جنيه'
    return text


def advance_receipt_text(advance):
    if not advance:return ''
    return ('\nمن الأرباح المتاحة: '+egp(advance['earned_paid_minor'])+' جنيه'
            '\nالزيادة المسجلة دفعة مقدمة: '+egp(advance['amount_minor'])+' جنيه'
            '\nالسبب: '+views.safe(advance['reason'],500)+
            '\nرصيد الحساب بعد هذا الصرف: <b>'+egp(advance['account_after_minor'])+' جنيه</b>'
            '\nتُغطّى الدفعة المقدمة من الأرباح التالية. تأكيد الاستلام لا يكرر الخصم.')

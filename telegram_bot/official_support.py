"""The owner-designated support destination. Safe to bundle independently."""
from html import escape

CHANNEL_ONLY = True
SUPPORT_USERNAME = 'zainchecker'
SUPPORT_URL = 'https://t.me/zainchecker?direct'


def support_button(label='التواصل مع الدعم'):
    return {'text':label,'url':SUPPORT_URL}


def support_text(order=None):
    text=('<b>الدعم الرسمي لتشيك زين</b>\n\n'
          'مراسلة @zainchecker هي وسيلة الدعم الوحيدة المعتمدة. '
          'اضغط الزر لفتح محادثة خاصة مع مشرفي القناة.\n'
          'أرسل تفاصيل المشكلة ورقم الطلب إن وُجد.')
    if order:text+='\n\nرقم الطلب: <code>'+escape(str(order)[:128])+'</code>'
    return text

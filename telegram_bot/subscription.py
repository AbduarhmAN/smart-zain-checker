"""Gate private customer inputs before any workflow consumes them."""
from . import views
from . import subscription_policy as policy


class SubscriptionMixin:
    def _subscription_status(self, actor):
        return policy.membership(self._api_call, actor)

    def _subscription_gate(self, actor, *, message_id=None, callback_id=None, verify=False):
        status = self._subscription_status(actor)
        if callback_id and (status != 'member' or verify):
            text = ('✅ تم التحقق من الاشتراك.' if status == 'member' else
                    'اشترك في القناة ثم اضغط تحقق.' if status == 'missing' else
                    'تعذّر التحقق الآن. حاول مرة أخرى أو تواصل مع الدعم.')
            self._api_call('answerCallbackQuery', {'callback_query_id': callback_id,
                'text': text, 'show_alert': status != 'member'})
        if status != 'member':
            # Send a separate panel: retain old payment/order details and waiting input.
            self._show_panel(actor, views.Panel(policy.prompt(status), policy.rows()))
            return False
        if verify:
            # Never replay an old purchase, payment approval, file or redeem action.
            self._dispatch_ui('home', actor, message_id)
            return False
        return True

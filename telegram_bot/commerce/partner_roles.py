"""A retained partner ledger is not an active operational role."""
from telegram_bot.authorization import admin_ids

def active_partner(profile):
    if profile.get('is_partner') is not True or profile.get('partnership_ended_at'):return False
    if 'partnership_active' in profile:return profile['partnership_active'] is True
    state=profile.get('partnership_state',profile.get('state'))
    return state in (None,'active')

class PartnerRoleMixin:
    def _revoke_partner_role(self,user):
        with self._ui_lock:
            for name in ('_known_partner_ids','_partner_menu_ready'):
                getattr(self,name,set()).discard(user)
            campaigns=getattr(self,'_campaign_reviews',{})
            removed={k for k,v in campaigns.items() if v.get('actor')==user and v.get('partner')}
            self._campaign_reviews={k:v for k,v in campaigns.items() if k not in removed}
            withdrawals=getattr(self,'_withdrawal_reviews',{})
            self._withdrawal_reviews={k:v for k,v in withdrawals.items() if v.get('user')!=user}
            waiting_inputs=getattr(self,'_waiting_user_inputs',{})
            waiting=waiting_inputs.get(user,{})
            if waiting.get('action') in {'commerce_withdraw_amount','commerce_withdraw_destination'} or waiting.get('token') in removed:
                waiting_inputs.pop(user,None)

    def _reset_customer_menu(self,user):
        # Delete only this user's scoped override; the global customer menu
        # then applies. Independently configured administrator roles stay intact.
        if user not in admin_ids(self):
            with self._ui_lock:
                if not hasattr(self,'_customer_menu_pending'):self._customer_menu_pending=set()
                self._customer_menu_pending.add(user)
            response=self._api_call('deleteMyCommands',{'scope':{'type':'chat','chat_id':user}})
            if isinstance(response,dict) and response.get('ok') is True:
                with self._ui_lock:self._customer_menu_pending.discard(user)
            return response

    def _has_active_partner_role(self,user):
        try:
            profile=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':user})
        except Exception:return False
        enabled=active_partner(profile)
        if not enabled:self._revoke_partner_role(user)
        return enabled

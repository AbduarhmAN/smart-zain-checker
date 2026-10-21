"""A retained partner ledger is not an active operational role."""
from telegram_bot.authorization import admin_ids
from telegram_bot.identity import TelegramIdentityMixin

def active_partner(profile):
    if profile.get('is_partner') is not True or profile.get('partnership_ended_at'):return False
    if 'partnership_active' in profile:return profile['partnership_active'] is True
    state=profile.get('partnership_state',profile.get('state'))
    return state in (None,'active')

class PartnerRoleMixin(TelegramIdentityMixin):
    def _remember_partner_sheet_access(self,user,enabled):
        with self._ui_lock:
            if not hasattr(self,'_partner_sheet_access_flags'):self._partner_sheet_access_flags={}
            self._partner_sheet_access_flags[user]=enabled is not False

    def _partner_sheet_access_allowed(self,user):
        try:
            profile=self._commerce().rpc('partner_sheet_context',{'p_telegram_user_id':user})
            self._remember_partner_sheet_access(user,profile.get('partner_sheet_enabled',True))
            return active_partner(profile) and profile.get('partner_sheet_enabled',True) is not False
        except Exception:return False

    def _grant_partner_role(self,user):
        with self._ui_lock:
            getattr(self,'_known_partner_ids',set()).add(user)

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
            self._remember_partner_sheet_access(user,profile.get('partner_sheet_enabled',True))
        except Exception:return False
        enabled=active_partner(profile)
        if not enabled:self._revoke_partner_role(user)
        return enabled

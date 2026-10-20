"""One strict authorization view for commands, callbacks, forms and notifications."""
import re


def user_id(value):
    if type(value) is str:
        value=value.strip()
        if not re.fullmatch(r'[1-9][0-9]{0,15}',value):return None
        value=int(value)
    return value if type(value) is int and 0<value<2**52 else None


def admin_ids(owner):
    current=getattr(owner,'current_authorized_ids',None)
    if current is not None:return {value for item in current if (value:=user_id(item)) is not None}
    # Non-runner adapters may supply an explicit authority set; production runners
    # always have current_authorized_ids and never reach the cached-set fallback.
    values=set(getattr(owner,'authorized_ids',set()))
    values.add(getattr(owner,'authorized_id',None))
    return {value for item in values if (value:=user_id(item)) is not None}

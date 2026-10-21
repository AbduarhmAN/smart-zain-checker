"""Opt-in service adapter; never touches the account backend or finance state."""
import os
from engine_v3.proxy_scheduler import CentralizedCooldownScheduler,RouteNotReady,route_key
from engine_v3.low_bandwidth_checker import LowBandwidthChecker
from workers.service_transport import set_ip_service_cooldown

SCHEDULER=CentralizedCooldownScheduler()


def enabled(proxy_url=None):
    if os.environ.get('ZAIN_SERVICE_ENGINE','browser')!='low_bandwidth':return False
    if not proxy_url and os.environ.get('ZAIN_SERVICE_HTTP_ALLOW_DIRECT')!='1':return False
    allowed=os.environ.get('ZAIN_SERVICE_HTTP_ROUTES','').split(',')
    try:return not any(allowed) or route_key(proxy_url) in allowed
    except ValueError:return False


def query_service(number,proxy_url=None,timeout=20,on_verification=None):
    try:key=route_key(proxy_url)
    except ValueError:return 'error',None,'Invalid HTTP service configuration'
    try:SCHEDULER.acquire_wait(key)
    except RouteNotReady as exc:
        set_ip_service_cooldown(key,exc.seconds)
        return ('verification_required' if exc.blocked else 'network_error'),None,'Service route is waiting for its cooldown'
    blocked=False;retry=60
    try:
        try:result=LowBandwidthChecker.check_service_number(number,proxy_url,timeout)
        except ImportError:return 'error',None,'Optional HTTP service dependency is unavailable'
        except ValueError:return 'error',None,'Invalid HTTP service configuration'
        retry=result['retry_after_seconds'];status=result['status']
        if status=='SUCCESS':return 'ok',float(result['amount']),'Verified HTTP service amount'
        if status in ('WAF_REJECTED','CAPTCHA'):
            blocked=True
            if on_verification:on_verification('Service route requires operator review')
            return 'verification_required',None,'Service route requires operator review'
        return ('network_error' if status in ('ERROR','RATE_LIMITED') else 'unknown_response'),None,'HTTP service result is not verified'
    finally:
        SCHEDULER.release(key,retry_after=retry,blocked=blocked)
        set_ip_service_cooldown(key,SCHEDULER.retry_after(key))

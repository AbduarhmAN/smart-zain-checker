"""Private bridge capability and per-worker extension provisioning; no cleanup."""
from __future__ import annotations
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from urllib.parse import urlsplit
from uuid import uuid4
from server.access import loopback


def capability(root):
    folder=Path(root).resolve()/'.bridge-runtime';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'authority.json'
    if not path.exists():
        pending=folder/('authority_'+str(uuid4())+'.pending')
        with pending.open('x',encoding='utf-8') as stream:
            json.dump({'version':1,'token':secrets.token_hex(32)},stream);stream.flush();os.fsync(stream.fileno())
        try:os.link(pending,path)
        except FileExistsError:pass
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('version')!=1 or not isinstance(data.get('token'),str) or not re.fullmatch(r'[a-f0-9]{64}',data['token']):
        raise ValueError('Bridge authority requires owner review')
    return data['token']


def origin(handler):
    peer=getattr(handler,'client_address',('',0))[0]
    if not loopback(peer):return None
    host=handler.headers.get('Host','')
    try:
        url=urlsplit('http://'+host)
        if url.hostname not in {'localhost','127.0.0.1','::1'} or (url.port or 80)!=handler.server.server_port:
            return None
        if url.username or url.password or url.path or url.query or url.fragment:return None
        if hasattr(handler.headers,'get_all') and len(handler.headers.get_all('Host') or [])!=1:return None
        value=handler.headers.get('Origin')
        if value is None:return ''
        parsed=urlsplit(value)
        if parsed.scheme!='chrome-extension' or not re.fullmatch(r'[a-p]{32}',parsed.netloc) or parsed.path or parsed.query or parsed.fragment:
            return None
        return value
    except (ValueError,AttributeError):return None


def authorized(handler):
    selected=origin(handler)
    if selected is None:return False
    expected=getattr(handler,'bridge_token',None)
    given=handler.headers.get('X-Zain-Bridge-Token','')
    return (isinstance(expected,str) and re.fullmatch(r'[a-f0-9]{64}',expected) is not None
        and isinstance(given,str) and re.fullmatch(r'[a-f0-9]{64}',given) is not None and hmac.compare_digest(expected,given))


def provision_extension(root,source,*,bridge_port=8766,worker_id=None):
    source=Path(source).resolve()
    if not (source/'manifest.json').is_file():raise ValueError('Managed extension source missing')
    token=capability(root)
    target=Path(root).resolve()/'.bridge-runtime'/('extension_'+str(uuid4()))
    shutil.copytree(source,target)
    script='globalThis.ZAIN_BRIDGE_TOKEN = '+json.dumps(token)+';\n'
    script+='globalThis.ZAIN_BRIDGE_URL = '+json.dumps('http://127.0.0.1:'+str(bridge_port))+';\n'
    script+='globalThis.ZAIN_BRIDGE_WORKER_ID = '+json.dumps(worker_id)+';\n'
    (target/'bridge-config.js').write_text(script,encoding='utf-8')
    manifest_path=target/'manifest.json';manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    permission='http://127.0.0.1:'+str(bridge_port)+'/*'
    if permission not in manifest.get('host_permissions',[]):manifest.setdefault('host_permissions',[]).append(permission)
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return target

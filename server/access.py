"""Local administrative HTTP boundary; no untrusted file or browser origins."""
from __future__ import annotations
import ipaddress
from pathlib import Path,PureWindowsPath
from urllib.parse import urlsplit

MAX_JSON_BODY=70*1024*1024
MAX_UPLOAD_BYTES=50*1024*1024


def loopback(value):
    try:return ipaddress.ip_address(value).is_loopback
    except ValueError:return False


def local_request(handler):
    peer=getattr(handler,'client_address',('',0))[0]
    if not loopback(peer):return False
    headers=handler.headers
    if hasattr(headers,'get_all') and len(headers.get_all('Host') or [])!=1:return False
    host=headers.get('Host','')
    if not host or any(char in host for char in ('/','\\','@','#','?',' ','\t','\r','\n')):return False
    try:
        target=urlsplit('http://'+host)
        port=target.port or 80
        actual=getattr(getattr(handler,'server',None),'server_port',None)
        if target.hostname not in {'localhost','127.0.0.1','::1'} or port!=actual:return False
        origin=headers.get('Origin')
        if origin:
            source=urlsplit(origin)
            if source.scheme!='http' or source.hostname!=target.hostname or (source.port or 80)!=port:
                return False
            if source.username or source.password or source.path or source.query or source.fragment:return False
    except (ValueError,AttributeError):return False
    if headers.get('Sec-Fetch-Site') in {'cross-site','same-site'}:return False
    return True


def workspace_file(root,value,*,suffixes=None,flat=False,private=False):
    if not isinstance(value,(str,Path)):raise ValueError('Select a workspace file')
    value=str(value)
    if not value or '\x00' in value or '\\' in value or ':' in value:raise ValueError('Invalid workspace path')
    supplied=Path(value)
    if supplied.is_absolute() or PureWindowsPath(value).drive or any(p in {'.','..'} for p in supplied.parts):
        raise ValueError('File must be inside the workspace')
    if not private and any(part.startswith('.') for part in supplied.parts):raise ValueError('Private workspace files are not public assets')
    if flat and len(supplied.parts)!=1:raise ValueError('Choose a file in the workspace folder')
    root=Path(root).resolve();target=(root/supplied).resolve()
    if not target.is_relative_to(root) or target==root:raise ValueError('File is outside the workspace')
    if suffixes is not None and target.suffix.lower() not in suffixes:raise ValueError('Unsupported file type')
    return target

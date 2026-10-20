"""Windows DPAPI storage: a server credential encrypted for the current user.

Linux deployments use ZAIN_COMMERCE_SERVICE_KEY in their service environment.
An encrypted fallback may live in the ignored private workspace runtime folder.
No plaintext keys are returned by diagnostics or written to files.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class CredentialUnavailable(RuntimeError):
    """Fixed diagnostic codes only; never carry credential or OS error text."""
    def __init__(self,reason,*,windows_code=None):
        super().__init__('Commerce credential unavailable')
        self.reason=reason
        self.windows_code=windows_code


class _Blob(ctypes.Structure):
    _fields_ = [('size',wintypes.DWORD),('data',ctypes.POINTER(ctypes.c_ubyte))]


def secret_path() -> Path:
    root = os.environ.get('LOCALAPPDATA')
    if not root:
        raise CredentialUnavailable('credential_profile_missing')
    return Path(root)/'SmartZainChecker'/'commerce-service.dpapi'


def workspace_secret_path() -> Path:
    # Anchor to the application, never its shell's current working directory.
    return Path(__file__).resolve().parents[2]/'.commerce'/'commerce-service.dpapi'


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('DPAPI credentials require Windows; use service environment on this platform')
    buffer = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    crypt = ctypes.WinDLL('crypt32',use_last_error=True)
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,
                         ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN, current-user scope (no machine scope flag).
    if not function(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing)):
        raise CredentialUnavailable('credential_decryption_failed' if decrypt else 'credential_encryption_failed',
                                    windows_code=ctypes.get_last_error())
    try:
        return ctypes.string_at(outgoing.data,outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data,wintypes.HLOCAL))


def save_service_key(key: str) -> None:
    if not isinstance(key,str) or len(key) < 20:
        raise ValueError('Invalid service credential')
    encrypted = _crypt(key.encode('utf-8'),decrypt=False)
    path = secret_path()
    path.parent.mkdir(parents=True,exist_ok=True)
    # A staged file is retained if replacement fails; no cleanup/delete operation.
    import secrets
    pending = path.with_name(path.name+'.'+secrets.token_hex(6)+'.pending')
    with pending.open('xb') as stream:
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending,path)


def load_service_key() -> str:
    key = os.environ.get('ZAIN_COMMERCE_SERVICE_KEY') or os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
    if key:
        return key
    path = secret_path()
    try:
        encrypted=path.read_bytes()
    except FileNotFoundError:
        # Some launch environments cannot see the same AppData file. This
        # fallback contains only the original Windows-user-encrypted bytes.
        try:
            encrypted=workspace_secret_path().read_bytes()
        except FileNotFoundError:
            raise CredentialUnavailable('credential_file_missing') from None
        except PermissionError:
            raise CredentialUnavailable('credential_file_denied') from None
        except OSError:
            raise CredentialUnavailable('credential_file_unreadable') from None
    except PermissionError:
        raise CredentialUnavailable('credential_file_denied') from None
    except OSError:
        raise CredentialUnavailable('credential_file_unreadable') from None
    if not encrypted:
        raise CredentialUnavailable('credential_file_empty')
    try:
        key=_crypt(encrypted,decrypt=True).decode('utf-8')
    except UnicodeError:
        raise CredentialUnavailable('credential_invalid_encoding') from None
    if not key.strip():
        raise CredentialUnavailable('credential_empty')
    return key

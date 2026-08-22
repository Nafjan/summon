"""Best-effort Windows Credential Manager lookup for local API credentials.

This module is deliberately narrow: it reads only the credential target owned by
Summon's OpenRouter integration, never enumerates the credential store, and never
logs the credential value. Environment variables remain the primary and portable
credential source; the Windows store is a local convenience fallback.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


_OPENROUTER_TARGET = "summonOpenRouter"
_CRED_TYPE_GENERIC = 1
_CRED_TYPE_DOMAIN_PASSWORD = 2


class _Credential(ctypes.Structure):
    """The subset/layout of Windows CREDENTIALW needed by CredReadW."""

    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _decode_blob(blob: bytes) -> str | None:
    """Decode a cmdkey password without ever returning an empty/whitespace value."""
    if not blob:
        return None
    # cmdkey commonly stores passwords as UTF-16LE. Detect that representation
    # without guessing based on the actual secret contents; generic credentials
    # created by other tools may be UTF-8/ASCII instead.
    if len(blob) % 2 == 0 and blob[1::2] == b"\x00" * (len(blob) // 2):
        try:
            value = blob.decode("utf-16-le")
        except UnicodeDecodeError:
            value = ""
    else:
        try:
            value = blob.decode("utf-8")
        except UnicodeDecodeError:
            value = ""
    value = value.rstrip("\x00").strip()
    return value or None


def read_credential(target: str) -> tuple[str | None, str | None]:
    """Return ``(secret, source)`` for a named Windows credential.

    Generic credentials are preferred. Domain-password entries are attempted as
    a compatibility fallback, but Windows may expose their blob as empty; in
    that case this function safely returns ``(None, None)``. Non-Windows hosts
    and any Credential Manager failure are a normal no-op.
    """
    if os.name != "nt" or not isinstance(target, str) or not target:
        return None, None
    try:
        advapi = ctypes.WinDLL("Advapi32.dll")
        advapi.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_Credential)),
        ]
        advapi.CredReadW.restype = wintypes.BOOL
        advapi.CredFree.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError, TypeError):
        return None, None

    for credential_type in (_CRED_TYPE_GENERIC, _CRED_TYPE_DOMAIN_PASSWORD):
        ptr = ctypes.POINTER(_Credential)()
        try:
            ok = advapi.CredReadW(target, credential_type, 0, ctypes.byref(ptr))
            if not ok or not ptr:
                continue
            credential = ptr.contents
            raw = (
                ctypes.string_at(credential.CredentialBlob,
                                 int(credential.CredentialBlobSize))
                if credential.CredentialBlob and credential.CredentialBlobSize
                else b""
            )
            value = _decode_blob(raw)
            # Drop the local byte copy before returning. The string is needed
            # only for the immediate Authorization header and is never logged.
            del raw
            if value:
                return value, "windows_credential"
        except (OSError, TypeError, ValueError, UnicodeError):
            pass
        finally:
            if ptr:
                try:
                    advapi.CredFree(ptr)
                except (OSError, TypeError):
                    pass
    return None, None


def resolve_openrouter_api_key() -> tuple[str | None, str | None]:
    """Read the opt-in local OpenRouter credential target, if available."""
    return read_credential(_OPENROUTER_TARGET)

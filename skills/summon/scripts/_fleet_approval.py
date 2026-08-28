"""Authenticated, provider-inert fleet approval recording.

This module deliberately records authority without selecting or executing a
candidate.  The private store is authenticated with a separate local key file
key and every approval is bound to one sealed fleet, compiled plan, project,
catalog, lane, operation, expiry, and exact lane authority.  Public projections
omit the store identity, actor identity, MACs, paths, and key material.

The local OS account is the trust boundary.  This protects against accidental
cross-store copying, foreign files, and unauthenticated edits; it is not a
defence against an attacker who controls that account or rolls back that
account's authenticated store and key together.
"""

from __future__ import annotations

import datetime as _dt
import ctypes
import functools
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ctypes import wintypes

import _evidence
import _fleet_compile
from _job_control import ControlBusyError, _exclusive_control_lock
from _jobs import _atomic_write_json


STORE_SCHEMA = "summon.fleet-approval-store/v1"
APPROVAL_SCHEMA = "summon.fleet-approval/v1"
DEFAULT_STORE_FILE = os.path.join(
    os.path.expanduser("~"), ".agents", "summon", "fleet-approvals.json")
MAX_STORE_BYTES = 4 * 1024 * 1024
MAX_COLLECTION_RECORDS = 4096
# A valid approval currently contains fewer than 128 JSON keys/values and a
# revocation fewer than 16. Keep the parser bounded while sizing it for both
# advertised collections at their structural maxima. The byte ceiling remains
# an independent, usually tighter, bound.
MAX_APPROVAL_PARSE_ITEMS = 128
MAX_REVOCATION_PARSE_ITEMS = 16
MAX_STORE_PARSE_ITEMS = (
    64
    + MAX_COLLECTION_RECORDS * (2 + MAX_APPROVAL_PARSE_ITEMS)
    + MAX_COLLECTION_RECORDS * (2 + MAX_REVOCATION_PARSE_ITEMS)
)
REVOCATION_RESERVE_BYTES = 512
MAX_GENERATION = (1 << 63) - 1
MIN_EXPIRY_SECONDS = 60
MAX_EXPIRY_SECONDS = 30 * 24 * 60 * 60
CLOCK_SKEW_SECONDS = 300
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_ID = _SHA256
_LANE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WINDOWS_SID = re.compile(r"^S-[0-9]+(?:-[0-9]+)+$")
_PROCESS_STORE_LOCK = threading.RLock()


class ApprovalBusyError(_evidence.EvidenceError):
    """A typed transient collision in the private approval-store lock."""


def store_path() -> str:
    raw = os.environ.get("SUMMON_FLEET_APPROVAL_STORE") or DEFAULT_STORE_FILE
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        raise _evidence.EvidenceError(
            "SUMMON_FLEET_APPROVAL_STORE must be an absolute path")
    return os.path.abspath(expanded)


def key_path() -> str:
    raw = os.environ.get("SUMMON_FLEET_APPROVAL_KEY")
    if not raw:
        return store_path() + ".key"
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(expanded):
        raise _evidence.EvidenceError(
            "SUMMON_FLEET_APPROVAL_KEY must be an absolute path")
    return os.path.abspath(expanded)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _timestamp(value: _dt.datetime) -> str:
    return value.astimezone(_dt.timezone.utc).replace(
        microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, field: str) -> _dt.datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise _evidence.EvidenceError(f"{field} must be a UTC timestamp")
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _evidence.EvidenceError(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != _dt.timedelta(0):
        raise _evidence.EvidenceError(f"{field} must be a UTC timestamp")
    return parsed


def parse_expiry(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([smhd])", str(value or ""))
    if not match:
        raise _evidence.EvidenceError(
            "fleet approval expiry must use an explicit unit such as 24h")
    amount = int(match.group(1))
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    seconds = amount * multiplier
    if not MIN_EXPIRY_SECONDS <= seconds <= MAX_EXPIRY_SECONDS:
        raise _evidence.EvidenceError(
            "fleet approval expiry must be between 60s and 30d")
    return seconds


def _is_reparse(path: str) -> bool:
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet approval private path could not be inspected") from exc
    attrs = getattr(value, "st_file_attributes", 0)
    return stat.S_ISLNK(value.st_mode) or bool(
        attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _reject_reparse_ancestors(path: str) -> None:
    """Reject an existing link/junction at any component of a private path."""
    current = Path(os.path.abspath(path))
    chain = [current, *current.parents]
    for component in reversed(chain):
        if _is_reparse(str(component)):
            raise _evidence.EvidenceError(
                "fleet approval private path refuses a reparse-point ancestor")


def _regular_single_link(path: str, field: str) -> os.stat_result:
    if _is_reparse(path):
        raise _evidence.EvidenceError(f"fleet approval {field} refuses a reparse point")
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _evidence.EvidenceError(
            f"fleet approval {field} could not be read") from exc
    if not stat.S_ISREG(value.st_mode) or getattr(value, "st_nlink", 1) != 1:
        raise _evidence.EvidenceError(
            f"fleet approval {field} must be a single-link regular file")
    return value


@functools.lru_cache(maxsize=1)
def _effective_user_sid() -> str:
    """Return the current process token's user SID without executable lookup."""
    if os.name != "nt":
        raise _evidence.EvidenceError(
            "fleet approval cannot determine a safe local owner identity")

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [
            ("Sid", ctypes.c_void_p),
            ("Attributes", wintypes.DWORD),
        ]

    class TokenUser(ctypes.Structure):
        _fields_ = [("User", SidAndAttributes)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    sid_text = wintypes.LPWSTR()
    try:
        if not advapi.OpenProcessToken(
                kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise _evidence.EvidenceError(
                "fleet approval cannot determine a safe local owner identity")

        needed = wintypes.DWORD()
        ctypes.set_last_error(0)
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if ctypes.get_last_error() != 122 or needed.value < ctypes.sizeof(TokenUser):
            raise _evidence.EvidenceError(
                "fleet approval cannot determine a safe local owner identity")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(
                token, 1, buffer, needed.value, ctypes.byref(needed)):
            raise _evidence.EvidenceError(
                "fleet approval cannot determine a safe local owner identity")
        user = ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents
        if not user.User.Sid or not advapi.ConvertSidToStringSidW(
                user.User.Sid, ctypes.byref(sid_text)):
            raise _evidence.EvidenceError(
                "fleet approval cannot determine a safe local owner identity")
        value = sid_text.value or ""
        if not _WINDOWS_SID.fullmatch(value):
            raise _evidence.EvidenceError(
                "fleet approval cannot determine a safe local owner identity")
        return value
    except _evidence.EvidenceError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError(
            "fleet approval cannot determine a safe local owner identity") from exc
    finally:
        if sid_text:
            kernel.LocalFree(sid_text)
        if token:
            kernel.CloseHandle(token)


def _windows_acl_snapshot(path: str) -> dict:
    """Read owner, DACL protection, and simple file ACEs without a shell."""
    if os.name != "nt":
        raise _evidence.EvidenceError(
            "fleet approval could not verify a private ACL")

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetAce.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    def sid_text(pointer: ctypes.c_void_p | int) -> str:
        rendered = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(
                ctypes.c_void_p(pointer), ctypes.byref(rendered)):
            raise _evidence.EvidenceError(
                "fleet approval could not verify a private ACL")
        try:
            value = rendered.value
            if not value or not _WINDOWS_SID.fullmatch(value):
                raise _evidence.EvidenceError(
                    "fleet approval could not verify a private ACL")
            return value
        finally:
            kernel.LocalFree(ctypes.cast(rendered, ctypes.c_void_p))

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    try:
        code = advapi.GetNamedSecurityInfoW(
            os.path.abspath(path), 1, 0x00000001 | 0x00000004,
            ctypes.byref(owner), None, ctypes.byref(dacl), None,
            ctypes.byref(descriptor))
        if code != 0 or not descriptor.value or not owner.value or not dacl.value:
            raise _evidence.EvidenceError(
                "fleet approval could not verify a private ACL")

        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not advapi.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise _evidence.EvidenceError(
                "fleet approval could not verify a private ACL")
        info = AclSizeInformation()
        if not advapi.GetAclInformation(
                dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise _evidence.EvidenceError(
                "fleet approval could not verify a private ACL")

        rules = []
        for index in range(info.AceCount):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise _evidence.EvidenceError(
                    "fleet approval could not verify a private ACL")
            header = AceHeader.from_address(ace.value)
            if header.AceType not in (0, 1) or header.AceSize < 12:
                rules.append({
                    "sid": None,
                    "type": f"Other:{header.AceType}",
                    "rights": "unknown",
                    "inherited": bool(header.AceFlags & 0x10),
                })
                continue
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            rules.append({
                "sid": sid_text(ace.value + 8),
                "type": "Allow" if header.AceType == 0 else "Deny",
                "rights": ("FullControl" if mask & 0x001F01FF == 0x001F01FF
                           else f"0x{mask:08x}"),
                "inherited": bool(header.AceFlags & 0x10),
            })
        return {
            "protected": bool(control.value & 0x1000),
            "owner": sid_text(owner.value),
            "rules": rules,
        }
    except _evidence.EvidenceError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError(
            "fleet approval could not verify a private ACL") from exc
    finally:
        if descriptor.value:
            kernel.LocalFree(descriptor)


def _verify_windows_acl(path: str, sid: str) -> None:
    value = _windows_acl_snapshot(path)
    rules = value.get("rules")
    if isinstance(rules, dict):
        rules = [rules]
    if (value.get("protected") is not True or value.get("owner") != sid
            or not isinstance(rules, list) or len(rules) != 1):
        raise _evidence.EvidenceError(
            "fleet approval private ACL is not owner-only")
    rule = rules[0]
    if (not isinstance(rule, dict) or rule.get("sid") != sid
            or rule.get("type") != "Allow" or rule.get("inherited") is not False
            or "FullControl" not in str(rule.get("rights", ""))):
        raise _evidence.EvidenceError(
            "fleet approval private ACL is not owner-only")


def _apply_windows_acl(path: str, *, directory: bool) -> None:
    sid = _effective_user_sid()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi.InitializeAcl.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
    ]
    advapi.InitializeAcl.restype = wintypes.BOOL
    advapi.AddAccessAllowedAceEx.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, ctypes.c_void_p,
    ]
    advapi.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
    ]
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    sid_pointer = ctypes.c_void_p()
    try:
        if not advapi.ConvertStringSidToSidW(sid, ctypes.byref(sid_pointer)):
            raise _evidence.EvidenceError(
                "fleet approval could not secure a private ACL")
        acl_buffer = ctypes.create_string_buffer(1024)
        acl_pointer = ctypes.cast(acl_buffer, ctypes.c_void_p)
        if not advapi.InitializeAcl(acl_pointer, len(acl_buffer), 2):
            raise _evidence.EvidenceError(
                "fleet approval could not secure a private ACL")
        inheritance = 0x01 | 0x02 if directory else 0
        if not advapi.AddAccessAllowedAceEx(
                acl_pointer, 2, inheritance, 0x001F01FF, sid_pointer):
            raise _evidence.EvidenceError(
                "fleet approval could not secure a private ACL")
        # The verifier promises both an owner-only DACL and current-user
        # ownership.  A freshly-created directory can inherit an Administrators
        # or service-account owner on hosted Windows runners, so applying only
        # the DACL leaves the postcondition false even though the ACE is right.
        # Set the owner and protected DACL atomically to the same SID.
        code = advapi.SetNamedSecurityInfoW(
            os.path.abspath(path), 1,
            0x00000001 | 0x00000004 | 0x80000000,
            sid_pointer, None, acl_pointer, None)
        if code != 0:
            raise _evidence.EvidenceError(
                "fleet approval could not secure a private ACL")
    except _evidence.EvidenceError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _evidence.EvidenceError(
            "fleet approval could not secure a private ACL") from exc
    finally:
        if sid_pointer.value:
            kernel.LocalFree(sid_pointer)
    _verify_windows_acl(path, sid)


def _verify_posix_private(path: str, *, directory: bool) -> None:
    try:
        value = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet approval private path could not be verified") from exc
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if (not expected or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o077):
        raise _evidence.EvidenceError(
            "fleet approval private path is not owner-only")


def _verify_private(path: str, *, directory: bool) -> None:
    if os.name == "nt":
        _verify_windows_acl(path, _effective_user_sid())
    else:
        _verify_posix_private(path, directory=directory)


def _claimable_private_root(root: str) -> bool:
    """Only claim an empty directory; filenames cannot prove prior ownership."""
    try:
        entries = set(os.listdir(root))
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet approval private directory could not be inspected") from exc
    return not entries


def _secure_private_root(root: str) -> None:
    _reject_reparse_ancestors(root)
    if _is_reparse(root):
        raise _evidence.EvidenceError(
            "fleet approval private directory refuses a reparse point")
    try:
        existed = os.path.exists(root)
        os.makedirs(root, exist_ok=True)
        try:
            _verify_private(root, directory=True)
        except _evidence.EvidenceError:
            if existed and not _claimable_private_root(root):
                raise _evidence.EvidenceError(
                    "fleet approval requires a dedicated empty private directory")
            if os.name == "nt":
                _apply_windows_acl(root, directory=True)
            else:
                os.chmod(root, 0o700)
        _verify_private(root, directory=True)
    except _evidence.EvidenceError:
        raise
    except (OSError, ValueError) as exc:
        raise _evidence.EvidenceError(
            "fleet approval could not secure its private directory") from exc


def _secure_file(path: str) -> None:
    try:
        _regular_single_link(path, "private file")
        if os.name != "nt":
            os.chmod(path, 0o600)
        else:
            try:
                _verify_private(path, directory=False)
            except _evidence.EvidenceError:
                _apply_windows_acl(path, directory=False)
        _verify_private(path, directory=False)
    except _evidence.EvidenceError:
        raise
    except (OSError, ValueError) as exc:
        raise _evidence.EvidenceError(
            "fleet approval could not secure a private file") from exc


def _load_key(*, create: bool) -> bytes | None:
    path = key_path()
    if not os.path.exists(path):
        if not create:
            return None
        _secure_private_root(os.path.dirname(path))
        if _is_reparse(path):
            raise _evidence.EvidenceError(
                "fleet approval key refuses a reparse point")
        raw = secrets.token_bytes(32)
        created = False
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            _secure_file(path)
            return raw
        except FileExistsError:
            pass
        except (_evidence.EvidenceError, OSError) as exc:
            if created:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if isinstance(exc, _evidence.EvidenceError):
                raise
            raise _evidence.EvidenceError(
                "fleet approval key could not be created") from exc
    _reject_reparse_ancestors(os.path.dirname(path))
    _verify_private(os.path.dirname(path), directory=True)
    value = _regular_single_link(path, "key")
    _verify_private(path, directory=False)
    if value.st_size != 32:
        raise _evidence.EvidenceError("fleet approval key is malformed")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise _evidence.EvidenceError("fleet approval key could not be read") from exc
    if len(raw) != 32:
        raise _evidence.EvidenceError("fleet approval key is malformed")
    return raw


def _mac(key: bytes, value: dict) -> str:
    return hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def _unsigned(value: dict) -> dict:
    return {name: item for name, item in value.items() if name != "mac"}


def _new_store(key: bytes, now: _dt.datetime) -> dict:
    body = {
        "schema": STORE_SCHEMA,
        "store_id": secrets.token_hex(32),
        "generation": 0,
        "last_seen_at": _timestamp(now),
        "approvals": {},
        "revocations": {},
    }
    return {**body, "mac": _mac(key, body)}


def _validate_authority(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "permission_ceiling", "data_boundary", "corrective", "spend"}:
        raise _evidence.EvidenceError("fleet approval authority is malformed")
    # Reuse the public fleet compiler instead of maintaining a subtly different
    # authority vocabulary (and without reaching into _evidence internals).
    validated = _fleet_compile.build_draft(
        lane="approval", seats=["placeholder"],
        permission_ceiling=value.get("permission_ceiling"),
        data_boundary=value.get("data_boundary"),
        corrective=value.get("corrective"), spend=value.get("spend"))
    constraints = _evidence.verify(validated)["lanes"][0]["constraints"]
    normalized = {
        "permission_ceiling": constraints["permission_ceiling"],
        "data_boundary": constraints["data_boundary"],
        "corrective": constraints["corrective"],
        "spend": constraints["spend"],
    }
    if normalized != value:
        raise _evidence.EvidenceError("fleet approval authority is not canonical")
    return json.loads(json.dumps(normalized))


def _validate_approval(value: Any, key: bytes, store_id: str, *,
                       now: _dt.datetime | None = None) -> dict:
    fields = {
        "schema", "approval_id", "store_id", "generation", "bindings",
        "actor", "operation", "authority", "issued_at", "expires_at", "mac",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise _evidence.EvidenceError("fleet approval record is malformed")
    if value.get("schema") != APPROVAL_SCHEMA:
        raise _evidence.EvidenceError("fleet approval record schema is unsupported")
    if value.get("store_id") != store_id:
        raise _evidence.EvidenceError("fleet approval record belongs to another store")
    if not _APPROVAL_ID.fullmatch(str(value.get("approval_id", ""))):
        raise _evidence.EvidenceError("fleet approval id is malformed")
    generation = value.get("generation")
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or not 1 <= generation <= MAX_GENERATION):
        raise _evidence.EvidenceError("fleet approval generation is malformed")
    bindings = value.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
            "fleet_sha256", "plan_sha256", "project_sha256", "catalog_sha256",
            "lane", "lane_sha256"}:
        raise _evidence.EvidenceError("fleet approval bindings are malformed")
    for field in ("fleet_sha256", "plan_sha256", "project_sha256",
                  "catalog_sha256", "lane_sha256"):
        if not _SHA256.fullmatch(str(bindings.get(field, ""))):
            raise _evidence.EvidenceError("fleet approval digest binding is malformed")
    if not _LANE.fullmatch(str(bindings.get("lane", ""))):
        raise _evidence.EvidenceError("fleet approval lane binding is malformed")
    actor = value.get("actor")
    if (not isinstance(actor, dict) or set(actor) != {"kind", "id"}
            or actor.get("kind") != "local_operator"
            or not _SHA256.fullmatch(str(actor.get("id", "")))):
        raise _evidence.EvidenceError("fleet approval actor is malformed")
    if value.get("operation") != "fleet_dispatch":
        raise _evidence.EvidenceError("fleet approval operation is unsupported")
    _validate_authority(value.get("authority"))
    issued = _parse_timestamp(value.get("issued_at"), "issued_at")
    expires = _parse_timestamp(value.get("expires_at"), "expires_at")
    if now is not None and issued > now + _dt.timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise _evidence.EvidenceError("fleet approval is not yet valid")
    duration = (expires - issued).total_seconds()
    if not MIN_EXPIRY_SECONDS <= duration <= MAX_EXPIRY_SECONDS:
        raise _evidence.EvidenceError("fleet approval expiry is outside its bounds")
    expected = _mac(key, _unsigned(value))
    if not hmac.compare_digest(str(value.get("mac", "")), expected):
        raise _evidence.EvidenceError("fleet approval authentication failed")
    identity = _approval_identity(value)
    if value["approval_id"] != hashlib.sha256(_canonical(identity)).hexdigest():
        raise _evidence.EvidenceError("fleet approval identity does not match its bindings")
    return value


def _validate_store(value: Any, key: bytes, now: _dt.datetime) -> dict:
    fields = {"schema", "store_id", "generation", "last_seen_at",
              "approvals", "revocations", "mac"}
    if not isinstance(value, dict) or set(value) != fields:
        raise _evidence.EvidenceError("fleet approval store is malformed")
    if value.get("schema") != STORE_SCHEMA:
        raise _evidence.EvidenceError("fleet approval store schema is unsupported")
    if not _SHA256.fullmatch(str(value.get("store_id", ""))):
        raise _evidence.EvidenceError("fleet approval store identity is malformed")
    generation = value.get("generation")
    if (not isinstance(generation, int) or isinstance(generation, bool)
            or not 0 <= generation <= MAX_GENERATION):
        raise _evidence.EvidenceError("fleet approval store generation is malformed")
    last_seen = _parse_timestamp(value.get("last_seen_at"), "last_seen_at")
    if last_seen > now + _dt.timedelta(seconds=CLOCK_SKEW_SECONDS):
        raise _evidence.EvidenceError(
            "fleet approval store clock rollback detected; correct the system clock "
            "and retry")
    approvals = value.get("approvals")
    revocations = value.get("revocations")
    if (not isinstance(approvals, dict)
            or len(approvals) > MAX_COLLECTION_RECORDS
            or not isinstance(revocations, dict)
            or len(revocations) > MAX_COLLECTION_RECORDS):
        raise _evidence.EvidenceError("fleet approval store collections are malformed")
    expected = _mac(key, _unsigned(value))
    if not hmac.compare_digest(str(value.get("mac", "")), expected):
        raise _evidence.EvidenceError("fleet approval store authentication failed")
    for approval_id, approval in approvals.items():
        if (not isinstance(approval, dict)
                or approval_id != str(approval.get("approval_id", ""))):
            raise _evidence.EvidenceError("fleet approval index is malformed")
        _validate_approval(approval, key, value["store_id"], now=now)
        if approval["generation"] > generation:
            raise _evidence.EvidenceError(
                "fleet approval generation exceeds its store generation")
    for approval_id, revocation in revocations.items():
        if (not _APPROVAL_ID.fullmatch(str(approval_id))
                or not isinstance(revocation, dict)
                or set(revocation) != {"generation", "revoked_at", "reason"}
                or not isinstance(revocation.get("generation"), int)
                or isinstance(revocation.get("generation"), bool)
                or not 1 <= revocation["generation"] <= MAX_GENERATION
                or revocation.get("reason") not in {"operator_revoked"}):
            raise _evidence.EvidenceError("fleet approval revocation is malformed")
        _parse_timestamp(revocation.get("revoked_at"), "revoked_at")
        if revocation["generation"] > generation:
            raise _evidence.EvidenceError(
                "fleet approval revocation exceeds its store generation")
    return value


def _read_store(key: bytes | None, now: _dt.datetime) -> dict | None:
    path = store_path()
    if not os.path.exists(path):
        return None
    if key is None:
        raise _evidence.EvidenceError("fleet approval key is unavailable")
    _reject_reparse_ancestors(os.path.dirname(path))
    _verify_private(os.path.dirname(path), directory=True)
    identity = _regular_single_link(path, "store")
    _verify_private(path, directory=False)
    if identity.st_size > MAX_STORE_BYTES:
        raise _evidence.EvidenceError(
            f"fleet approval store exceeds {MAX_STORE_BYTES} bytes")
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise _evidence.EvidenceError("fleet approval store could not be read") from exc
    value = _evidence.loads(
        raw, max_bytes=MAX_STORE_BYTES, max_items=MAX_STORE_PARSE_ITEMS)
    return _validate_store(value, key, now)


def _write_store(value: dict, key: bytes, *, reserve_bytes: int = 0) -> None:
    body = _unsigned(value)
    authenticated = {**body, "mac": _mac(key, body)}
    _validate_store(authenticated, key, _now())
    serialized = json.dumps(
        authenticated, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(serialized) + reserve_bytes > MAX_STORE_BYTES:
        if reserve_bytes:
            raise _evidence.EvidenceError(
                "fleet approval store cannot add authority while preserving "
                "revocation headroom")
        raise _evidence.EvidenceError(
            f"fleet approval store exceeds {MAX_STORE_BYTES} bytes")
    # Validate the exact bytes that the atomic writer will serialize. This
    # keeps write acceptance symmetric with the next read, including duplicate,
    # depth, item-count, UTF-8, and numeric bounds.
    reparsed = _evidence.loads(
        serialized, max_bytes=MAX_STORE_BYTES, max_items=MAX_STORE_PARSE_ITEMS)
    _validate_store(reparsed, key, _now())
    try:
        _atomic_write_json(store_path(), authenticated)
        _secure_file(store_path())
    except _evidence.EvidenceError:
        raise
    except OSError as exc:
        raise _evidence.EvidenceError(
            "fleet approval store could not be written atomically") from exc


@contextmanager
def _store_lock():
    """Use the shared cross-platform lock, then verify its actual leaf safely."""
    with _PROCESS_STORE_LOCK:
        lock = store_path() + ".lock"
        if not os.path.lexists(lock):
            created = False
            try:
                descriptor = os.open(
                    lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
                os.close(descriptor)
                _secure_file(lock)
            except FileExistsError:
                pass
            except OSError as exc:
                if created:
                    try:
                        os.unlink(lock)
                    except OSError:
                        pass
                raise _evidence.EvidenceError(
                    "fleet approval store lock could not be created") from exc
            except BaseException:
                if created:
                    try:
                        os.unlink(lock)
                    except OSError:
                        pass
                raise

        _regular_single_link(lock, "lock")
        try:
            _verify_private(lock, directory=False)
        except _evidence.EvidenceError as exc:
            repairable = False
            if os.name == "nt":
                snapshot = _windows_acl_snapshot(lock)
                rules = snapshot.get("rules")
                if isinstance(rules, dict):
                    rules = [rules]
                sid = _effective_user_sid()
                repairable = (
                    snapshot.get("owner") == sid and isinstance(rules, list)
                    and bool(rules) and all(
                        isinstance(rule, dict) and rule.get("sid") == sid
                        and rule.get("type") == "Allow"
                        and "FullControl" in str(rule.get("rights", ""))
                        for rule in rules))
            if not repairable:
                raise _evidence.EvidenceError(
                    "fleet approval store lock is unsafe; after confirming no approval "
                    "command is running, remove the lock file and retry") from exc
            _apply_windows_acl(lock, directory=False)

        inner = _exclusive_control_lock(store_path())
        try:
            inner.__enter__()
        except ControlBusyError as exc:
            raise ApprovalBusyError(
                "fleet approval store is busy; retry the command") from exc
        except ValueError as exc:
            raise _evidence.EvidenceError(
                "fleet approval store lock could not be acquired") from exc
        except OSError as exc:
            raise _evidence.EvidenceError(
                "fleet approval store lock could not be acquired") from exc
        try:
            # Validation happens before any store/key read or mutation. If a
            # race swapped the leaf for a link, the operation fails while
            # holding the resulting lock and writes nothing.
            _regular_single_link(lock, "lock")
            _verify_private(lock, directory=False)
            yield
        finally:
            try:
                inner.__exit__(None, None, None)
            except OSError as exc:
                raise _evidence.EvidenceError(
                    "fleet approval store lock could not be released") from exc


def _approval_scope(value: dict) -> dict:
    return {
        "schema": APPROVAL_SCHEMA,
        "store_id": value["store_id"],
        "bindings": value["bindings"],
        "actor": value["actor"],
        "operation": value["operation"],
        "authority": value["authority"],
    }


def _approval_identity(value: dict) -> dict:
    """Bind a public id to one issuance, never a reusable authorization scope."""
    return {
        **_approval_scope(value),
        "generation": value["generation"],
        "issued_at": value["issued_at"],
        "expires_at": value["expires_at"],
    }


def _lane(plan: dict, lane_name: str) -> tuple[dict, dict]:
    payload = _evidence.verify(plan)
    if plan.get("schema") != _fleet_compile.PLAN_SCHEMA:
        raise _evidence.EvidenceError("fleet approval requires a compiled fleet plan")
    lanes = [lane for lane in payload["lanes"] if lane["name"] == lane_name]
    if len(lanes) != 1:
        raise _evidence.EvidenceError("fleet approval requires one existing lane")
    return payload, lanes[0]


def _bindings(fleet: dict, plan: dict, lane: dict) -> dict:
    payload = _fleet_compile.verify_plan_for_fleet(plan, fleet)
    lane_sha = hashlib.sha256(
        b"summon.fleet-lane/v1\0" + _canonical(lane)).hexdigest()
    return {
        "fleet_sha256": fleet["sha256"],
        "plan_sha256": plan["sha256"],
        "project_sha256": payload["project_sha256"],
        "catalog_sha256": payload["catalog_sha256"],
        "lane": lane["name"],
        "lane_sha256": lane_sha,
    }


def _authority(lane: dict) -> dict:
    constraints = lane["constraints"]
    return _validate_authority({
        "permission_ceiling": constraints["permission_ceiling"],
        "data_boundary": constraints["data_boundary"],
        "corrective": dict(constraints["corrective"]),
        "spend": dict(constraints["spend"]),
    })


def _state(approval_id: str, store: dict, now: _dt.datetime) -> str:
    if approval_id in store["revocations"]:
        return "revoked"
    approval = store["approvals"][approval_id]
    return ("expired" if _parse_timestamp(
        approval["expires_at"], "expires_at") <= now else "active")


def _monotonic_now(store: dict, observed: _dt.datetime) -> _dt.datetime:
    """Never move authenticated store time backward within the allowed skew."""
    last_seen = _parse_timestamp(store["last_seen_at"], "last_seen_at")
    return max(observed, last_seen)


def _prune_terminal(store: dict, now: _dt.datetime, *, keep: str | None = None) -> int:
    """Compact expired or revoked authority before the next authenticated write."""
    removable = [
        approval_id for approval_id, approval in store["approvals"].items()
        if approval_id != keep and (
            approval_id in store["revocations"]
            or _parse_timestamp(approval["expires_at"], "expires_at") <= now)
    ]
    for approval_id in removable:
        store["approvals"].pop(approval_id, None)
        store["revocations"].pop(approval_id, None)
    return len(removable)


def _public(approval: dict, store: dict, now: _dt.datetime, *, action: str) -> dict:
    bindings = approval["bindings"]
    return {
        "status": "success",
        "action": action,
        "provider_contacted": False,
        "authorization": "recorded_not_activated",
        "selection": None,
        "dispatch_available": False,
        "approval": {
            "approval_id": approval["approval_id"],
            "state": _state(approval["approval_id"], store, now),
            "operation": approval["operation"],
            "generation": approval["generation"],
            "store_generation": store["generation"],
            "issued_at": approval["issued_at"],
            "expires_at": approval["expires_at"],
            **bindings,
        },
        "authority": json.loads(json.dumps(approval["authority"])),
    }


def status() -> dict:
    now = _now()
    key = _load_key(create=False)
    store = _read_store(key, now)
    if store is None:
        return {
            "status": "success", "action": "fleet_approval_status",
            "provider_contacted": False,
            "authorization": "recorded_not_activated",
            "selection": None,
            "initialized": False, "generation": 0,
            "counts": {"active": 0, "expired": 0, "revoked": 0},
            "dispatch_available": False,
        }
    counts = {"active": 0, "expired": 0, "revoked": 0}
    for approval_id in store["approvals"]:
        counts[_state(approval_id, store, now)] += 1
    return {
        "status": "success", "action": "fleet_approval_status",
        "provider_contacted": False,
        "authorization": "recorded_not_activated",
        "selection": None,
        "initialized": True, "generation": store["generation"],
        "counts": counts, "dispatch_available": False,
    }


def list_approvals() -> dict:
    now = _now()
    key = _load_key(create=False)
    store = _read_store(key, now)
    rows = [] if store is None else [
        _public(store["approvals"][approval_id], store, now,
                action="fleet_approval_inspected")["approval"]
        for approval_id in sorted(store["approvals"])
    ]
    return {
        "status": "success", "action": "fleet_approval_list",
        "provider_contacted": False,
        "authorization": "recorded_not_activated",
        "selection": None,
        "generation": 0 if store is None else store["generation"],
        "approvals": rows, "dispatch_available": False,
    }


def inspect(approval_id: str) -> dict:
    if not _APPROVAL_ID.fullmatch(str(approval_id or "")):
        raise _evidence.EvidenceError("fleet approval id is malformed")
    now = _now()
    key = _load_key(create=False)
    store = _read_store(key, now)
    if store is None or approval_id not in store["approvals"]:
        raise _evidence.EvidenceError("fleet approval does not exist")
    return _public(store["approvals"][approval_id], store, now,
                   action="fleet_approval_inspected")


def approve(*, fleet: dict, plan: dict, lane_name: str,
            expires_in_seconds: int, expected_generation: int) -> dict:
    if (not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or not 0 <= expected_generation <= MAX_GENERATION):
        raise _evidence.EvidenceError(
            "fleet approval requires a bounded non-negative expected generation")
    if not MIN_EXPIRY_SECONDS <= expires_in_seconds <= MAX_EXPIRY_SECONDS:
        raise _evidence.EvidenceError(
            "fleet approval expiry must be between 60s and 30d")
    payload, lane = _lane(plan, lane_name)
    del payload
    bindings = _bindings(fleet, plan, lane)
    authority = _authority(lane)
    root = os.path.dirname(store_path())
    _secure_private_root(root)
    now = _now()
    with _store_lock():
        key = _load_key(create=False)
        store = _read_store(key, now)
        if store is None:
            if expected_generation != 0:
                raise _evidence.EvidenceError(
                    "fleet approval generation changed; inspect status and retry explicitly")
            key = _load_key(create=True)
            if key is None:
                raise _evidence.EvidenceError(
                    "fleet approval key could not be created")
            store = _new_store(key, now)
        if key is None:
            raise _evidence.EvidenceError(
                "fleet approval key is unavailable")
        now = _monotonic_now(store, now)
        actor = {
            "kind": "local_operator",
            "id": hmac.new(key, b"summon-fleet-actor-v1",
                           hashlib.sha256).hexdigest(),
        }
        scope = {
            "schema": APPROVAL_SCHEMA,
            "store_id": store["store_id"],
            "bindings": bindings,
            "actor": actor,
            "operation": "fleet_dispatch",
            "authority": authority,
        }
        existing = next((item for approval_id, item in store["approvals"].items()
                         if approval_id not in store["revocations"]
                         and _approval_scope(item) == scope
                         and int((_parse_timestamp(
                             item["expires_at"], "expires_at") - _parse_timestamp(
                             item["issued_at"], "issued_at")).total_seconds())
                         == expires_in_seconds
                         and _parse_timestamp(
                             item["expires_at"], "expires_at") > now), None)
        if existing is not None:
            return _public(existing, store, now,
                           action="fleet_approval_recorded")
        if store["generation"] != expected_generation:
            raise _evidence.EvidenceError(
                "fleet approval generation changed; inspect status and retry explicitly")
        _prune_terminal(store, now)
        if len(store["approvals"]) >= MAX_COLLECTION_RECORDS:
            raise _evidence.EvidenceError(
                "fleet approval store is full of active approvals; revoke or wait for "
                "an approval to expire, then retry")
        future_active = len(store["approvals"]) + 1
        if store["generation"] + 1 + future_active > MAX_GENERATION:
            raise _evidence.EvidenceError(
                "fleet approval store cannot add authority while preserving "
                "revocation generation headroom")
        generation = store["generation"] + 1
        issuance = {
            **scope,
            "generation": generation,
            "issued_at": _timestamp(now),
            "expires_at": _timestamp(
                now + _dt.timedelta(seconds=expires_in_seconds)),
        }
        approval_id = hashlib.sha256(
            _canonical(_approval_identity(issuance))).hexdigest()
        unsigned = {**issuance, "approval_id": approval_id}
        approval = {**unsigned, "mac": _mac(key, unsigned)}
        _validate_approval(approval, key, store["store_id"], now=now)
        store["approvals"][approval_id] = approval
        store["generation"] = generation
        store["last_seen_at"] = _timestamp(now)
        _write_store(store, key, reserve_bytes=REVOCATION_RESERVE_BYTES)
        return _public(approval, store, now,
                       action="fleet_approval_recorded")


def revoke(approval_id: str, *, expected_generation: int) -> dict:
    if not _APPROVAL_ID.fullmatch(str(approval_id or "")):
        raise _evidence.EvidenceError("fleet approval id is malformed")
    if (not isinstance(expected_generation, int)
            or isinstance(expected_generation, bool)
            or not 0 <= expected_generation <= MAX_GENERATION):
        raise _evidence.EvidenceError(
            "fleet approval requires a bounded non-negative expected generation")
    root = os.path.dirname(store_path())
    _secure_private_root(root)
    now = _now()
    with _store_lock():
        key = _load_key(create=False)
        store = _read_store(key, now)
        if store is None or key is None or approval_id not in store["approvals"]:
            raise _evidence.EvidenceError("fleet approval does not exist")
        now = _monotonic_now(store, now)
        if approval_id in store["revocations"]:
            return _public(store["approvals"][approval_id], store, now,
                           action="fleet_approval_revoked")
        if store["generation"] != expected_generation:
            raise _evidence.EvidenceError(
                "fleet approval generation changed; inspect status and retry explicitly")
        _prune_terminal(store, now, keep=approval_id)
        if store["generation"] >= MAX_GENERATION:
            raise _evidence.EvidenceError(
                "fleet approval generation is exhausted")
        generation = store["generation"] + 1
        store["revocations"][approval_id] = {
            "generation": generation,
            "revoked_at": _timestamp(now),
            "reason": "operator_revoked",
        }
        store["generation"] = generation
        store["last_seen_at"] = _timestamp(now)
        _write_store(store, key)
        return _public(store["approvals"][approval_id], store, now,
                       action="fleet_approval_revoked")

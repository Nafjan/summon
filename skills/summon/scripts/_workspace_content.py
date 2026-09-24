"""Bounded immutable private blobs for a trusted local workspace supervisor.

No authority, message admission, provider access, deletion or automatic retry lives
here. Cooperating processes serialize all store access through the existing OS
control-lock primitive. The OS user is trusted; hostile same-user isolation and
Windows namespace/power-loss durability are not qualified by this module.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass

from _context_target import _final_open_path
from _fleet_approval import (
    _regular_single_link, _reject_reparse_ancestors, _secure_file, _verify_private,
)
from _job_control import ControlBusyError, _exclusive_control_lock


MAX_OBJECTS = 32
MAX_PAYLOAD_BYTES = 4096
MAX_AGGREGATE_PAYLOAD_BYTES = 131072
_REF = re.compile(r"blob-[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_LOCK_BASE = ".workspace-content"
_LOCK_NAME = _LOCK_BASE + ".lock"


class ContentError(ValueError):
    """Public-safe category and uncertainty, never content or local paths."""

    def __init__(self, kind: str, *, durability: str = "not_written"):
        self.kind = kind
        self.durability = durability
        super().__init__(f"workspace content refused ({kind}; {durability})")


@dataclass(frozen=True)
class ContentRef:
    reference: str
    sha256: str
    payload_bytes: int


@dataclass(frozen=True)
class ContentWrite:
    content: ContentRef
    file_durability: str
    namespace_durability: str
    reused: bool


def _bytes(value: bytes | str) -> bytes:
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else value
        if not isinstance(raw, bytes):
            raise ContentError("invalid_payload")
        raw.decode("utf-8", errors="strict")
    except UnicodeError:
        raise ContentError("invalid_utf8") from None
    if not 1 <= len(raw) <= MAX_PAYLOAD_BYTES:
        raise ContentError("payload_limit")
    return raw


def prepare_content(value: bytes | str) -> ContentRef:
    """Mint a supervisor reference before writing; retain it on uncertain IO."""
    raw = _bytes(value)
    return ContentRef("blob-" + secrets.token_hex(16),
                      hashlib.sha256(raw).hexdigest(), len(raw))


def _descriptor(value: ContentRef) -> None:
    if (not isinstance(value, ContentRef)
            or not isinstance(value.reference, str) or not _REF.fullmatch(value.reference)
            or not isinstance(value.sha256, str) or not _SHA.fullmatch(value.sha256)
            or not isinstance(value.payload_bytes, int) or isinstance(value.payload_bytes, bool)
            or not 1 <= value.payload_bytes <= MAX_PAYLOAD_BYTES):
        raise ContentError("invalid_reference")


def _identity(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class ContentStore:
    """An existing, dedicated, owner-private directory; never creates a root.

    Blob bytes have zero framing overhead. Every regular entry except the fixed
    empty lock counts against both object and file-length admission, including
    unknown names, incomplete writes and unreferenced objects. Filesystem block
    allocation, directory entries and ACL metadata are separate from these limits.
    """

    def __init__(self, root: str | os.PathLike[str]):
        try:
            self._root = os.path.abspath(os.fspath(root))
            self._check_root()
            self._root_id = self._directory_id()
        except ContentError:
            raise
        except (OSError, ValueError, TypeError):
            raise ContentError("private_root_invalid") from None

    def _directory_id(self) -> tuple:
        info = os.lstat(self._root)
        return info.st_dev, info.st_ino

    def _check_root(self) -> None:
        try:
            _reject_reparse_ancestors(self._root)
            info = os.lstat(self._root)
            if not stat.S_ISDIR(info.st_mode):
                raise ContentError("private_root_invalid")
            _verify_private(self._root, directory=True)
            if hasattr(self, "_root_id") and self._directory_id() != self._root_id:
                raise ContentError("root_replaced")
        except ContentError:
            raise
        except (OSError, ValueError):
            raise ContentError("private_root_invalid") from None

    def _entry(self, path: str) -> os.stat_result:
        try:
            _reject_reparse_ancestors(path)
            result = _regular_single_link(path, "content")
            _verify_private(path, directory=False)
            return result
        except (OSError, ValueError):
            raise ContentError("unsafe_entry") from None

    def _path(self, reference: str) -> str:
        # Validation forbids separators, drive syntax, traversal and ADS names.
        if not isinstance(reference, str) or not _REF.fullmatch(reference):
            raise ContentError("invalid_reference")
        path = os.path.join(self._root, reference)
        if os.path.dirname(path) != self._root:
            raise ContentError("invalid_reference")
        return path

    def _verify_handle(self, descriptor: int, path: str) -> os.stat_result:
        expected = self._entry(path)
        opened = os.fstat(descriptor)
        final = _final_open_path(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or _identity(opened) != _identity(expected)
                or final is None
                or os.path.normcase(final) != os.path.normcase(os.path.realpath(path))):
            raise ContentError("opened_file_changed")
        return opened

    @contextmanager
    def _locked(self):
        self._check_root()
        path = os.path.join(self._root, _LOCK_NAME)
        entered = False
        try:
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
                _secure_file(path)
            before = self._entry(path)
            if before.st_size != 0:
                raise ContentError("unsafe_lock")
            with _exclusive_control_lock(os.path.join(self._root, _LOCK_BASE)):
                self._check_root()
                after = self._entry(path)
                if _identity(before) != _identity(after):
                    raise ContentError("lock_replaced")
                entered = True
                yield
        except ContentError:
            raise
        except ControlBusyError:
            raise ContentError("store_busy") from None
        except (OSError, ValueError):
            raise ContentError("store_io", durability="unknown" if entered else "not_written") from None

    def _inventory(self) -> tuple[int, int]:
        count = total = 0
        try:
            with os.scandir(self._root) as entries:
                for item in entries:
                    if item.name == _LOCK_NAME:
                        continue
                    info = self._entry(item.path)
                    count += 1
                    total += info.st_size
                    if (count > MAX_OBJECTS or total > MAX_AGGREGATE_PAYLOAD_BYTES
                            or info.st_size > MAX_PAYLOAD_BYTES):
                        raise ContentError("store_limit")
        except ContentError:
            raise
        except OSError:
            raise ContentError("inventory_unavailable") from None
        return count, total

    def _read_locked(self, content: ContentRef) -> bytes:
        path = self._path(content.reference)
        try:
            try:
                os.lstat(path)
            except FileNotFoundError:
                raise ContentError("content_missing") from None
            self._entry(path)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
            fd = os.open(path, flags)
            try:
                before = self._verify_handle(fd, path)
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(MAX_PAYLOAD_BYTES + 1)
                after = self._verify_handle(fd, path)
            finally:
                os.close(fd)
            if _identity(before) != _identity(after):
                raise ContentError("content_changed")
            if (len(raw) != content.payload_bytes or after.st_size != len(raw)
                    or hashlib.sha256(raw).hexdigest() != content.sha256):
                raise ContentError("content_mismatch")
            _bytes(raw)
            return raw
        except ContentError:
            raise
        except OSError:
            raise ContentError("content_unreadable") from None

    def read(self, content: ContentRef) -> bytes:
        """Read verified exact bytes. A read alone makes no durability claim."""
        _descriptor(content)
        with self._locked():
            self._inventory()
            return self._read_locked(content)

    def provision(self) -> None:
        """Supervisor verifies the empty lock before publishing this store.

        Concurrent first use may safely refuse a not-yet-normalized lock ACL.
        Provision once before sharing the root with cooperating callers; this
        creates no blob and grants no message or execution authority.
        """
        with self._locked():
            self._inventory()

    def _sync_namespace(self) -> str:
        if os.name == "nt":
            return "unqualified"
        fd = os.open(self._root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return "directory_fsync_confirmed"

    def put(self, content: ContentRef, value: bytes | str, *,
            require_namespace_durable: bool = False) -> ContentWrite:
        """Create once, or verify and re-fsync the SAME immutable reference.

        An incomplete/mismatching prior write is retained and refused, never
        overwritten. The caller owns reconciliation and journal admission.
        """
        _descriptor(content)
        raw = _bytes(value)
        if len(raw) != content.payload_bytes or hashlib.sha256(raw).hexdigest() != content.sha256:
            raise ContentError("request_mismatch")
        if not isinstance(require_namespace_durable, bool):
            raise ContentError("invalid_durability_requirement")
        if require_namespace_durable and os.name == "nt":
            raise ContentError("namespace_durability_unqualified")
        with self._locked():
            count, total = self._inventory()
            path = self._path(content.reference)
            try:
                os.lstat(path)
                exists = True
            except FileNotFoundError:
                exists = False
            except OSError:
                raise ContentError("content_unreadable") from None
            if exists:
                self._read_locked(content)
            elif count >= MAX_OBJECTS or total + len(raw) > MAX_AGGREGATE_PAYLOAD_BYTES:
                raise ContentError("store_limit")
            phase = "open"
            try:
                flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
                if not exists:
                    flags |= os.O_CREAT | os.O_EXCL
                fd = os.open(path, flags, 0o600)
                try:
                    phase = "private_file"
                    if not exists:
                        _secure_file(path)  # Empty file is secured before any content.
                    self._verify_handle(fd, path)
                    with os.fdopen(fd, "r+b", closefd=False) as stream:
                        if not exists:
                            phase = "write"
                            if stream.write(raw) != len(raw):
                                raise OSError("short write")
                        phase = "flush"
                        stream.flush()
                        phase = "fsync"
                        os.fsync(stream.fileno())
                        phase = "close"
                finally:
                    os.close(fd)
                phase = "namespace_sync"
                namespace = self._sync_namespace()
                phase = "verify"
                self._check_root()
                if self._read_locked(content) != raw:
                    raise ContentError("content_mismatch")
            except (OSError, ValueError):
                raise ContentError("write_" + phase, durability="unknown") from None
            return ContentWrite(content, "file_fsync_confirmed", namespace, exists)

"""Published-address compatibility guard for NEW private workspace runs.

The existing coordinator remains the sole inner journal writer. Publication is
non-replacing and process-restart oriented; namespace power-loss durability and
hostile same-user isolation are deliberately not qualified here.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field

from _context_target import _final_open_path
from _fleet_approval import (
    _regular_single_link, _reject_reparse_ancestors, _secure_file,
    _secure_private_root, _verify_private,
)
from _rundir import run_path, validate_run_id


FORMAT = "summon.workspace-layout/v1"
MANIFEST = "workspace-layout.json"
GUARD = "generation.txt"
MAX_MANIFEST_BYTES = 1024
# Windows may briefly deny a rename while the just-created private tree is
# inspected by the host.  Retry only that transient permission result, without
# replacing an existing destination or retrying after publication.
_WINDOWS_PUBLISH_RETRY_DELAYS = (0.0, 0.01, 0.05)


class WorkspaceLayoutError(ValueError):
    """Public-safe refusal; underlying paths and manifest bodies stay private."""

    def __init__(self, kind: str, *, publication: str = "not_published",
                 subreason: str = "unknown"):
        self.kind = kind
        self.publication = publication
        self.subreason = subreason if _SAFE_SUBREASON.fullmatch(subreason) else "unknown"
        super().__init__(
            f"workspace layout refused ({kind}; {publication}; {self.subreason})")


_LAYOUT_FAILURE_STAGES = frozenset({
    "root", "staging", "acl", "manifest", "fsync", "validate", "publish", "resolve",
})
_LAYOUT_FAILURE_CATEGORIES = frozenset({
    "invalid", "missing", "permission", "storage", "busy", "host_load_sensitive",
    "refused", "os_error", "unknown",
})
_SAFE_SUBREASON = re.compile(
    r"(?:unknown|(?:root|staging|acl|manifest|fsync|validate|publish|resolve)_"
    r"(?:invalid|missing|permission|storage|busy|host_load_sensitive|refused|os_error|unknown))"
)


def _safe_failure_subreason(stage: str, error: BaseException | None = None) -> str:
    """Return a bounded diagnostic category without errno, path, or ACL details."""
    stage = stage if stage in _LAYOUT_FAILURE_STAGES else "root"
    category = "unknown"
    if isinstance(error, WorkspaceLayoutError):
        category = "refused"
    elif isinstance(error, OSError):
        code = getattr(error, "errno", None)
        if code in {errno.EACCES, errno.EPERM}:
            category = "permission"
        elif code in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
            category = "storage"
        elif code in {errno.EBUSY, errno.ETXTBSY}:
            category = "busy"
        elif code in {errno.EAGAIN, errno.EMFILE, errno.ENFILE}:
            category = "host_load_sensitive"
        elif code in {errno.ENOENT, errno.ENOTDIR}:
            category = "missing"
        elif code in {errno.EINVAL, errno.ELOOP}:
            category = "invalid"
        else:
            category = "os_error"
    elif isinstance(error, (ValueError, TypeError)):
        category = "invalid"
    if category not in _LAYOUT_FAILURE_CATEGORIES:
        category = "unknown"
    return f"{stage}_{category}"


@dataclass(frozen=True)
class WorkspaceLayout:
    run_id: str
    # These are private coordinator inputs, not public receipt fields.
    outer_directory: str = field(repr=False)
    inner_runs_root: str = field(repr=False)
    inner_run_directory: str = field(repr=False)
    format: str = FORMAT


def _directory(path: str) -> tuple[int, int]:
    _reject_reparse_ancestors(path)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise WorkspaceLayoutError("invalid_directory")
    _verify_private(path, directory=True)
    return info.st_dev, info.st_ino


def _manifest(run_id: str) -> bytes:
    value = {"format": FORMAT, "run_id": run_id, "inner_namespace": GUARD}
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def _read_manifest(path: str) -> bytes:
    _reject_reparse_ancestors(path)
    expected = _regular_single_link(path, "workspace manifest")
    _verify_private(path, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        final = _final_open_path(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino)
                or final is None
                or os.path.normcase(final) != os.path.normcase(os.path.realpath(path))):
            raise WorkspaceLayoutError("manifest_changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        after = os.fstat(fd)
        current = _regular_single_link(path, "workspace manifest")
        _verify_private(path, directory=False)
        identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
        if (identity(before) != identity(after) or identity(after) != identity(current)
                or len(raw) != after.st_size):
            raise WorkspaceLayoutError("manifest_changed")
    finally:
        os.close(fd)
    if not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise WorkspaceLayoutError("invalid_manifest")
    return raw


def _validate_outer(outer: str, run_id: str) -> WorkspaceLayout:
    outer_id = _directory(outer)
    if set(os.listdir(outer)) != {MANIFEST, GUARD}:
        raise WorkspaceLayoutError("incomplete_or_foreign_layout")
    raw = _read_manifest(os.path.join(outer, MANIFEST))
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise WorkspaceLayoutError("invalid_manifest") from None
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise WorkspaceLayoutError("unsupported_format")
    if raw != _manifest(run_id):
        raise WorkspaceLayoutError("manifest_binding_mismatch")
    inner_root = os.path.join(outer, GUARD)
    guard_id = _directory(inner_root)
    if set(os.listdir(inner_root)) != {run_id}:
        raise WorkspaceLayoutError("incomplete_or_foreign_layout")
    inner = run_path(inner_root, run_id)
    inner_id = _directory(inner)
    if (_directory(outer) != outer_id or _directory(inner_root) != guard_id
            or _directory(inner) != inner_id):
        raise WorkspaceLayoutError("directory_changed")
    return WorkspaceLayout(run_id, outer, inner_root, inner)


def resolve_workspace_layout(runs_root: str | os.PathLike[str], run_id: str) -> WorkspaceLayout:
    """Validate a completed published layout without acquiring or repairing."""
    try:
        run_id = validate_run_id(run_id)
        outer = run_path(os.fspath(runs_root), run_id)
        root = os.path.dirname(outer)
        root_id = _directory(root)
        result = _validate_outer(outer, run_id)
        if _directory(root) != root_id:
            raise WorkspaceLayoutError("directory_changed")
        return result
    except WorkspaceLayoutError:
        raise
    except (OSError, ValueError, TypeError):
        raise WorkspaceLayoutError("invalid_private_layout") from None


def _publish_new(source: str, destination: str) -> None:
    """Atomically publish without replacing even an existing empty directory."""
    if os.name == "nt":
        failure = None
        for delay in _WINDOWS_PUBLISH_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                os.rename(source, destination)  # Windows refuses an existing destination.
                return
            except PermissionError as exc:
                failure = exc
        if failure is not None:
            raise failure
        return
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise WorkspaceLayoutError("publication_unsupported")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                           ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        # AT_FDCWD, RENAME_NOREPLACE. The filesystem can refuse support too.
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            raise OSError(ctypes.get_errno(), "workspace publication failed")
        return
    raise WorkspaceLayoutError("publication_unsupported")


def create_workspace_layout(runs_root: str | os.PathLike[str], run_id: str) -> WorkspaceLayout:
    """Provision NEW layout, then publish its complete structural guard.

    Existing destinations are never adopted or rewritten. A bounded Windows
    retry covers only transient permission denial during no-replace publication;
    other failures leave private staging evidence for explicit disposition, and
    no retry occurs after publication becomes uncertain.
    The return value publishes coordinator inputs, not message/task authority.
    """
    published = False
    publication_started = False
    stage = "root"
    try:
        run_id = validate_run_id(run_id)
        outer = run_path(os.fspath(runs_root), run_id)
        root = os.path.dirname(outer)
        root_id = _directory(root)
        if os.path.lexists(outer):
            raise WorkspaceLayoutError("run_exists")
        # A leading dot is outside validate_run_id's language. Old normal
        # commands cannot address the incomplete staging name as a run ID.
        stage = "staging"
        staged = tempfile.mkdtemp(prefix=".workspace-stage-", dir=root)
        stage = "acl"
        _secure_private_root(staged)
        guard = os.path.join(staged, GUARD)
        _secure_private_root(guard)
        _secure_private_root(os.path.join(guard, run_id))
        stage = "manifest"
        path = os.path.join(staged, MANIFEST)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _secure_file(path)
            with os.fdopen(fd, "wb", closefd=False) as stream:
                raw = _manifest(run_id)
                if stream.write(raw) != len(raw):
                    raise OSError("short manifest write")
                stage = "fsync"
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)
        stage = "validate"
        _validate_outer(staged, run_id)
        if _directory(root) != root_id:
            raise WorkspaceLayoutError("directory_changed")
        publication_started = True
        stage = "publish"
        _publish_new(staged, outer)
        published = True
        stage = "resolve"
        return resolve_workspace_layout(root, run_id)
    except WorkspaceLayoutError as exc:
        if published:
            raise WorkspaceLayoutError(
                "published_validation_failed", publication="unknown",
                subreason=_safe_failure_subreason(stage, exc)) from None
        publication = "unknown" if publication_started else exc.publication
        subreason = (exc.subreason if exc.subreason != "unknown"
                     else _safe_failure_subreason(stage, exc))
        raise WorkspaceLayoutError(
            exc.kind, publication=publication, subreason=subreason) from None
    except (OSError, ValueError, TypeError) as exc:
        publication = "unknown" if publication_started or published else "not_published"
        kind = "published_validation_failed" if published else "provision_failed"
        raise WorkspaceLayoutError(
            kind, publication=publication,
            subreason=_safe_failure_subreason(stage, exc)) from None

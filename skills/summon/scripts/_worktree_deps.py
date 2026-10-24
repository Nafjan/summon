"""Opt-in dependency directory bridging for isolated git worktrees.

Summon's ``--worktree`` creates a fresh checkout so concurrent agents cannot
collide on uncommitted edits. Large dependency directories (``node_modules``,
``.venv``, ``target``) are typically untracked and absent from new worktrees.
When the operator explicitly passes ``--link-deps <dirs>``, this module bridges
the named repo-relative directories from the base repository into the worktree
using directory junctions on Windows and symlinks on POSIX.

Safety invariants:
* Strictly opt-in via ``--link-deps``; requires ``--worktree``.
* Refused under ``permission=yolo`` unless ``--link-deps-allow-mutation`` is
  explicitly supplied.
* Path validation rejects absolute paths, drive letters, ``..`` traversal,
  Windows trailing-dot/space and 8.3 aliases, and ``.git`` / ``.claude``
  segments. Base targets must exist as real directories (never symlinks or
  junctions) inside the repository root.
* Pre- and post-dispatch fingerprints cover nearby lockfiles and bounded
  multi-level directory trees (including scoped packages ``@scope/pkg``).
  Authoritative baseline state is retained in dispatcher memory; missing or
  tampered on-disk metadata or unacknowledged dependency mutation after
  dispatch fails closed (``error_kind="linked_deps_mutated"``).
* Intact Summon-created dependency links are excluded from untracked file lists
  in workspace snapshots, progress fingerprints, and worktree GC, and are
  cleanly unlinked before worktree removal only after verifying ancestor
  containment and target binding.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable

SCHEMA = "summon.worktree-deps/v1"
WORKTREE_DEPS_SCHEMA = SCHEMA
METADATA_FILENAME = "summon-linked-deps.json"
LOCKFILES = (
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Cargo.lock",
)
INNER_STATE_FILES = (
    ".package-lock.json",
    ".modules.yaml",
    ".yarn-state.yml",
    "pyvenv.cfg",
)
MAX_LOCKFILE_BYTES = 16 * 1024 * 1024
MAX_TREE_DEPTH = 32
MAX_TREE_VISITED_ENTRIES = 100_000
MAX_INLINE_FILE_HASH_BYTES = 65_536
MAX_TREE_HASH_BYTES = 8 * 1024 * 1024
_FORBIDDEN_SEGMENTS = frozenset({"", ".", "..", ".git", ".claude"})
_STRATEGIES = frozenset({"junction", "symlink"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _valid_strategy(value: Any) -> bool:
    return isinstance(value, str) and value in _STRATEGIES


class WorktreeDepsError(ValueError):
    """Typed failure for ``--link-deps`` validation, bridging, or integrity."""

    def __init__(self, kind: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


class _BridgedDepsProjection(dict):
    """Public projection dict carrying non-serialized dispatcher authority."""

    _authority: dict[str, Any] | None = None


def _is_link_or_junction(path: str) -> bool:
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if callable(isjunction):
        try:
            if isjunction(path):
                return True
        except OSError:
            pass
    if os.name == "nt":
        try:
            st = os.lstat(path)
            attrs = getattr(st, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if bool(attrs & reparse_flag):
                return True
        except OSError:
            pass
    return False


def _create_dir_link(src: str, dst: str) -> str:
    if os.name == "nt":
        try:
            import _winapi
            _winapi.CreateJunction(src, dst)
            return "junction"
        except OSError:
            os.symlink(src, dst, target_is_directory=True)
            return "symlink"
    os.symlink(src, dst, target_is_directory=True)
    return "symlink"


def _remove_dir_link(path: str) -> None:
    if not _is_link_or_junction(path):
        raise WorktreeDepsError(
            "link_deps_tampered",
            f"refusing to unlink non-link directory: {path}",
        )
    if os.name == "nt":
        try:
            os.unlink(path)
            return
        except OSError:
            os.rmdir(path)
            return
    os.unlink(path)


def _is_forbidden_segment(seg: str) -> bool:
    if seg in _FORBIDDEN_SEGMENTS:
        return True
    if seg.endswith((".", " ")) or "~" in seg:
        return True
    stripped = seg.rstrip(". ").lower()
    return stripped in _FORBIDDEN_SEGMENTS


def parse_link_deps(raw: str | Iterable[str] | None) -> tuple[str, ...]:
    """Validate and canonicalize ``--link-deps`` into a sorted tuple of relative POSIX paths."""
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, Iterable):
        parts = []
        for item in raw:
            if not isinstance(item, str):
                raise WorktreeDepsError(
                    "link_deps_invalid_path",
                    "--link-deps entries must be strings",
                )
            parts.extend(item.split(","))
    else:
        raise WorktreeDepsError(
            "link_deps_invalid_path",
            "--link-deps must be a comma-separated string of repo-relative paths",
        )
    cleaned: set[str] = set()
    for part in parts:
        token = part.strip()
        if not token:
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                "--link-deps contains an empty path entry",
            )
        if any(ord(ch) < 32 for ch in token) or ":" in token:
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                f"--link-deps path {token!r} contains forbidden characters or a drive prefix",
            )
        norm = token.replace("\\", "/")
        if (norm.startswith("/")
                or PurePosixPath(norm).is_absolute()
                or PureWindowsPath(token).is_absolute()
                or PureWindowsPath(token).drive):
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                f"--link-deps path {token!r} must be relative to the repository root",
            )
        norm = norm.rstrip("/")
        if not norm:
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                f"--link-deps path {token!r} is empty",
            )
        segments = norm.split("/")
        for seg in segments:
            if _is_forbidden_segment(seg):
                raise WorktreeDepsError(
                    "link_deps_invalid_path",
                    f"--link-deps path {token!r} contains forbidden or aliased segment {seg!r}",
                )
        cleaned.add("/".join(segments))
    items = sorted(cleaned)
    for idx, left in enumerate(items):
        prefix = left + "/"
        for right in items[idx + 1:]:
            if right.startswith(prefix):
                raise WorktreeDepsError(
                    "link_deps_invalid_path",
                    f"--link-deps paths {left!r} and {right!r} overlap",
                )
    return tuple(items)


def canonical_link_deps_arg(link_deps: tuple[str, ...] | list[str] | str | None) -> str | None:
    """Return canonical comma-joined string for request identity or None when empty."""
    parsed = parse_link_deps(link_deps)
    return ",".join(parsed) if parsed else None


def _git_toplevel(cwd: str) -> str:
    from _spawn import run_flags
    cmd = ["git"]
    if os.name == "nt":
        cmd += ["-c", "core.longpaths=true"]
    try:
        proc = subprocess.run(
            [*cmd, "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=15, check=False,
            **run_flags(),
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            f"--link-deps could not query git repository root: {exc}",
        ) from exc
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            f"--link-deps requires a git repository; {cwd} is not inside one",
        )
    return proc.stdout.strip()


def _validate_repo_target(repo_root: str, rel: str) -> tuple[str, str]:
    parsed = parse_link_deps([rel])
    if len(parsed) != 1 or parsed[0] != rel:
        raise WorktreeDepsError(
            "link_deps_invalid_path",
            f"--link-deps path {rel!r} is not in canonical repo-relative form",
        )
    repo_real = os.path.realpath(repo_root)
    if _is_link_or_junction(repo_root):
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            "--link-deps refuses repository root that is a symlink or junction",
        )
    cur = repo_root
    for seg in rel.split("/"):
        if _is_forbidden_segment(seg):
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                f"--link-deps path {rel!r} contains forbidden segment {seg!r}",
            )
        cur = os.path.join(cur, seg)
        if not os.path.lexists(cur):
            raise WorktreeDepsError(
                "link_deps_invalid_target",
                f"--link-deps target {rel!r} does not exist in repository root",
            )
        if _is_link_or_junction(cur):
            raise WorktreeDepsError(
                "link_deps_invalid_target",
                f"--link-deps target {rel!r} (or parent {seg!r}) must be a real directory, not a symlink or junction",
            )
    if not os.path.isdir(cur):
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            f"--link-deps target {rel!r} is not a directory",
        )
    target_real = os.path.realpath(cur)
    try:
        common = os.path.commonpath([os.path.normcase(repo_real), os.path.normcase(target_real)])
    except ValueError as exc:
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            f"--link-deps target {rel!r} resolves outside repository root",
        ) from exc
    if common != os.path.normcase(repo_real) or os.path.normcase(target_real) == os.path.normcase(repo_real):
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            f"--link-deps target {rel!r} resolves outside repository root",
        )
    rel_resolved = os.path.relpath(target_real, repo_real).replace("\\", "/")
    for rseg in rel_resolved.split("/"):
        if _is_forbidden_segment(rseg):
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                f"--link-deps target {rel!r} resolves to forbidden path {rel_resolved!r}",
            )
    return cur, target_real


def preflight_link_deps(
    cwd: str,
    link_deps: tuple[str, ...] | list[str] | str | None,
    *,
    worktree_arg: str | None,
    permission: str | None,
    allow_mutation: bool = False,
) -> dict[str, Any]:
    """Validate ``--link-deps`` before any worktree or branch is created."""
    parsed = parse_link_deps(link_deps)
    if not parsed:
        if allow_mutation:
            raise WorktreeDepsError(
                "link_deps_invalid_path",
                "--link-deps-allow-mutation requires --link-deps",
            )
        return {"enabled": False}
    if worktree_arg is None:
        raise WorktreeDepsError(
            "link_deps_requires_worktree",
            "--link-deps requires --worktree",
        )
    if permission == "yolo" and not allow_mutation:
        raise WorktreeDepsError(
            "link_deps_yolo_refused",
            "--link-deps is refused when permission is 'yolo' because bridged dependency "
            "directories are shared with the base checkout; pass --link-deps-allow-mutation "
            "to acknowledge shared dependency mutation risk",
        )
    repo_root = _git_toplevel(cwd)
    for rel in parsed:
        _validate_repo_target(repo_root, rel)
    return {
        "schema": SCHEMA,
        "enabled": True,
        "count": len(parsed),
        "paths": list(parsed),
        "strategy": "junction" if os.name == "nt" else "symlink",
        "allow_mutation": bool(allow_mutation),
        "repo": repo_root,
    }


_UNREADABLE_FILE = "__UNREADABLE__"


def _hash_regular_file(path: str) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE_FILE
    if _is_link_or_junction(path) or not stat.S_ISREG(st.st_mode):
        return _UNREADABLE_FILE
    if st.st_size > MAX_LOCKFILE_BYTES:
        return _UNREADABLE_FILE
    h = hashlib.sha256()
    total_read = 0
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            fst = os.fstat(fd)
            if (
                _is_link_or_junction(path)
                or not stat.S_ISREG(fst.st_mode)
                or fst.st_size > MAX_LOCKFILE_BYTES
                or (
                    getattr(st, "st_ino", 0)
                    and getattr(fst, "st_ino", 0)
                    and (fst.st_ino != st.st_ino or fst.st_dev != st.st_dev)
                )
            ):
                return _UNREADABLE_FILE
            while True:
                remaining = (MAX_LOCKFILE_BYTES - total_read) + 1
                chunk = os.read(fd, min(65536, max(1, remaining)))
                if not chunk:
                    break
                total_read += len(chunk)
                if total_read > MAX_LOCKFILE_BYTES:
                    return _UNREADABLE_FILE
                h.update(chunk)
        finally:
            os.close(fd)
    except OSError:
        return _UNREADABLE_FILE
    return h.hexdigest()


def _scandir_digest(
    directory: str,
    *,
    depth: int = MAX_TREE_DEPTH,
    budget: list[int] | None = None,
    byte_budget: list[int] | None = None,
    incomplete: list[bool] | None = None,
) -> str | None:
    if budget is None:
        budget = [MAX_TREE_VISITED_ENTRIES]
    if byte_budget is None:
        byte_budget = [MAX_TREE_HASH_BYTES]
    if incomplete is None:
        incomplete = [False]
    h = hashlib.sha256()
    try:
        entries = []
        with os.scandir(directory) as it:
            for entry in it:
                if budget[0] <= 0:
                    incomplete[0] = True
                    return None
                budget[0] -= 1
                entries.append(entry)
        entries.sort(key=lambda e: e.name)
        h.update(f"total:{len(entries)}\0".encode("utf-8"))
        for entry in entries:
            try:
                is_sym = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and not is_sym:
                    # Directory st_size/st_mtime_ns on Windows NTFS can update
                    # asynchronously when background indexers/scanners inspect a
                    # newly created folder. Hash directory names/structure and
                    # regular-file stats across the bounded tree instead.
                    if depth <= 0:
                        incomplete[0] = True
                        return None
                    rec = f"D\0{entry.name}\n"
                    h.update(rec.encode("utf-8", errors="surrogateescape"))
                    sub_digest = _scandir_digest(
                        entry.path,
                        depth=depth - 1,
                        budget=budget,
                        byte_budget=byte_budget,
                        incomplete=incomplete,
                    )
                    if sub_digest is None or incomplete[0]:
                        incomplete[0] = True
                        return None
                    h.update(
                        f"S\0{entry.name}\0{sub_digest}\n".encode(
                            "utf-8", errors="surrogateescape"
                        )
                    )
                else:
                    st = entry.stat(follow_symlinks=False)
                    mode_oct = f"{stat.S_IMODE(st.st_mode):o}"
                    sym_target = ""
                    content_digest = ""
                    if is_sym:
                        try:
                            sym_target = os.readlink(entry.path)
                        except OSError:
                            incomplete[0] = True
                            return None
                    elif stat.S_ISREG(st.st_mode) and st.st_size > 0:
                        if (
                            st.st_size > MAX_INLINE_FILE_HASH_BYTES
                            or byte_budget[0] < st.st_size
                        ):
                            incomplete[0] = True
                            return None
                        byte_budget[0] -= st.st_size
                        digest = _hash_regular_file(entry.path)
                        if digest is None or digest == _UNREADABLE_FILE:
                            incomplete[0] = True
                            return None
                        content_digest = digest
                    rec = (
                        f"F\0{entry.name}\0{int(is_sym)}\0{mode_oct}\0"
                        f"{st.st_size}\0{st.st_mtime_ns}\0{sym_target}\0{content_digest}\n"
                    )
                    h.update(rec.encode("utf-8", errors="surrogateescape"))
            except OSError:
                incomplete[0] = True
                return None
    except OSError:
        incomplete[0] = True
        return None
    return None if incomplete[0] else h.hexdigest()


def fingerprint_dep_target(repo_root: str, rel: str) -> str | None:
    """Compute a bounded SHA-256 fingerprint of a dependency directory and nearby lockfiles.

    Returns ``None`` (fail-closed) if the directory is missing, unreadable, or
    exceeds ``MAX_TREE_DEPTH`` or ``MAX_TREE_VISITED_ENTRIES``.
    """
    target = os.path.join(repo_root, *rel.split("/"))
    if not os.path.isdir(target) or _is_link_or_junction(target):
        return None
    parent = os.path.dirname(target) or repo_root
    h = hashlib.sha256()
    h.update(f"rel:{rel}\0".encode("utf-8"))

    lock_dirs = [repo_root]
    if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(os.path.realpath(repo_root)):
        lock_dirs.append(parent)
    for ldir in lock_dirs:
        label = "root" if ldir == repo_root else "parent"
        for lock_name in LOCKFILES:
            lpath = os.path.join(ldir, lock_name)
            digest = _hash_regular_file(lpath)
            if digest == _UNREADABLE_FILE:
                return None
            if digest is not None:
                h.update(f"lock:{label}:{lock_name}:{digest}\0".encode("utf-8"))

    for inner_name in INNER_STATE_FILES:
        ipath = os.path.join(target, inner_name)
        digest = _hash_regular_file(ipath)
        if digest == _UNREADABLE_FILE:
            return None
        if digest is not None:
            h.update(f"inner:{inner_name}:{digest}\0".encode("utf-8"))

    budget = [MAX_TREE_VISITED_ENTRIES]
    incomplete = [False]
    top_digest = _scandir_digest(
        target, depth=MAX_TREE_DEPTH, budget=budget, incomplete=incomplete
    )
    if top_digest is None or incomplete[0]:
        return None
    h.update(f"top:{top_digest}\0".encode("utf-8"))
    return h.hexdigest()


def _overall_fingerprint(per_path: dict[str, str]) -> str:
    h = hashlib.sha256()
    for rel in sorted(per_path):
        h.update(f"{rel}:{per_path[rel]}\n".encode("utf-8"))
    return h.hexdigest()


def _find_worktree_root_and_gitdir(path: str) -> tuple[str, str] | None:
    """Locate the enclosing linked-worktree root and its private gitdir without spawning git."""
    if not path or not isinstance(path, str):
        return None
    cur = os.path.abspath(path)
    for _ in range(32):
        dot_git = os.path.join(cur, ".git")
        try:
            if os.path.isfile(dot_git) and not _is_link_or_junction(dot_git):
                with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
                    first = fh.readline(4096).strip()
                if first.startswith("gitdir:"):
                    raw_gitdir = first[len("gitdir:"):].strip()
                    gitdir = (raw_gitdir if os.path.isabs(raw_gitdir)
                              else os.path.normpath(os.path.join(cur, raw_gitdir)))
                    if os.path.isdir(gitdir):
                        return cur, gitdir
                return None
            if os.path.isdir(dot_git):
                return None
        except OSError:
            return None
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    return None


def _metadata_path(worktree_path: str) -> str | None:
    found = _find_worktree_root_and_gitdir(worktree_path)
    if found is not None:
        return os.path.join(found[1], METADATA_FILENAME)
    from _spawn import run_flags
    cmd = ["git"]
    if os.name == "nt":
        cmd += ["-c", "core.longpaths=true"]
    try:
        proc = subprocess.run(
            [*cmd, "-C", worktree_path, "rev-parse", "--path-format=absolute",
             "--git-path", METADATA_FILENAME],
            capture_output=True, text=True, timeout=10, check=False,
            **run_flags(),
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return None


def _write_metadata_atomic(meta_path: str, payload: dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    tmp_path = f"{meta_path}.{uuid.uuid4().hex}.tmp"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(raw)
            fh.flush()
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.replace(tmp_path, meta_path)
    return digest


def _validate_private_metadata(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    raw_paths = data.get("paths")
    targets = data.get("targets")
    repo_root = data.get("repo_root")
    before_sha256 = data.get("before_sha256")
    allow_mutation = data.get("allow_mutation")
    strategy = data.get("strategy")
    if (not isinstance(raw_paths, list)
            or not raw_paths
            or any(not isinstance(p, str) for p in raw_paths)
            or not isinstance(targets, dict)
            or not isinstance(repo_root, str) or not repo_root
            or not isinstance(before_sha256, str) or not _HEX64_RE.fullmatch(before_sha256)
            or type(allow_mutation) is not bool
            or not _valid_strategy(strategy)):
        return None
    try:
        canonical_paths = list(parse_link_deps(raw_paths))
    except WorktreeDepsError:
        return None
    if canonical_paths != raw_paths or set(targets.keys()) != set(canonical_paths):
        return None
    repo_real = os.path.realpath(repo_root)
    for rel in canonical_paths:
        tinfo = targets.get(rel)
        if not isinstance(tinfo, dict):
            return None
        target_real = tinfo.get("target_realpath")
        before_fp = tinfo.get("before_sha256")
        t_strategy = tinfo.get("strategy")
        if (not isinstance(target_real, str) or not target_real
                or not isinstance(before_fp, str) or not _HEX64_RE.fullmatch(before_fp)
                or (t_strategy is not None and not _valid_strategy(t_strategy))):
            return None
        try:
            common = os.path.commonpath([os.path.normcase(repo_real), os.path.normcase(target_real)])
        except ValueError:
            return None
        if common != os.path.normcase(repo_real) or os.path.normcase(target_real) == os.path.normcase(repo_real):
            return None
    return data


def _read_metadata_with_digest(worktree_path: str) -> tuple[str | None, dict[str, Any] | None, str | None]:
    found = _find_worktree_root_and_gitdir(worktree_path)
    if found is None:
        return None, None, None
    wt_root, gitdir = found
    meta_path = os.path.join(gitdir, METADATA_FILENAME)
    if not os.path.isfile(meta_path) or _is_link_or_junction(meta_path):
        return wt_root, None, None
    try:
        with open(meta_path, "rb") as fh:
            raw = fh.read(MAX_LOCKFILE_BYTES)
        digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode("utf-8"))
        validated = _validate_private_metadata(data)
        if validated is None:
            return wt_root, None, digest
        return wt_root, validated, digest
    except (OSError, ValueError, TypeError, UnicodeDecodeError):
        return wt_root, None, None


def _read_metadata(worktree_path: str) -> tuple[str | None, dict[str, Any] | None]:
    wt_root, data, _ = _read_metadata_with_digest(worktree_path)
    return wt_root, data


def _resolve_contained_worktree_link(
    wt_root: str,
    rel: str,
    *,
    expected_target_realpath: str | None = None,
    repo_root: str | None = None,
) -> tuple[str, str | None]:
    """Resolve ``rel`` inside ``wt_root`` while rejecting traversal or ancestor link hops."""
    try:
        parsed = parse_link_deps([rel])
    except WorktreeDepsError:
        return "", "invalid_rel_path"
    if len(parsed) != 1 or parsed[0] != rel:
        return "", "invalid_rel_path"
    if not wt_root or _is_link_or_junction(wt_root) or not os.path.isdir(wt_root):
        return "", "invalid_worktree_root"
    wt_real = os.path.realpath(wt_root)

    if expected_target_realpath is not None:
        if not isinstance(expected_target_realpath, str) or not expected_target_realpath:
            return "", "invalid_expected_target"
        if repo_root:
            repo_real = os.path.realpath(repo_root)
            try:
                common_repo = os.path.commonpath(
                    [os.path.normcase(repo_real), os.path.normcase(expected_target_realpath)]
                )
            except ValueError:
                return "", "expected_target_outside_repo"
            if (common_repo != os.path.normcase(repo_real)
                    or os.path.normcase(expected_target_realpath) == os.path.normcase(repo_real)):
                return "", "expected_target_outside_repo"

    segments = rel.split("/")
    cur = wt_root
    for seg in segments[:-1]:
        cur = os.path.join(cur, seg)
        if not os.path.lexists(cur) and not _is_link_or_junction(cur):
            return os.path.join(wt_root, *segments), "missing_ancestor"
        if _is_link_or_junction(cur) or not os.path.isdir(cur):
            return "", "ancestor_not_real_dir"
        cur_real = os.path.realpath(cur)
        try:
            common = os.path.commonpath([os.path.normcase(wt_real), os.path.normcase(cur_real)])
        except ValueError:
            return "", "ancestor_outside_worktree"
        if common != os.path.normcase(wt_real) or os.path.normcase(cur_real) == os.path.normcase(wt_real):
            return "", "ancestor_outside_worktree"

    dst = os.path.join(cur, segments[-1])
    return dst, None


def bridge_worktree_deps(
    repo_root: str,
    worktree_path: str,
    link_deps: tuple[str, ...] | list[str] | str | None,
    *,
    allow_mutation: bool = False,
) -> dict[str, Any]:
    """Create dependency directory junctions/symlinks inside a newly created worktree."""
    parsed = parse_link_deps(link_deps)
    if not parsed:
        raise WorktreeDepsError("link_deps_invalid_path", "bridge_worktree_deps requires non-empty link_deps")

    created_links: list[str] = []
    targets_meta: dict[str, dict[str, Any]] = {}
    before_per_path: dict[str, str] = {}
    strategies: set[str] = set()
    wt_real = os.path.realpath(worktree_path)

    try:
        for rel in parsed:
            src, src_real = _validate_repo_target(repo_root, rel)
            fp = fingerprint_dep_target(repo_root, rel)
            if fp is None or not _HEX64_RE.fullmatch(fp):
                raise WorktreeDepsError(
                    "link_deps_fingerprint_incomplete",
                    f"--link-deps target {rel!r} could not be completely fingerprinted "
                    f"(exceeded {MAX_TREE_VISITED_ENTRIES} entries or depth {MAX_TREE_DEPTH}, or unreadable)",
                )
            before_per_path[rel] = fp

            segments = rel.split("/")
            cur = worktree_path
            for parent_seg in segments[:-1]:
                cur = os.path.join(cur, parent_seg)
                if _is_link_or_junction(cur):
                    raise WorktreeDepsError(
                        "link_deps_bridge_failed",
                        f"worktree parent directory {parent_seg!r} for {rel!r} is a symlink or junction",
                    )
                if not os.path.exists(cur):
                    os.makedirs(cur, exist_ok=True)
                elif not os.path.isdir(cur):
                    raise WorktreeDepsError(
                        "link_deps_bridge_failed",
                        f"worktree parent path for {rel!r} exists and is not a directory",
                    )
                cur_real = os.path.realpath(cur)
                if os.path.commonpath([os.path.normcase(wt_real), os.path.normcase(cur_real)]) != os.path.normcase(wt_real):
                    raise WorktreeDepsError(
                        "link_deps_bridge_failed",
                        f"worktree parent path for {rel!r} resolves outside worktree",
                    )

            dst = os.path.join(worktree_path, *segments)
            if os.path.lexists(dst) or _is_link_or_junction(dst):
                raise WorktreeDepsError(
                    "link_deps_bridge_failed",
                    f"worktree path {rel!r} already exists in checkout; refusing to overwrite tracked or existing files",
                )
            strategy = _create_dir_link(src, dst)
            created_links.append(dst)
            strategies.add(strategy)
            targets_meta[rel] = {
                "target_realpath": src_real,
                "before_sha256": fp,
                "strategy": strategy,
            }

        before_sha256 = _overall_fingerprint(before_per_path)
        effective_strategy = ("junction" if "junction" in strategies
                              else "symlink" if "symlink" in strategies
                              else ("junction" if os.name == "nt" else "symlink"))
        meta_path = _metadata_path(worktree_path)
        if not meta_path:
            raise WorktreeDepsError(
                "link_deps_bridge_failed",
                "could not resolve worktree git metadata path for linked dependencies",
            )
        private_meta = {
            "schema": SCHEMA,
            "repo_root": os.path.realpath(repo_root),
            "paths": list(parsed),
            "strategy": effective_strategy,
            "allow_mutation": bool(allow_mutation),
            "before_sha256": before_sha256,
            "targets": targets_meta,
        }
        meta_sha256 = _write_metadata_atomic(meta_path, private_meta)
    except BaseException as exc:
        for link_path in reversed(created_links):
            try:
                if _is_link_or_junction(link_path):
                    _remove_dir_link(link_path)
            except OSError:
                pass
        if isinstance(exc, WorktreeDepsError):
            raise
        raise WorktreeDepsError(
            "link_deps_bridge_failed",
            f"failed to bridge dependency directories into worktree: {exc}",
        ) from exc

    projection = _BridgedDepsProjection({
        "schema": SCHEMA,
        "count": len(parsed),
        "paths": list(parsed),
        "strategy": effective_strategy,
        "allow_mutation": bool(allow_mutation),
        "before_sha256": before_sha256,
        "after_sha256": None,
        "deps_mutated": False,
        "links_intact": True,
        "mutated_paths": [],
    })
    projection._authority = copy.deepcopy(private_meta) | {"meta_sha256": meta_sha256}
    return projection


def _link_points_to_expected(dst: str, expected_realpath: str) -> bool:
    if not _is_link_or_junction(dst):
        return False
    try:
        actual_real = os.path.realpath(dst)
        return os.path.normcase(actual_real) == os.path.normcase(expected_realpath)
    except OSError:
        return False


def intact_linked_dep_paths(cwd: str | None) -> frozenset[str]:
    """Return repo-relative POSIX paths of intact Summon-created dependency links in ``cwd``'s worktree."""
    if not cwd or not isinstance(cwd, str):
        return frozenset()
    wt_root, data = _read_metadata(cwd)
    if not wt_root or not isinstance(data, dict):
        return frozenset()
    repo_root = data.get("repo_root")
    targets = data.get("targets")
    if not isinstance(targets, dict):
        return frozenset()
    intact: set[str] = set()
    for rel, info in targets.items():
        if not isinstance(rel, str) or not isinstance(info, dict):
            continue
        expected_real = info.get("target_realpath")
        if not isinstance(expected_real, str) or not expected_real:
            continue
        dst, problem = _resolve_contained_worktree_link(
            wt_root, rel, expected_target_realpath=expected_real,
            repo_root=repo_root if isinstance(repo_root, str) else None,
        )
        if problem is not None or not dst:
            continue
        if _link_points_to_expected(dst, expected_real):
            intact.add(rel)
    return frozenset(intact)


def _unescape_git_quoted_path(raw: str) -> str:
    """Decode Git C-style quoted path from porcelain v1 line without altering backslashes."""
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    inner = raw[1:-1]
    out_bytes = bytearray()
    i = 0
    n = len(inner)
    escape_map = {
        "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
        "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C,
    }
    while i < n:
        ch = inner[i]
        if ch != "\\" or i + 1 >= n:
            out_bytes.extend(ch.encode("utf-8", errors="surrogateescape"))
            i += 1
            continue
        nxt = inner[i + 1]
        if nxt in "01234567" and i + 3 < n and all(c in "01234567" for c in inner[i + 1:i + 4]):
            out_bytes.append(int(inner[i + 1:i + 4], 8) & 0xFF)
            i += 4
        elif nxt in escape_map:
            out_bytes.append(escape_map[nxt])
            i += 2
        else:
            out_bytes.extend(nxt.encode("utf-8", errors="surrogateescape"))
            i += 2
    return out_bytes.decode("utf-8", errors="surrogateescape")


def is_excluded_untracked_path(rel_path: str, intact_deps: frozenset[str]) -> bool:
    """True when ``rel_path`` (from git status porcelain) is an intact bridged dependency link.

    Git porcelain always uses forward slashes ``/`` as directory separators on both
    POSIX and Windows. Literal backslashes in ``rel_path`` are preserved so POSIX
    filenames containing ``\\`` are never mistaken for directory separators.
    """
    if not intact_deps or not rel_path:
        return False
    norm = rel_path.rstrip("/")
    for dep in intact_deps:
        if norm == dep or norm.startswith(dep + "/"):
            return True
    return False


def filter_porcelain_v1_text(porcelain_text: str, worktree_path: str) -> str:
    """Filter intact Summon-linked dependency untracked lines from ``git status --porcelain`` text."""
    if not porcelain_text:
        return porcelain_text
    intact = intact_linked_dep_paths(worktree_path)
    if not intact:
        return porcelain_text
    kept: list[str] = []
    for line in porcelain_text.splitlines():
        if line.startswith("?? "):
            raw_path = _unescape_git_quoted_path(line[3:].strip())
            if is_excluded_untracked_path(raw_path, intact):
                continue
        kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def filter_porcelain_v2_raw(raw: bytes, worktree_path: str) -> bytes:
    """Filter intact Summon-linked dependency untracked NUL records from ``--porcelain=v2 -z`` bytes."""
    if not raw:
        return raw
    intact = intact_linked_dep_paths(worktree_path)
    if not intact:
        return raw
    tokens = raw.split(b"\0")
    kept: list[bytes] = []
    expect_rename_source = False
    for token in tokens:
        if expect_rename_source:
            expect_rename_source = False
            kept.append(token)
            continue
        if token.startswith(b"2 "):
            expect_rename_source = True
            kept.append(token)
            continue
        if token.startswith(b"? "):
            rel = token[2:].decode("utf-8", errors="surrogateescape")
            if is_excluded_untracked_path(rel, intact):
                continue
        kept.append(token)
    return b"\0".join(kept)


def verify_linked_deps(worktree_info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Re-verify linked dependency fingerprints and link integrity after dispatch.

    Uses dispatcher-owned in-memory authority whenever present, and fails closed
    if on-disk metadata was deleted, corrupted, or modified during the run.
    """
    if not isinstance(worktree_info, dict):
        return None
    wt_path = worktree_info.get("path") or worktree_info.get("cwd")
    if not isinstance(wt_path, str) or not wt_path:
        return None

    existing = worktree_info.get("linked_deps")
    auth = worktree_info.get("_linked_deps_authority")
    if not isinstance(auth, dict):
        auth = getattr(existing, "_authority", None)

    wt_root, disk_meta, disk_digest = _read_metadata_with_digest(wt_path)
    if not wt_root:
        found = _find_worktree_root_and_gitdir(wt_path)
        wt_root = found[0] if found else os.path.abspath(wt_path)

    source_meta = auth if isinstance(auth, dict) else disk_meta
    if not isinstance(source_meta, dict):
        if isinstance(existing, dict):
            fallback_paths = [p for p in (existing.get("paths") or ()) if isinstance(p, str)]
            raw_strat = existing.get("strategy")
            failed_proj = {
                "schema": SCHEMA,
                "count": len(fallback_paths),
                "paths": list(fallback_paths),
                "strategy": raw_strat if _valid_strategy(raw_strat) else (
                    "junction" if os.name == "nt" else "symlink"
                ),
                "allow_mutation": bool(existing.get("allow_mutation", False)),
                "before_sha256": existing.get("before_sha256"),
                "after_sha256": None,
                "deps_mutated": True,
                "links_intact": False,
                "mutated_paths": list(fallback_paths),
            }
            worktree_info["linked_deps"] = failed_proj
            return failed_proj
        return None

    repo_root = source_meta.get("repo_root") or worktree_info.get("repo")
    paths = [p for p in (source_meta.get("paths") or ()) if isinstance(p, str)]
    targets = source_meta.get("targets") if isinstance(source_meta.get("targets"), dict) else {}
    before_sha256 = source_meta.get("before_sha256")
    allow_mutation = bool(source_meta.get("allow_mutation", False))
    raw_strat = source_meta.get("strategy")
    strategy = raw_strat if _valid_strategy(raw_strat) else (
        "junction" if os.name == "nt" else "symlink"
    )

    metadata_intact = disk_meta is not None
    if isinstance(auth, dict) and auth.get("meta_sha256"):
        if disk_digest != auth.get("meta_sha256"):
            metadata_intact = False

    after_per_path: dict[str, str] = {}
    mutated_paths: list[str] = []
    links_intact = bool(metadata_intact and paths)

    for rel in paths:
        tinfo = targets.get(rel) if isinstance(targets.get(rel), dict) else {}
        expected_real = tinfo.get("target_realpath")
        before_fp = tinfo.get("before_sha256")
        dst, problem = _resolve_contained_worktree_link(
            wt_root, rel,
            expected_target_realpath=expected_real if isinstance(expected_real, str) else None,
            repo_root=str(repo_root) if isinstance(repo_root, str) else None,
        )
        intact = (problem is None
                  and bool(dst)
                  and isinstance(expected_real, str)
                  and bool(expected_real)
                  and _link_points_to_expected(dst, expected_real))
        if not intact:
            links_intact = False
        after_fp = (fingerprint_dep_target(str(repo_root), rel)
                    if isinstance(repo_root, str) and repo_root else None)
        if isinstance(after_fp, str) and _HEX64_RE.fullmatch(after_fp):
            after_per_path[rel] = after_fp
        else:
            after_fp = None
        if (after_fp is None
                or not isinstance(before_fp, str)
                or not _HEX64_RE.fullmatch(before_fp)
                or after_fp != before_fp
                or not intact
                or not metadata_intact):
            mutated_paths.append(rel)

    after_sha256 = (
        _overall_fingerprint(after_per_path)
        if paths and len(after_per_path) == len(paths)
        else None
    )
    deps_mutated = bool(
        not metadata_intact
        or after_sha256 is None
        or after_sha256 != before_sha256
        or not links_intact
        or mutated_paths
    )
    projection = _BridgedDepsProjection({
        "schema": SCHEMA,
        "count": len(paths),
        "paths": list(paths),
        "strategy": strategy,
        "allow_mutation": allow_mutation,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "deps_mutated": deps_mutated,
        "links_intact": links_intact,
        "mutated_paths": mutated_paths,
    })
    if isinstance(auth, dict):
        projection._authority = copy.deepcopy(auth)
    worktree_info["linked_deps"] = projection
    return dict(projection)


def validate_projection(record: Any) -> dict[str, Any]:
    """Validate a public ``summon.worktree-deps/v1`` projection dict."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise WorktreeDepsError("link_deps_projection_invalid", "invalid summon.worktree-deps/v1 record")
    required = {
        "schema", "count", "paths", "strategy", "allow_mutation",
        "before_sha256", "after_sha256", "deps_mutated", "links_intact", "mutated_paths",
    }
    if set(record.keys()) != required:
        raise WorktreeDepsError("link_deps_projection_invalid", "exact summon.worktree-deps/v1 fields required")
    count = record["count"]
    paths = record["paths"]
    strategy = record["strategy"]
    allow_mutation = record["allow_mutation"]
    before_sha256 = record["before_sha256"]
    after_sha256 = record["after_sha256"]
    deps_mutated = record["deps_mutated"]
    links_intact = record["links_intact"]
    mutated_paths = record["mutated_paths"]

    if type(count) is not int or count < 0:
        raise WorktreeDepsError("link_deps_projection_invalid", "count must be a non-negative integer")
    if not isinstance(paths, list) or len(paths) != count or any(not isinstance(p, str) for p in paths):
        raise WorktreeDepsError("link_deps_projection_invalid", "paths must be a list of strings matching count")
    if paths and list(parse_link_deps(paths)) != paths:
        raise WorktreeDepsError("link_deps_projection_invalid", "paths must be canonical sorted relative paths")
    if not _valid_strategy(strategy):
        raise WorktreeDepsError("link_deps_projection_invalid", "strategy must be 'junction' or 'symlink'")
    if (type(allow_mutation) is not bool
            or type(deps_mutated) is not bool
            or type(links_intact) is not bool):
        raise WorktreeDepsError("link_deps_projection_invalid", "boolean fields must be strict bools")
    if not isinstance(before_sha256, str) or not _HEX64_RE.fullmatch(before_sha256):
        raise WorktreeDepsError("link_deps_projection_invalid", "before_sha256 must be a 64-char hex digest")
    if after_sha256 is not None and (not isinstance(after_sha256, str) or not _HEX64_RE.fullmatch(after_sha256)):
        raise WorktreeDepsError("link_deps_projection_invalid", "after_sha256 must be None or a 64-char hex digest")
    if (not isinstance(mutated_paths, list)
            or any(not isinstance(p, str) for p in mutated_paths)
            or not set(mutated_paths).issubset(set(paths))):
        raise WorktreeDepsError("link_deps_projection_invalid", "mutated_paths must be a subset of paths")
    if (not links_intact
            or bool(mutated_paths)
            or (after_sha256 is not None and after_sha256 != before_sha256)) and not deps_mutated:
        raise WorktreeDepsError(
            "link_deps_projection_invalid",
            "deps_mutated must be True when links_intact is False, mutated_paths is non-empty, or digests differ",
        )
    return dict(record)


def teardown_linked_deps(worktree_path: str) -> dict[str, Any]:
    """Safely unlink Summon-created dependency junctions/symlinks before worktree removal.

    Re-validates stored metadata, checks every ancestor directory for containment
    and symlink/junction hops, and binds each link to its expected target realpath.
    Never deletes a real directory or a redirected link; reports ``tampered`` instead
    so the caller preserves the worktree.
    """
    found = _find_worktree_root_and_gitdir(worktree_path)
    if found is None:
        return {"present": False, "removed": [], "tampered": [], "errors": []}
    wt_root, gitdir = found
    meta_path = os.path.join(gitdir, METADATA_FILENAME)
    if not os.path.lexists(meta_path):
        return {"present": False, "removed": [], "tampered": [], "errors": []}
    if _is_link_or_junction(meta_path) or not os.path.isfile(meta_path):
        return {"present": True, "removed": [], "tampered": ["<metadata>"], "errors": []}

    _, meta, _ = _read_metadata_with_digest(worktree_path)
    if not isinstance(meta, dict):
        return {"present": True, "removed": [], "tampered": ["<metadata>"], "errors": []}

    repo_root = meta.get("repo_root")
    paths = [p for p in (meta.get("paths") or ()) if isinstance(p, str)]
    targets = meta.get("targets") if isinstance(meta.get("targets"), dict) else {}
    removed: list[str] = []
    tampered: list[str] = []
    errors: list[str] = []

    for rel in paths:
        tinfo = targets.get(rel) if isinstance(targets.get(rel), dict) else {}
        expected_real = tinfo.get("target_realpath")
        dst, problem = _resolve_contained_worktree_link(
            wt_root, rel,
            expected_target_realpath=expected_real if isinstance(expected_real, str) else None,
            repo_root=str(repo_root) if isinstance(repo_root, str) else None,
        )
        if problem == "missing_ancestor":
            continue
        if problem is not None or not dst:
            tampered.append(rel)
            continue
        if not os.path.lexists(dst) and not _is_link_or_junction(dst):
            continue
        if _is_link_or_junction(dst) and isinstance(expected_real, str) and _link_points_to_expected(dst, expected_real):
            try:
                _remove_dir_link(dst)
                removed.append(rel)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        else:
            tampered.append(rel)

    if not tampered and not errors:
        if os.path.isfile(meta_path) and not _is_link_or_junction(meta_path):
            try:
                os.unlink(meta_path)
            except OSError:
                pass
    return {
        "present": True,
        "removed": removed,
        "tampered": tampered,
        "errors": errors,
    }

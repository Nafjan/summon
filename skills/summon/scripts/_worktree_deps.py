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
* Path validation rejects absolute paths, drive letters, ``..`` traversal, and
  ``.git`` / ``.claude`` segments. Base targets must exist as real directories
  (never symlinks or junctions) inside the repository root.
* Pre- and post-dispatch fingerprints cover nearby lockfiles and bounded
  directory metadata. Unacknowledged mutation after dispatch fails closed
  (``error_kind="linked_deps_mutated"``).
* Intact Summon-created dependency links are excluded from untracked file lists
  in workspace snapshots, progress fingerprints, and worktree GC, and are
  cleanly unlinked before worktree removal.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import uuid
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable

SCHEMA = "summon.worktree-deps/v1"
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
MAX_SCANDIR_ENTRIES = 512
_FORBIDDEN_SEGMENTS = frozenset({"", ".", "..", ".git", ".claude"})


class WorktreeDepsError(ValueError):
    """Typed failure for ``--link-deps`` validation, bridging, or integrity."""

    def __init__(self, kind: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


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
            if seg in _FORBIDDEN_SEGMENTS or seg.lower() in {".git", ".claude"}:
                raise WorktreeDepsError(
                    "link_deps_invalid_path",
                    f"--link-deps path {token!r} contains forbidden segment {seg!r}",
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
    repo_real = os.path.realpath(repo_root)
    if _is_link_or_junction(repo_root):
        raise WorktreeDepsError(
            "link_deps_invalid_target",
            "--link-deps refuses repository root that is a symlink or junction",
        )
    cur = repo_root
    for seg in rel.split("/"):
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


def _hash_regular_file(path: str) -> str | None:
    try:
        if _is_link_or_junction(path):
            return "link"
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > MAX_LOCKFILE_BYTES:
            return f"oversize:{st.st_size}:{st.st_mtime_ns}"
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _scandir_digest(directory: str, *, depth: int = 1) -> str:
    h = hashlib.sha256()
    try:
        entries = []
        total = 0
        with os.scandir(directory) as it:
            for entry in it:
                total += 1
                entries.append(entry)
        entries.sort(key=lambda e: e.name)
        h.update(f"total:{total}\0".encode("utf-8"))
        for entry in entries[:MAX_SCANDIR_ENTRIES]:
            try:
                is_sym = entry.is_symlink()
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and not is_sym:
                    # Directory st_size/st_mtime_ns on Windows NTFS can update
                    # asynchronously when background indexers/scanners inspect a
                    # newly created folder. Hash directory names/structure and
                    # regular-file stats instead.
                    rec = f"D\0{entry.name}\n"
                    h.update(rec.encode("utf-8", errors="surrogateescape"))
                    if depth > 0:
                        sub_digest = _scandir_digest(entry.path, depth=depth - 1)
                        h.update(f"S\0{entry.name}\0{sub_digest}\n".encode("utf-8", errors="surrogateescape"))
                else:
                    st = entry.stat(follow_symlinks=False)
                    rec = f"F\0{entry.name}\0{int(is_sym)}\0{st.st_size}\0{st.st_mtime_ns}\n"
                    h.update(rec.encode("utf-8", errors="surrogateescape"))
            except OSError:
                rec = f"{entry.name}\0err\n"
                h.update(rec.encode("utf-8", errors="surrogateescape"))
    except OSError:
        h.update(b"missing")
    return h.hexdigest()


def fingerprint_dep_target(repo_root: str, rel: str) -> str:
    """Compute a bounded SHA-256 fingerprint of a dependency directory and nearby lockfiles."""
    target = os.path.join(repo_root, *rel.split("/"))
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
            if digest is not None:
                h.update(f"lock:{label}:{lock_name}:{digest}\0".encode("utf-8"))

    for inner_name in INNER_STATE_FILES:
        ipath = os.path.join(target, inner_name)
        digest = _hash_regular_file(ipath)
        if digest is not None:
            h.update(f"inner:{inner_name}:{digest}\0".encode("utf-8"))

    h.update(f"top:{_scandir_digest(target)}\0".encode("utf-8"))

    for sub_rel in (".bin", "Lib/site-packages", "Scripts", "bin"):
        sub_path = os.path.join(target, *sub_rel.split("/"))
        if os.path.isdir(sub_path) and not _is_link_or_junction(sub_path):
            h.update(f"sub:{sub_rel}:{_scandir_digest(sub_path)}\0".encode("utf-8"))
    lib_dir = os.path.join(target, "lib")
    if os.path.isdir(lib_dir) and not _is_link_or_junction(lib_dir):
        try:
            with os.scandir(lib_dir) as it:
                py_dirs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))[:8]
            for py_name in py_dirs:
                sp = os.path.join(lib_dir, py_name, "site-packages")
                if os.path.isdir(sp) and not _is_link_or_junction(sp):
                    h.update(f"sub:lib/{py_name}/site-packages:{_scandir_digest(sp)}\0".encode("utf-8"))
        except OSError:
            pass
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
                # Main repository checkout, not a linked worktree.
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


def _write_metadata_atomic(meta_path: str, payload: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    tmp_path = f"{meta_path}.{uuid.uuid4().hex}.tmp"
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
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


def _read_metadata(worktree_path: str) -> tuple[str | None, dict[str, Any] | None]:
    found = _find_worktree_root_and_gitdir(worktree_path)
    if found is None:
        return None, None
    wt_root, gitdir = found
    meta_path = os.path.join(gitdir, METADATA_FILENAME)
    if not os.path.isfile(meta_path) or _is_link_or_junction(meta_path):
        return wt_root, None
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return wt_root, None
        return wt_root, data
    except (OSError, ValueError):
        return wt_root, None


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
        _write_metadata_atomic(meta_path, private_meta)
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

    return {
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
    }


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
        dst = os.path.join(wt_root, *rel.split("/"))
        if _link_points_to_expected(dst, expected_real):
            intact.add(rel)
    return frozenset(intact)


def is_excluded_untracked_path(rel_path: str, intact_deps: frozenset[str]) -> bool:
    """True when ``rel_path`` (from git status porcelain) is an intact bridged dependency link."""
    if not intact_deps or not rel_path:
        return False
    norm = rel_path.replace("\\", "/").rstrip("/")
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
            raw_path = line[3:].strip()
            if raw_path.startswith('"') and raw_path.endswith('"') and len(raw_path) >= 2:
                raw_path = raw_path[1:-1]
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
    for token in tokens:
        if token.startswith(b"? "):
            rel = token[2:].decode("utf-8", errors="surrogateescape")
            if is_excluded_untracked_path(rel, intact):
                continue
        kept.append(token)
    return b"\0".join(kept)


def verify_linked_deps(worktree_info: dict[str, Any] | None) -> dict[str, Any] | None:
    """Re-verify linked dependency fingerprints and link integrity after dispatch."""
    if not isinstance(worktree_info, dict):
        return None
    wt_path = worktree_info.get("path") or worktree_info.get("cwd")
    if not isinstance(wt_path, str) or not wt_path:
        return None
    wt_root, meta = _read_metadata(wt_path)
    if not wt_root or not isinstance(meta, dict):
        existing = worktree_info.get("linked_deps")
        return existing if isinstance(existing, dict) else None

    repo_root = meta.get("repo_root") or worktree_info.get("repo")
    paths = [p for p in (meta.get("paths") or ()) if isinstance(p, str)]
    targets = meta.get("targets") if isinstance(meta.get("targets"), dict) else {}
    before_sha256 = meta.get("before_sha256")
    allow_mutation = bool(meta.get("allow_mutation", False))
    strategy = meta.get("strategy") if isinstance(meta.get("strategy"), str) else (
        "junction" if os.name == "nt" else "symlink"
    )

    after_per_path: dict[str, str] = {}
    mutated_paths: list[str] = []
    links_intact = True

    for rel in paths:
        tinfo = targets.get(rel) if isinstance(targets.get(rel), dict) else {}
        expected_real = tinfo.get("target_realpath")
        before_fp = tinfo.get("before_sha256")
        dst = os.path.join(wt_root, *rel.split("/"))
        intact = (isinstance(expected_real, str)
                  and bool(expected_real)
                  and _link_points_to_expected(dst, expected_real))
        if not intact:
            links_intact = False
        after_fp = (fingerprint_dep_target(str(repo_root), rel)
                    if isinstance(repo_root, str) and repo_root else "missing")
        after_per_path[rel] = after_fp
        if after_fp != before_fp or not intact:
            mutated_paths.append(rel)

    after_sha256 = _overall_fingerprint(after_per_path)
    deps_mutated = bool(after_sha256 != before_sha256 or not links_intact or mutated_paths)
    projection = {
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
    }
    worktree_info["linked_deps"] = projection
    return projection


def validate_projection(record: Any) -> dict[str, Any]:
    """Validate a public ``summon.worktree-deps/v1`` projection dict."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise WorktreeDepsError("link_deps_projection_invalid", "invalid summon.worktree-deps/v1 record")
    required = {
        "schema", "count", "paths", "strategy", "allow_mutation",
        "before_sha256", "after_sha256", "deps_mutated", "links_intact", "mutated_paths",
    }
    if not required.issubset(record.keys()):
        raise WorktreeDepsError("link_deps_projection_invalid", "missing required summon.worktree-deps/v1 fields")
    return dict(record)


def teardown_linked_deps(worktree_path: str) -> dict[str, Any]:
    """Safely unlink Summon-created dependency junctions/symlinks before worktree removal.

    Never deletes a real directory if an agent replaced a link with a real directory;
    reports ``tampered`` instead so the caller can preserve the worktree.
    """
    wt_root, meta = _read_metadata(worktree_path)
    if not wt_root or not isinstance(meta, dict):
        return {"present": False, "removed": [], "tampered": [], "errors": []}
    paths = [p for p in (meta.get("paths") or ()) if isinstance(p, str)]
    removed: list[str] = []
    tampered: list[str] = []
    errors: list[str] = []
    for rel in paths:
        dst = os.path.join(wt_root, *rel.split("/"))
        if not os.path.lexists(dst) and not _is_link_or_junction(dst):
            continue
        if _is_link_or_junction(dst):
            try:
                _remove_dir_link(dst)
                removed.append(rel)
            except OSError as exc:
                errors.append(f"{rel}: {exc}")
        else:
            tampered.append(rel)
    if not tampered and not errors:
        meta_path = _metadata_path(wt_root)
        if meta_path and os.path.isfile(meta_path):
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

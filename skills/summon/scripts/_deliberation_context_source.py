"""Bounded local source observation for durable deliberation context.

Only local Git metadata or an explicitly named revision-manifest file is read.
The module never contacts a provider, repairs authentication, changes routing,
or follows a shell.  Callers inject a runner in tests.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from _deliberation_context import (MAX_INPUT_BYTES, OBSERVATION_SCHEMA,
                                   ContextFreshnessError, ParsedContext)


MAX_GIT_OUTPUT_BYTES = 1024 * 1024
MAX_GIT_CONTENT_BYTES = 256 * 1024 * 1024
MAX_GIT_FILES = 50_000
GIT_TIMEOUT_SECONDS = 10
_HEX_REV = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _local_path(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContextFreshnessError(f"{label} is unavailable")
    normalized = value.replace("/", "\\")
    if os.name == "nt" and normalized.startswith("\\\\"):
        raise ContextFreshnessError(f"{label} must be local")
    return os.path.abspath(value)


def git_source_digest(revision: str, worktree_evidence: bytes) -> str:
    if not isinstance(revision, str) or _HEX_REV.fullmatch(revision) is None:
        raise ContextFreshnessError("git source revision is invalid")
    if (not isinstance(worktree_evidence, bytes)
            or len(worktree_evidence) > MAX_GIT_OUTPUT_BYTES):
        raise ContextFreshnessError("git worktree evidence is outside its bound")
    return hashlib.sha256(
        b"summon.git-source/v2\0" + revision.encode("ascii") + b"\0"
        + worktree_evidence
    ).hexdigest()


def _run_git(arguments: tuple[str, ...], cwd: str, runner: Callable | None) -> tuple[int, bytes]:
    executable = shutil.which("git")
    if not executable:
        raise ContextFreshnessError("git source observer is unavailable")
    # Windows executable lookup may prefer the current directory.  The source
    # root is untrusted input, so never execute a repository-provided git shim.
    executable_real = os.path.realpath(os.path.abspath(executable))
    cwd_real = os.path.realpath(os.path.abspath(cwd))
    try:
        common = os.path.commonpath((cwd_real, executable_real))
    except ValueError:
        # On Windows, different drives have no common path.  That necessarily
        # places the executable outside the untrusted source root.
        common = None
    if common == cwd_real:
        raise ContextFreshnessError("git source observer resolved inside the source root")
    command = (executable_real, *arguments)
    try:
        if runner is None:
            from _spawn import popen_flags
            process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, **popen_flags())
            chunks: list[bytes] = []
            size = [0]
            overflow = threading.Event()
            read_error: list[BaseException] = []

            def drain() -> None:
                try:
                    assert process.stdout is not None
                    while True:
                        chunk = process.stdout.read(64 * 1024)
                        if not chunk:
                            return
                        size[0] += len(chunk)
                        if size[0] > MAX_GIT_OUTPUT_BYTES:
                            overflow.set()
                            return
                        chunks.append(chunk)
                except BaseException as exc:
                    read_error.append(exc)

            reader = threading.Thread(target=drain, name="summon-git-reader",
                                      daemon=True)
            reader.start()
            deadline = time.monotonic() + GIT_TIMEOUT_SECONDS
            timed_out = False
            while process.poll() is None and not overflow.is_set():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                overflow.wait(0.01)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=1.0)
            reader.join(timeout=1.0)
            if reader.is_alive() or read_error:
                raise ContextFreshnessError("git source output could not be bounded")
            if timed_out:
                raise ContextFreshnessError("git source observation timed out")
            if overflow.is_set():
                raise ContextFreshnessError("git source output exceeds its bound")
            result = type("GitResult", (), {
                "returncode": process.returncode, "stdout": b"".join(chunks)})()
        else:
            result = runner(command, cwd=cwd, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise ContextFreshnessError("git source observation failed") from exc
    code = getattr(result, "returncode", None)
    stdout = getattr(result, "stdout", b"")
    if isinstance(code, bool) or not isinstance(code, int):
        raise ContextFreshnessError("git source observer returned an invalid exit")
    if isinstance(stdout, str):
        try:
            stdout = stdout.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ContextFreshnessError("git source output is invalid") from exc
    if not isinstance(stdout, bytes) or len(stdout) > MAX_GIT_OUTPUT_BYTES:
        raise ContextFreshnessError("git source output exceeds its bound")
    return code, stdout


def _worktree_evidence(cwd: str, tracked_rows: bytes,
                       untracked_rows: bytes) -> bytes:
    root = os.path.realpath(cwd)
    entries: list[tuple[bytes, bytes]] = []
    for row in tracked_rows.split(b"\0"):
        if not row:
            continue
        try:
            metadata, raw_path = row.split(b"\t", 1)
            mode, _object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ContextFreshnessError("git tracked source list is invalid") from exc
        if stage != b"0":
            raise ContextFreshnessError("git source has an unresolved index")
        if mode == b"160000":
            raise ContextFreshnessError("git submodule source is unsupported")
        if mode not in {b"100644", b"100755"}:
            raise ContextFreshnessError("git tracked source type is unsupported")
        entries.append((b"tracked:" + mode, raw_path))
    for raw_path in untracked_rows.split(b"\0"):
        if raw_path:
            entries.append((b"untracked", raw_path))
    if len(entries) > MAX_GIT_FILES:
        raise ContextFreshnessError("git source file count exceeds its bound")

    digest = hashlib.sha256(b"summon.worktree-files/v1\0")
    consumed = 0
    seen: set[bytes] = set()
    for kind, raw_path in entries:
        if not raw_path or b"\0" in raw_path:
            raise ContextFreshnessError("git source path is invalid")
        if raw_path in seen:
            raise ContextFreshnessError("git source path is duplicated")
        seen.add(raw_path)
        try:
            relative = os.fsdecode(raw_path)
        except UnicodeError as exc:
            raise ContextFreshnessError("git source path is invalid") from exc
        if os.path.isabs(relative) or os.path.splitdrive(relative)[0]:
            raise ContextFreshnessError("git source path is not relative")
        candidate = os.path.abspath(os.path.join(root, relative))
        target = os.path.realpath(candidate)
        try:
            if os.path.commonpath((root, target)) != root:
                raise ContextFreshnessError("git source path escaped the source root")
            if not os.path.lexists(candidate):
                if kind.startswith(b"tracked:"):
                    digest.update(kind + b"\0" + raw_path + b"\0missing\0")
                    continue
                raise ContextFreshnessError("git untracked source disappeared")
            before = os.lstat(candidate)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise ContextFreshnessError("git source is not a regular file")
            consumed += before.st_size
            if consumed > MAX_GIT_CONTENT_BYTES:
                raise ContextFreshnessError("git source content exceeds its bound")
            file_digest = hashlib.sha256()
            with open(candidate, "rb") as handle:
                remaining = before.st_size
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    file_digest.update(chunk)
                    remaining -= len(chunk)
                extra = handle.read(1)
                after = os.fstat(handle.fileno())
        except ContextFreshnessError:
            raise
        except (OSError, ValueError) as exc:
            raise ContextFreshnessError("git source could not be read") from exc
        if (remaining != 0 or extra or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            raise ContextFreshnessError("git source changed during readback")
        digest.update(kind)
        digest.update(b"\0")
        digest.update(len(raw_path).to_bytes(4, "big"))
        digest.update(raw_path)
        digest.update(file_digest.digest())
    return digest.digest()


def _git_state(cwd: str, runner: Callable | None) -> tuple[str, bytes]:
    code, raw_head = _run_git(("rev-parse", "--verify", "HEAD"), cwd, runner)
    try:
        head = raw_head.decode("ascii", errors="strict").strip().lower()
    except UnicodeError as exc:
        raise ContextFreshnessError("git source revision is invalid") from exc
    if code != 0 or _HEX_REV.fullmatch(head) is None:
        raise ContextFreshnessError("git source revision could not be verified")
    code, tracked = _run_git(("ls-files", "--stage", "-z"), cwd, runner)
    if code != 0:
        raise ContextFreshnessError("git tracked source list could not be verified")
    code, untracked = _run_git(
        ("ls-files", "--others", "--exclude-standard", "-z"), cwd, runner)
    if code != 0:
        raise ContextFreshnessError("git untracked source list could not be verified")
    return head, _worktree_evidence(cwd, tracked, untracked)


def capture_git_source(cwd: str, *, unix_now_ms: int,
                       runner: Callable | None = None) -> dict:
    """Capture the exact source block used by a future context packet."""
    if isinstance(unix_now_ms, bool) or not isinstance(unix_now_ms, int) or unix_now_ms < 0:
        raise ContextFreshnessError("git source capture clock is invalid")
    root = _local_path(cwd, "git source root")
    if not os.path.isdir(root):
        raise ContextFreshnessError("git source root is unavailable")
    revision, evidence = _git_state(root, runner)
    return {
        "kind": "git_commit",
        "revision": revision,
        "source_digest": git_source_digest(revision, evidence),
        "captured_at_unix_ms": unix_now_ms,
    }


def _relation(captured: str, current: str, cwd: str,
              runner: Callable | None) -> tuple[str, int]:
    if captured == current:
        return "same", 0
    code, _ = _run_git(("merge-base", "--is-ancestor", captured, current), cwd, runner)
    if code == 0:
        count_code, raw_count = _run_git(
            ("rev-list", "--count", f"{captured}..{current}"), cwd, runner)
        try:
            count = int(raw_count.decode("ascii", errors="strict").strip())
        except (UnicodeError, ValueError) as exc:
            raise ContextFreshnessError("git revision delta is invalid") from exc
        if count_code != 0 or not 1 <= count <= 1_000_000_000:
            raise ContextFreshnessError("git revision delta is outside its bound")
        return "descendant", count
    if code != 1:
        return "unknown", 0
    reverse, _ = _run_git(("merge-base", "--is-ancestor", current, captured), cwd, runner)
    if reverse == 0:
        return "rewound", 0
    return ("diverged", 0) if reverse == 1 else ("unknown", 0)


def _manifest_observation(parsed: ParsedContext, path: str, now: int) -> dict:
    target = Path(_local_path(path, "revision manifest"))
    if not target.is_absolute():
        raise ContextFreshnessError("revision manifest path must be absolute")
    try:
        before = target.lstat()
        if target.is_symlink() or not target.is_file() or before.st_size > MAX_INPUT_BYTES:
            raise ContextFreshnessError("revision manifest is not a bounded regular file")
        with target.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
            after = os.fstat(handle.fileno())
    except ContextFreshnessError:
        raise
    except OSError as exc:
        raise ContextFreshnessError("revision manifest could not be read") from exc
    if (len(raw) > MAX_INPUT_BYTES or before.st_dev != after.st_dev
            or before.st_ino != after.st_ino or before.st_size != after.st_size):
        raise ContextFreshnessError("revision manifest changed during readback")
    digest = hashlib.sha256(raw).hexdigest()
    current_revision = "sha256:" + digest
    same = current_revision == parsed.source["revision"]
    return {
        "schema": OBSERVATION_SCHEMA,
        "kind": "revision_manifest",
        "captured_revision": parsed.source["revision"],
        "current_revision": current_revision,
        "current_source_digest": digest,
        "relation": "same" if same else "unknown",
        "revision_delta": 0,
        "verification_method": "sha256-readback",
        "observed_at_unix_ms": now,
    }


def observe_source(parsed: ParsedContext, *, cwd: str, unix_now_ms: int,
                   manifest_path: str | None = None,
                   runner: Callable | None = None) -> dict:
    """Observe the packet's source without provider contact or caller assertions."""
    if not isinstance(parsed, ParsedContext):
        raise ContextFreshnessError("source observation requires a parsed context packet")
    if isinstance(unix_now_ms, bool) or not isinstance(unix_now_ms, int) or unix_now_ms < 0:
        raise ContextFreshnessError("source observation clock is invalid")
    kind = parsed.source["kind"]
    if kind == "revision_manifest":
        if not manifest_path:
            raise ContextFreshnessError("revision manifest observation needs a file")
        return _manifest_observation(parsed, manifest_path, unix_now_ms)
    if manifest_path is not None:
        raise ContextFreshnessError("git context does not accept a revision manifest file")
    root = _local_path(cwd, "git source root")
    if not os.path.isdir(root):
        raise ContextFreshnessError("git source root is unavailable")
    current, evidence = _git_state(root, runner)
    relation, delta = _relation(parsed.source["revision"], current, root, runner)
    return {
        "schema": OBSERVATION_SCHEMA,
        "kind": "git_commit",
        "captured_revision": parsed.source["revision"],
        "current_revision": current,
        "current_source_digest": git_source_digest(current, evidence),
        "relation": relation,
        "revision_delta": delta,
        "verification_method": "git-readback",
        "observed_at_unix_ms": unix_now_ms,
    }


__all__ = ["capture_git_source", "git_source_digest", "observe_source"]

"""Provider-inert tests for durable-context source readback."""
from __future__ import annotations

import hashlib
import pathlib
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)) if str(HERE) not in sys.path else None

from _deliberation_context import ContextFreshnessError, parse_context_packet  # noqa: E402
from _deliberation_context_source import (  # noqa: E402
    _run_git,
    _worktree_evidence,
    MAX_GIT_OUTPUT_BYTES,
    capture_git_source,
    git_source_digest,
    observe_source,
)

NOW = 5_000_000
OLD, NEW = "a" * 40, "b" * 40


class FakeGit:
    def __init__(self, rows):
        self.rows = {tuple(k): list(v) for k, v in rows.items()}
        self.calls = []

    def __call__(self, command, *, cwd, timeout):
        key = tuple(command[1:])
        self.calls.append((key, cwd, timeout))
        code, stdout = self.rows[key].pop(0)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=b"PRIVATE")


def context(source):
    return parse_context_packet({
        "schema": "summon.deliberation-context/v1", "source": source,
        "freshness_policy": {"fresh_max_age_ms": 1_000,
                             "hard_max_age_ms": 20_000,
                             "hard_max_revision_delta": 5},
        "entries": [{"id": "decision-1", "kind": "decision",
                     "body": "Ship after review.",
                     "provenance": {"kind": "authored",
                                    "source_sha256": "1" * 64}}],
    })


def test_git_capture_and_same_observation(tmp_path):
    (tmp_path / "tracked.py").write_text("changed", encoding="utf-8")
    tracked = b"100644 " + b"1" * 40 + b" 0\ttracked.py\0"
    evidence = _worktree_evidence(str(tmp_path), tracked, b"")
    fake = FakeGit({
        ("rev-parse", "--verify", "HEAD"): [(0, (OLD + "\n").encode())] * 2,
        ("ls-files", "--stage", "-z"): [(0, tracked)] * 2,
        ("ls-files", "--others", "--exclude-standard", "-z"):
            [(0, b"")] * 2})
    source = capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)
    assert source["source_digest"] == git_source_digest(OLD, evidence)
    observed = observe_source(context(source), cwd=str(tmp_path),
                              unix_now_ms=NOW + 1, runner=fake)
    assert (observed["relation"], observed["revision_delta"]) == ("same", 0)
    assert all(call[2] == 10 for call in fake.calls)


def test_git_descendant_delta_is_locally_derived(tmp_path):
    source = {"kind": "git_commit", "revision": OLD,
              "source_digest": "2" * 64, "captured_at_unix_ms": NOW}
    fake = FakeGit({
        ("rev-parse", "--verify", "HEAD"): [(0, (NEW + "\n").encode())],
        ("ls-files", "--stage", "-z"): [(0, b"")],
        ("ls-files", "--others", "--exclude-standard", "-z"): [(0, b"")],
        ("merge-base", "--is-ancestor", OLD, NEW): [(0, b"")],
        ("rev-list", "--count", f"{OLD}..{NEW}"): [(0, b"3\n")]})
    observed = observe_source(context(source), cwd=str(tmp_path),
                              unix_now_ms=NOW + 1, runner=fake)
    assert (observed["relation"], observed["revision_delta"]) == ("descendant", 3)


@pytest.mark.parametrize("forward,reverse,expected", [(1, 0, "rewound"),
                                                        (1, 1, "diverged"),
                                                        (2, 2, "unknown")])
def test_unbounded_git_relations_are_explicit(tmp_path, forward, reverse, expected):
    source = {"kind": "git_commit", "revision": OLD,
              "source_digest": "2" * 64, "captured_at_unix_ms": NOW}
    fake = FakeGit({
        ("rev-parse", "--verify", "HEAD"): [(0, (NEW + "\n").encode())],
        ("ls-files", "--stage", "-z"): [(0, b"")],
        ("ls-files", "--others", "--exclude-standard", "-z"): [(0, b"")],
        ("merge-base", "--is-ancestor", OLD, NEW): [(forward, b"")],
        ("merge-base", "--is-ancestor", NEW, OLD): [(reverse, b"")]})
    assert observe_source(context(source), cwd=str(tmp_path),
                          unix_now_ms=NOW, runner=fake)["relation"] == expected


def test_manifest_is_hash_read_back_and_change_is_unknown(tmp_path):
    raw = b'{"source":"manifest"}\n'
    path = tmp_path / "revision.json"
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    parsed = context({"kind": "revision_manifest",
                      "revision": "sha256:" + digest,
                      "source_digest": digest, "captured_at_unix_ms": NOW})
    assert observe_source(parsed, cwd=str(tmp_path), unix_now_ms=NOW,
                          manifest_path=str(path))["relation"] == "same"
    path.write_bytes(raw + b"changed")
    assert observe_source(parsed, cwd=str(tmp_path), unix_now_ms=NOW + 1,
                          manifest_path=str(path))["relation"] == "unknown"


def test_manifest_identity_must_equal_source_digest():
    with pytest.raises(ContextFreshnessError, match="identity"):
        context({"kind": "revision_manifest", "revision": "sha256:" + "3" * 64,
                 "source_digest": "4" * 64, "captured_at_unix_ms": NOW})


def test_git_failures_do_not_echo_stderr_or_local_path(tmp_path):
    fake = FakeGit({("rev-parse", "--verify", "HEAD"): [(1, b"")]})
    with pytest.raises(ContextFreshnessError) as caught:
        capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)
    assert "PRIVATE" not in str(caught.value)
    assert str(tmp_path) not in str(caught.value)


def test_same_status_class_but_changed_tracked_bytes_changes_digest(tmp_path):
    target = tmp_path / "x"
    target.write_text("first", encoding="utf-8")
    tracked = b"100644 " + b"1" * 40 + b" 0\tx\0"
    fake = FakeGit({
        ("rev-parse", "--verify", "HEAD"): [(0, (OLD + "\n").encode())] * 2,
        ("ls-files", "--stage", "-z"): [(0, tracked), (0, tracked)],
        ("ls-files", "--others", "--exclude-standard", "-z"):
            [(0, b""), (0, b"")],
    })
    captured = capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)
    target.write_text("second", encoding="utf-8")
    observed = observe_source(context(captured), cwd=str(tmp_path),
                              unix_now_ms=NOW + 1, runner=fake)
    assert observed["relation"] == "same"
    assert observed["current_source_digest"] != captured["source_digest"]


def test_untracked_file_content_is_bound_and_revalidated(tmp_path):
    untracked = tmp_path / "draft.py"
    untracked.write_text("first", encoding="utf-8")
    fake = FakeGit({
        ("rev-parse", "--verify", "HEAD"): [(0, (OLD + "\n").encode())] * 2,
        ("ls-files", "--stage", "-z"): [(0, b""), (0, b"")],
        ("ls-files", "--others", "--exclude-standard", "-z"):
            [(0, b"draft.py\0"), (0, b"draft.py\0")],
    })
    captured = capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)
    untracked.write_text("second", encoding="utf-8")
    observed = observe_source(context(captured), cwd=str(tmp_path),
                              unix_now_ms=NOW + 1, runner=fake)
    assert observed["current_source_digest"] != captured["source_digest"]


def test_windows_unc_source_paths_are_refused_before_filesystem_access(
        monkeypatch, tmp_path):
    import _deliberation_context_source as source

    monkeypatch.setattr(source.os, "name", "nt")
    with pytest.raises(ContextFreshnessError, match="local"):
        capture_git_source(r"\\server\share\repo", unix_now_ms=NOW)
    parsed = context({"kind": "revision_manifest",
                      "revision": "sha256:" + "3" * 64,
                      "source_digest": "3" * 64,
                      "captured_at_unix_ms": NOW})
    with pytest.raises(ContextFreshnessError, match="local"):
        observe_source(parsed, cwd=str(tmp_path), unix_now_ms=NOW,
                       manifest_path=r"\\?\C:\private\manifest.json")


def test_git_output_and_special_tracked_types_fail_closed(tmp_path):
    huge = b"x" * (MAX_GIT_OUTPUT_BYTES + 1)
    fake = FakeGit({("rev-parse", "--verify", "HEAD"): [(0, huge)]})
    with pytest.raises(ContextFreshnessError, match="exceeds"):
        capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)

    for mode, message in ((b"160000", "submodule"), (b"120000", "type")):
        tracked = mode + b" " + b"1" * 40 + b" 0\tentry\0"
        fake = FakeGit({
            ("rev-parse", "--verify", "HEAD"): [(0, (OLD + "\n").encode())],
            ("ls-files", "--stage", "-z"): [(0, tracked)],
            ("ls-files", "--others", "--exclude-standard", "-z"): [(0, b"")],
        })
        with pytest.raises(ContextFreshnessError, match=message):
            capture_git_source(str(tmp_path), unix_now_ms=NOW, runner=fake)


def test_git_subprocess_path_has_stream_cap_and_discards_stderr():
    source = pathlib.Path(__import__("_deliberation_context_source").__file__).read_text(
        encoding="utf-8")
    assert "subprocess.run(" not in source
    assert "stderr=subprocess.DEVNULL" in source
    assert "process.kill()" in source
    assert "MAX_GIT_OUTPUT_BYTES" in source


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, chunks, *, running=False):
        self.stdout = _FakeStdout(chunks)
        self.returncode = None if running else 0
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("git", timeout)
        return self.returncode


def test_real_git_subprocess_path_is_bounded_and_shell_free(monkeypatch, tmp_path):
    import _deliberation_context_source as source

    fake = _FakeProcess([b"abc", b"def"])
    observed = {}

    def popen(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return fake

    monkeypatch.setattr(source.shutil, "which", lambda _name: r"C:\Program Files\Git\cmd\git.exe")
    monkeypatch.setattr(source.subprocess, "Popen", popen)
    code, output = _run_git(("status",), str(tmp_path), None)
    assert (code, output) == (0, b"abcdef")
    assert observed["shell"] is False
    assert observed["stderr"] is subprocess.DEVNULL
    assert observed["stdin"] is subprocess.DEVNULL


def test_real_git_subprocess_timeout_and_overflow_kill_child(monkeypatch, tmp_path):
    import _deliberation_context_source as source

    monkeypatch.setattr(source.shutil, "which", lambda _name: r"C:\Program Files\Git\cmd\git.exe")
    timed = _FakeProcess([b""], running=True)
    monkeypatch.setattr(source.subprocess, "Popen", lambda *_a, **_k: timed)
    monkeypatch.setattr(source, "GIT_TIMEOUT_SECONDS", 0)
    with pytest.raises(ContextFreshnessError, match="timed out"):
        _run_git(("status",), str(tmp_path), None)
    assert timed.killed is True

    overflow = _FakeProcess([b"123456789"])
    monkeypatch.setattr(source.subprocess, "Popen", lambda *_a, **_k: overflow)
    monkeypatch.setattr(source, "GIT_TIMEOUT_SECONDS", 10)
    monkeypatch.setattr(source, "MAX_GIT_OUTPUT_BYTES", 8)
    with pytest.raises(ContextFreshnessError, match="exceeds"):
        _run_git(("status",), str(tmp_path), None)


def test_repository_local_git_executable_is_refused(monkeypatch, tmp_path):
    import _deliberation_context_source as source

    monkeypatch.setattr(source.shutil, "which", lambda _name: str(tmp_path / "git.exe"))
    with pytest.raises(ContextFreshnessError, match="inside the source root"):
        _run_git(("status",), str(tmp_path), None)


def test_cross_drive_git_executable_is_allowed(monkeypatch, tmp_path):
    import _deliberation_context_source as source

    fake = _FakeProcess([b"ok"])
    monkeypatch.setattr(source.shutil, "which", lambda _name: r"C:\Program Files\Git\cmd\git.exe")
    monkeypatch.setattr(source.os.path, "commonpath",
                        lambda _paths: (_ for _ in ()).throw(ValueError("different drives")))
    monkeypatch.setattr(source.subprocess, "Popen", lambda *_a, **_k: fake)
    assert _run_git(("status",), str(tmp_path), None) == (0, b"ok")


def test_worktree_fail_closed_edge_matrix(monkeypatch, tmp_path):
    import _deliberation_context_source as source

    missing = b"100644 " + b"1" * 40 + b" 0\tmissing.py\0"
    assert isinstance(_worktree_evidence(str(tmp_path), missing, b""), bytes)

    unresolved = b"100644 " + b"1" * 40 + b" 1\tconflict.py\0"
    with pytest.raises(ContextFreshnessError, match="unresolved"):
        _worktree_evidence(str(tmp_path), unresolved, b"")

    escaped = os.fsencode(os.path.join("..", "outside.py"))
    tracked_escape = b"100644 " + b"1" * 40 + b" 0\t" + escaped + b"\0"
    with pytest.raises(ContextFreshnessError, match="escaped"):
        _worktree_evidence(str(tmp_path), tracked_escape, b"")

    (tmp_path / "directory").mkdir()
    directory = b"100644 " + b"1" * 40 + b" 0\tdirectory\0"
    with pytest.raises(ContextFreshnessError, match="regular file"):
        _worktree_evidence(str(tmp_path), directory, b"")

    (tmp_path / "large.py").write_bytes(b"x")
    large = b"100644 " + b"1" * 40 + b" 0\tlarge.py\0"
    monkeypatch.setattr(source, "MAX_GIT_CONTENT_BYTES", 0)
    with pytest.raises(ContextFreshnessError, match="content exceeds"):
        _worktree_evidence(str(tmp_path), large, b"")

    monkeypatch.setattr(source, "MAX_GIT_FILES", 0)
    with pytest.raises(ContextFreshnessError, match="file count"):
        _worktree_evidence(str(tmp_path), b"", b"untracked.py\0")

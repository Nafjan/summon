"""Provider-inert adversarial tests for the experimental portable projection."""

from __future__ import annotations

import json
import hashlib
import errno
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import _portable_result as portable
import _jobs


def _envelope(**changes):
    value = {
        "status": "success", "execution_status": "success", "attempts": 1,
        "attempt_status": "completed", "provider_contacted": True,
        "report_ok": True, "verdict": "pass", "error_kind": None,
        "raw_backend_exit_code": 0, "normalized_exit_code": 0,
        "agent": "reviewer", "provider": "anthropic", "cli": "claude",
        "transport": "subprocess",
        "model": {"requested": "claude-opus-5", "targeted": "claude-opus-5", "served": "claude-opus-5"},
        "served_model_evidence": "reported",
        "summon": {"version": "3.2.1", "scripts_sha256": "a" * 64},
        "artifacts": {"workspace_mutation": "none", "stability": "stable", "files": []},
    }
    value.update(changes)
    return value


def _project(envelope, root, *, source_sha256=None):
    if source_sha256 is None:
        exact = json.dumps(envelope, sort_keys=True, separators=(",", ":"),
                           allow_nan=False).encode("utf-8")
        source_sha256 = hashlib.sha256(exact).hexdigest()
    return portable.project_dispatch(
        envelope, root, source_sha256=source_sha256)


def _reseal(projection):
    projection["integrity"]["projection_sha256"] = portable.canonical_digest({
        **projection,
        "integrity": {key: value for key, value in projection["integrity"].items()
                      if key != "projection_sha256"},
    })
    return projection


def test_projection_is_bounded_recomputed_and_canonical(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "safe.txt").write_text("ok", encoding="utf-8")
    envelope = _envelope(
        result="private model transcript", prompt="private prompt", error="token=secret",
        session_id="session-private", pid=999, url="https://private.example",
        native_adapter_attested=True, model_match=False, named_model_verified=False,
        artifacts={"workspace_mutation": "none", "stability": "stable", "files": [{"path": "safe.txt", "sha256": "b" * 64}]},
    )
    out = _project(envelope, root)
    portable.validate_projection(out)
    rendered = json.dumps(out, sort_keys=True)
    for private in ("private model", "private prompt", "secret", "session-private", "999", "private.example", str(root)):
        assert private not in rendered
    assert out["source"]["surface"] == "dispatch"
    assert out["source"]["binding_sha256"] is None
    assert out["source"]["relationship"] == "external"
    assert out["source"]["native_adapter_attested"] is False
    assert out["source"]["receipt_sha256"] == portable.canonical_digest(envelope)
    assert out["model"]["model_match"] is True
    assert out["artifacts"]["files"] == [{"sha256": "b" * 64}]


def test_structural_refusal_recomputes_all_no_contact_facts(tmp_path):
    out = _project(_envelope(
        execution_status="not_run", attempts=9, attempt_status="completed", provider_contacted=True,
        model={"requested": "claude-opus-5", "targeted": "claude-opus-5", "served": "claude-opus-5"},
        served_model_evidence="reported",
        artifacts={"workspace_mutation": "observed", "stable_during_dispatch": True,
                   "bytes": 99, "files": [{"sha256": "f" * 64}]}), tmp_path)
    assert out["outcome"]["attempts"] == 0
    assert out["outcome"]["execution_status"] == out["outcome"]["attempt_status"] == "not_run"
    assert out["contact"]["provider_contacted"] is False
    assert out["model"]["served"] is None and out["model"]["served_model_evidence"] == "absent"
    assert out["model"]["model_match"] is None and out["model"]["named_model_verified"] is False
    assert out["outcome"]["report_ok"] is False and out["outcome"]["result_usable"] is False
    assert out["contact"]["retry_or_fallback"] == "none"
    assert out["artifacts"]["bytes"] is None and out["artifacts"]["files"] == []
    assert out["artifacts"]["workspace_mutation"] == "unknown"
    assert out["artifacts"]["artifact_stability"] == "unknown"


@pytest.mark.parametrize("path", [
    "safe.txt", "/tmp/x", "C:/x", "\\\\server\\x", "\\\\?\\C:\\x",
    "a/../b", "a//b", "a:stream", "a\n.txt", "",
])
def test_artifact_paths_are_never_exported_but_digest_is_preserved(tmp_path, path):
    out = _project(_envelope(artifacts={"files": [{
        "path": path, "sha256": "c" * 64, "bytes": 7,
        "media_type": "text/plain",
    }]}), tmp_path)
    assert out["artifacts"]["files"] == [{
        "sha256": "c" * 64, "bytes": 7, "media_type": "text/plain",
    }]


def test_artifact_limit_counts_valid_digests_not_leading_noise(tmp_path):
    files = ["path-only"] * 64 + [{"sha256": "d" * 64}]
    out = _project(_envelope(artifacts={"files": files}), tmp_path)
    assert out["artifacts"]["files"] == [{"sha256": "d" * 64}]


def test_public_forgery_unknown_fields_and_digest_are_rejected(tmp_path):
    out = _project(_envelope(), tmp_path)
    forged = json.loads(json.dumps(out))
    forged["model"]["served"] = None
    forged["model"]["model_match"] = True
    forged["model"]["named_model_verified"] = True
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged = json.loads(json.dumps(_project(_envelope(
        execution_status="not_run", attempts=0, attempt_status="not_run",
        provider_contacted=False), tmp_path)))
    forged["artifacts"]["files"] = [{"sha256": "f" * 64}]
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged = json.loads(json.dumps(_project(_envelope(
        execution_status="not_run", attempts=0, attempt_status="not_run",
        provider_contacted=False), tmp_path)))
    forged["contact"]["retry_or_fallback"] = "retried"
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged["contact"]["retry_or_fallback"] = "none"
    forged["outcome"]["status"] = "blocked"
    forged["outcome"]["verdict"] = "pass"
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged["outcome"]["verdict"] = None
    forged["outcome"]["status"] = "partial"
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged = json.loads(json.dumps(out))
    forged["artifacts"]["files"] = [{"path": "folder\\..\\private.txt"}]
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged = json.loads(json.dumps(out))
    forged["outcome"]["summary"] = "private"
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)
    forged = json.loads(json.dumps(out))
    forged["integrity"]["projection_sha256"] = "0" * 64
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)


@pytest.mark.parametrize(("section", "field", "numeric"), [
    ("outcome", "report_ok", 0),
    ("outcome", "report_ok", 1),
    ("outcome", "result_usable", 0),
    ("outcome", "result_usable", 1),
    ("contact", "provider_contacted", 0),
    ("contact", "provider_contacted", 1),
    ("model", "model_match", 0),
    ("model", "model_match", 1),
])
def test_strict_boolean_fields_reject_json_integers(
        tmp_path, section, field, numeric):
    forged = json.loads(json.dumps(_project(_envelope(), tmp_path)))
    forged[section][field] = numeric
    _reseal(forged)
    with pytest.raises(portable.PortableResultError):
        portable.validate_projection(forged)


def test_reported_mismatch_false_but_inferred_is_unverified(tmp_path):
    mismatch = _project(_envelope(model={"requested": "a", "targeted": "a", "served": "b"}), tmp_path)
    assert mismatch["model"]["model_match"] is False and mismatch["model"]["named_model_verified"] is False
    inferred = _project(_envelope(served_model_evidence="inferred"), tmp_path)
    assert inferred["model"]["model_match"] is None and inferred["model"]["named_model_verified"] is False


def test_missing_attempt_count_does_not_erase_explicit_contact(tmp_path):
    out = _project(_envelope(attempts=None, provider_contacted=True), tmp_path)
    assert out["contact"]["provider_contacted"] is True
    assert out["outcome"]["attempts"] is None
    assert out["outcome"]["attempt_status"] == "completed"
    assert out["outcome"]["execution_status"] == "success"


def test_missing_attempt_and_contact_evidence_remain_unknown(tmp_path):
    out = _project(_envelope(
        attempts=None, provider_contacted=None, attempt_status="unknown"), tmp_path)
    assert out["contact"]["provider_contacted"] is None
    assert out["outcome"]["attempts"] is None
    assert out["outcome"]["attempt_status"] == "unknown"


@pytest.mark.parametrize("status", ["running", "prepared", "unknown", None])
def test_projector_rejects_nonterminal_private_status(tmp_path, status):
    with pytest.raises(portable.PortableResultError, match="terminal"):
        _project(_envelope(status=status), tmp_path)


def test_nested_regular_artifact_is_reduced_to_content_identity(tmp_path):
    root = tmp_path / "repo"
    nested = root / "folder"
    nested.mkdir(parents=True)
    (nested / "result.json").write_text("{}", encoding="utf-8")
    out = _project(_envelope(artifacts={
        "files": [{"path": "folder/result.json", "sha256": "7" * 64}],
    }), root)
    assert out["artifacts"]["files"] == [{"sha256": "7" * 64}]


def test_explicit_private_receipt_digest_is_bound_and_checked(tmp_path):
    out = _project(_envelope(), tmp_path, source_sha256="e" * 64)
    assert out["source"]["receipt_sha256"] == "e" * 64
    forged = json.loads(json.dumps(out))
    forged["source"]["receipt_sha256"] = "f" * 64
    forged["integrity"]["projection_sha256"] = portable.canonical_digest({
        **forged, "integrity": {k: v for k, v in forged["integrity"].items()
                              if k != "projection_sha256"}})
    # A public receipt can be self-consistent, but this only checks its schema;
    # an external consumer must compare the bound digest to its private source.
    portable.validate_projection(forged)


def test_projector_requires_exact_private_receipt_digest(tmp_path):
    with pytest.raises(portable.PortableResultError):
        portable.project_dispatch(_envelope(), tmp_path, source_sha256=None)


def test_public_filesystem_error_never_exposes_local_path():
    private_path = r"C:\\Users\\owner\\private\\receipt.json"
    rendered = portable.public_error_message(
        OSError(2, "file unavailable", private_path))
    assert rendered == "portable result filesystem operation failed"
    assert private_path not in rendered


def test_workspace_and_stability_use_canonical_private_fields(tmp_path):
    out = _project(_envelope(
        workspace_evidence={"mutation": True},
        artifacts={"stable_during_dispatch": True, "bytes": 42, "files": []}), tmp_path)
    assert out["artifacts"] == {"workspace_mutation": "observed", "artifact_stability": "stable", "bytes": 42, "files": []}

    unchanged = _project(_envelope(workspace_evidence={"mutation": False}), tmp_path)
    unknown = _project(_envelope(workspace_evidence={"mutation": None}), tmp_path)
    assert unchanged["artifacts"]["workspace_mutation"] == "none"
    assert unknown["artifacts"]["workspace_mutation"] == "unknown"


def test_retry_state_uses_an_actual_multi_attempt_count(tmp_path):
    one = _project(_envelope(), tmp_path)
    many = _project(_envelope(attempts=2), tmp_path)
    assert one["contact"]["retry_or_fallback"] == "none"
    assert many["contact"]["retry_or_fallback"] == "retried"


def test_nonfinite_and_overlong_values_cannot_enter_projection(tmp_path):
    out = _project(_envelope(raw_backend_exit_code=float("nan"), agent="a" * 500), tmp_path,
                   source_sha256="d" * 64)
    assert out["outcome"]["raw_backend_exit_code"] is None
    assert out["attestation"]["agent"] is None
    portable.validate_projection(out)


def test_strict_loader_rejects_duplicate_nonfinite_and_oversized_json(tmp_path):
    projected = _project(_envelope(), tmp_path)
    encoded = json.dumps(projected).encode("utf-8")
    assert portable.load_projection_bytes(encoded) == projected
    with pytest.raises(portable.PortableResultError):
        portable.load_projection_bytes(b'{"schema":"x","schema":"y"}')
    with pytest.raises(portable.PortableResultError):
        portable.load_projection_bytes(b'{"value":NaN}')
    with pytest.raises(portable.PortableResultError):
        portable.load_projection_bytes(encoded, max_bytes=8)


def test_private_loader_is_bounded_strict_and_keeps_exact_bytes_for_caller_hash():
    raw = b'{"status":"success","prompt":"still private"}'
    assert portable.load_private_envelope_bytes(raw)["prompt"] == "still private"
    with pytest.raises(portable.PortableResultError):
        portable.load_private_envelope_bytes(b'{"status":1,"status":2}')
    with pytest.raises(portable.PortableResultError):
        portable.load_private_envelope_bytes(b'[1,2,3]')


def test_stable_regular_file_reader_rejects_links(tmp_path):
    source = tmp_path / "source.json"
    source.write_bytes(b'{"status":"success"}')
    assert portable.read_regular_file_bytes(source) == source.read_bytes()
    link = tmp_path / "link.json"
    try:
        link.symlink_to(source)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")
    with pytest.raises(portable.PortableResultError):
        portable.read_regular_file_bytes(link)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO swap regression")
def test_stable_regular_file_reader_cannot_block_on_fifo_swap(tmp_path, monkeypatch):
    regular = tmp_path / "regular.json"
    regular.write_text("{}", encoding="utf-8")
    regular_stat = regular.lstat()
    fifo = tmp_path / "record.json"
    os.mkfifo(fifo)
    real_lstat = portable.Path.lstat

    def stale_regular_lstat(path):
        return regular_stat if path == fifo else real_lstat(path)

    monkeypatch.setattr(portable.Path, "lstat", stale_regular_lstat)
    started = time.monotonic()
    with pytest.raises(portable.PortableResultError):
        portable.read_regular_file_bytes(fifo)
    assert time.monotonic() - started < 1.0


def test_reference_consumer_is_bounded_and_grants_no_authority(tmp_path):
    projected = _project(_envelope(), tmp_path)
    consumed = portable.consume_reference(projected)
    assert consumed == {
        "schema": "summon.portable-consumer/experimental-1",
        "status": "accepted",
        "authority_granted": False,
        "source_surface": "dispatch",
        "source_receipt_sha256": projected["source"]["receipt_sha256"],
        "projection_sha256": projected["integrity"]["projection_sha256"],
        "execution_status": "success",
        "result_usable": None,
        "named_model_verified": True,
        "artifact_count": 0,
    }


def test_exclusive_atomic_writer_publishes_only_a_valid_new_file(tmp_path):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"
    portable.write_projection_file(target, projected)
    assert portable.load_projection_bytes(target.read_bytes()) == projected
    with pytest.raises(portable.PortableResultError):
        portable.write_projection_file(target, projected)


def test_atomic_writer_success_is_not_confused_by_outer_exception(tmp_path):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"
    try:
        raise RuntimeError("outer")
    except RuntimeError:
        portable.write_projection_file(target, projected)
    assert portable.load_projection_bytes(target.read_bytes()) == projected


@pytest.mark.parametrize("code", [errno.EINVAL, errno.EPERM, errno.EXDEV])
def test_atomic_writer_link_failure_cleans_owned_temporary(
        tmp_path, monkeypatch, code):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"

    def unsupported(*_args, **_kwargs):
        raise OSError(code, "hard links unavailable")

    monkeypatch.setattr(portable.os, "link", unsupported)
    with pytest.raises(portable.PortableResultError, match="unsupported"):
        portable.write_projection_file(target, projected)
    assert not target.exists()
    assert not list(tmp_path.glob(".summon-portable-*.tmp"))


def test_atomic_writer_does_not_delete_mutated_temp_after_link_error(
        tmp_path, monkeypatch):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"
    real_unlink = portable.os.unlink
    substituted_source = None
    cleanup_unlinks = []

    def mutate_then_fail(source, _destination, *args, **kwargs):
        nonlocal substituted_source
        substituted_source = Path(source)
        Path(source).write_bytes(b"substituted")
        raise OSError("ambiguous link failure")

    def tracked_unlink(path, *args, **kwargs):
        cleanup_unlinks.append(Path(path))
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(portable.os, "link", mutate_then_fail)
    monkeypatch.setattr(portable.os, "unlink", tracked_unlink)
    with pytest.raises(portable.PortableResultError):
        portable.write_projection_file(target, projected)
    assert not target.exists()
    assert substituted_source is not None
    assert substituted_source not in cleanup_unlinks


def test_atomic_writer_removes_authenticated_target_when_link_lands_then_errors(
        tmp_path, monkeypatch):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"
    real_link = portable.os.link

    def link_then_error(source, destination, *args, **kwargs):
        real_link(source, destination, *args, **kwargs)
        raise OSError("reported after commit")

    monkeypatch.setattr(portable.os, "link", link_then_error)
    with pytest.raises(portable.PortableResultError):
        portable.write_projection_file(target, projected)
    assert not target.exists()
    assert not list(tmp_path.glob(".summon-portable-*.tmp"))


def test_atomic_writer_detects_parent_identity_change(tmp_path, monkeypatch):
    projected = _project(_envelope(), tmp_path)
    target = tmp_path / "receipt.json"
    real_identity = portable._root_identity
    calls = 0

    def changed_after_link(path):
        nonlocal calls
        calls += 1
        identity = real_identity(path)
        return identity if calls < 3 else (identity[0], identity[1] + 1)

    monkeypatch.setattr(portable, "_root_identity", changed_after_link)
    with pytest.raises(portable.PortableResultError):
        portable.write_projection_file(target, projected)
    assert not target.exists()


def test_cli_projects_validates_and_reference_consumes_without_provider(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    private = tmp_path / "private.json"
    private_bytes = json.dumps(_envelope(), sort_keys=True).encode("utf-8")
    private.write_bytes(private_bytes)
    public = tmp_path / "public.json"
    runner = Path(__file__).with_name("run_subagent.py")

    project = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "dispatch",
        "--from", str(private), "--repo-root", str(root), "--out", str(public),
        "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert project.returncode == 0, project.stderr or project.stdout
    projected = json.loads(project.stdout)
    assert projected["source"]["receipt_sha256"] == hashlib.sha256(private_bytes).hexdigest()
    assert public.exists()

    validate = subprocess.run([
        sys.executable, str(runner), "result", "validate", str(public), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert validate.returncode == 0, validate.stderr or validate.stdout
    assert json.loads(validate.stdout)["authority_granted"] is False

    consume = subprocess.run([
        sys.executable, str(runner), "result", "consume", str(public),
        "--adapter", "reference", "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert consume.returncode == 0, consume.stderr or consume.stdout
    receipt = json.loads(consume.stdout)
    assert receipt["status"] == "accepted" and receipt["authority_granted"] is False


def test_cli_refuses_unimplemented_surface_before_provider_contact(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    private = tmp_path / "private.json"
    private.write_text("{}", encoding="utf-8")
    runner = Path(__file__).with_name("run_subagent.py")
    result = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "chat",
        "--from", str(private), "--repo-root", str(root), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 1
    error = json.loads(result.stdout)
    assert error["execution_status"] == "not_run"
    assert error["provider_contacted"] is False
    assert error["authority_granted"] is False
    assert error["error_kind"] == "portable_chat_source_unavailable"


def test_cli_projects_only_an_authenticated_terminal_job(tmp_path):
    jobs = tmp_path / "jobs"
    _jobs.ensure_jobs_dir(str(jobs))
    job_id = "a" * 32
    nonce = "n" * 32
    prompt_sha = "c" * 64
    scripts_sha = "a" * 64
    frozen_summon = {
        "scripts_sha256": scripts_sha,
        "background_bundle": {
            "kind": "immutable_per_job_snapshot",
            "scripts_sha256": scripts_sha,
            "prompt_sha256": prompt_sha,
        },
    }
    _jobs.write_prepared(
        str(jobs), job_id, nonce=nonce, agent="reviewer",
        prompt_sha256=prompt_sha, cwd=str(tmp_path), flags={}, summon=frozen_summon)
    result_path = Path(_jobs.result_path(str(jobs), job_id))
    result = _envelope(
        job_nonce=nonce, prompt_sha256=prompt_sha,
        summon={"version": "3.2.1", "scripts_sha256": scripts_sha})
    _jobs._atomic_write_json(str(result_path), result)
    record_path = Path(_jobs.record_path(str(jobs), job_id))
    exact_result = result_path.read_bytes()
    exact_record = record_path.read_bytes()
    runner = Path(__file__).with_name("run_subagent.py")

    command = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "job",
        "--from", str(result_path), "--repo-root", str(tmp_path), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert command.returncode == 0, command.stderr or command.stdout
    projected = json.loads(command.stdout)
    assert projected["source"]["surface"] == "job"
    assert projected["source"]["receipt_sha256"] == hashlib.sha256(exact_result).hexdigest()
    assert projected["source"]["binding_sha256"] == hashlib.sha256(exact_record).hexdigest()

    # Removing the private record makes the same result explicitly untrusted.
    record_path.unlink()
    refused = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "job",
        "--from", str(result_path), "--repo-root", str(tmp_path), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert refused.returncode == 1
    refusal = json.loads(refused.stdout)
    assert refusal["provider_contacted"] is False
    assert refusal["execution_status"] == "not_run"


def test_cli_refuses_legacy_nonce_only_job_for_portable_provenance(tmp_path):
    jobs = tmp_path / "jobs"
    _jobs.ensure_jobs_dir(str(jobs))
    job_id = "b" * 32
    nonce = "legacy"
    _jobs.write_prepared(
        str(jobs), job_id, nonce=nonce, agent="reviewer",
        prompt_sha256=None, cwd=str(tmp_path), flags={}, summon={})
    result_path = Path(_jobs.result_path(str(jobs), job_id))
    _jobs._atomic_write_json(str(result_path), _envelope(job_nonce=nonce))
    # The legacy status API remains backward compatible and readable.
    assert _jobs.job_status(str(jobs), job_id)["trusted"] is True
    runner = Path(__file__).with_name("run_subagent.py")
    refused = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "job",
        "--from", str(result_path), "--repo-root", str(tmp_path), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert refused.returncode == 1
    error = json.loads(refused.stdout)
    assert error["provider_contacted"] is False
    assert "immutable" in error["error"]


def test_cli_refuses_authenticated_but_nonterminal_job(tmp_path):
    jobs = tmp_path / "jobs"
    _jobs.ensure_jobs_dir(str(jobs))
    job_id = "c" * 32
    nonce = "r" * 32
    prompt_sha = "d" * 64
    scripts_sha = "e" * 64
    frozen_summon = {
        "scripts_sha256": scripts_sha,
        "background_bundle": {
            "kind": "immutable_per_job_snapshot",
            "scripts_sha256": scripts_sha,
            "prompt_sha256": prompt_sha,
        },
    }
    _jobs.write_prepared(
        str(jobs), job_id, nonce=nonce, agent="reviewer",
        prompt_sha256=prompt_sha, cwd=str(tmp_path), flags={}, summon=frozen_summon)
    result_path = Path(_jobs.result_path(str(jobs), job_id))
    running = _envelope(
        status="running", execution_status="partial", job_nonce=nonce,
        prompt_sha256=prompt_sha,
        summon={"version": "3.2.1", "scripts_sha256": scripts_sha})
    _jobs._atomic_write_json(str(result_path), running)
    status = _jobs.job_status(str(jobs), job_id)
    assert status["trusted"] is True
    assert status["state"] == "running"

    runner = Path(__file__).with_name("run_subagent.py")
    refused = subprocess.run([
        sys.executable, str(runner), "result", "project", "--kind", "job",
        "--from", str(result_path), "--repo-root", str(tmp_path), "--json",
    ], capture_output=True, text=True, timeout=30, check=False)
    assert refused.returncode == 1
    error = json.loads(refused.stdout)
    assert error["execution_status"] == "not_run"
    assert error["provider_contacted"] is False
    assert "terminal" in error["error"]

"""Integrated provider-inert Phase 1 operator and compatibility goldens."""

from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import _deliberation_context as durable_context
import _executor
import _jobs


HERE = Path(__file__).resolve().parent
RUNNER = HERE / "run_subagent.py"
PHASE1_EXAMPLES = HERE.parent / "examples" / "phase1"


def _run(args: list[str], *, env: dict[str, str], expected: int = 0) -> dict:
    completed = subprocess.run(
        [sys.executable, str(RUNNER), *args], capture_output=True, text=True,
        encoding="utf-8", timeout=60, env=env)
    assert completed.returncode == expected, completed.stdout + completed.stderr
    assert completed.stdout.strip(), completed.stderr
    return json.loads(completed.stdout)


def _seal_portable(value: dict) -> dict:
    unsigned = json.loads(json.dumps(value))
    unsigned["integrity"].pop("projection_sha256", None)
    value["integrity"]["projection_sha256"] = hashlib.sha256(
        json.dumps(unsigned, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return value


def _standalone_consume(path: Path, *, expected: int = 0) -> subprocess.CompletedProcess:
    consumer = PHASE1_EXAMPLES / "consume_portable_result.py"
    completed = subprocess.run(
        [sys.executable, str(consumer), str(path)], capture_output=True,
        text=True, encoding="utf-8", timeout=30)
    assert completed.returncode == expected, completed.stdout + completed.stderr
    return completed


def _fake_claude(bin_dir: Path) -> None:
    bin_dir.mkdir()
    if os.name == "nt":
        (bin_dir / "claude.cmd").write_text(
            "@echo off\n"
            "if \"%~1\"==\"--version\" (echo 2.1.114 ^(Claude Code^) & exit /b 0)\n"
            "exit /b 97\n",
            encoding="ascii", newline="\n")
    else:
        command = bin_dir / "claude"
        command.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo '2.1.114 (Claude Code)'; exit 0; fi\n"
            "exit 97\n",
            encoding="ascii", newline="\n")
        command.chmod(0o700)


def _private_dispatch_envelope() -> dict:
    return {
        "status": "success", "execution_status": "success",
        "attempts": 1, "attempt_status": "completed",
        "provider_contacted": True, "report_ok": True,
        "result_usable": True, "verdict": "pass", "error_kind": None,
        "raw_backend_exit_code": 0, "normalized_exit_code": 0,
        "agent": "reviewer", "provider": "anthropic", "cli": "claude",
        "transport": "subprocess",
        "model": {
            "requested": "frontier-alpha", "targeted": "frontier-alpha",
            "served": "frontier-alpha",
        },
        "served_model_evidence": "reported",
        "summon": {"version": "3.2.1", "scripts_sha256": "a" * 64},
        "artifacts": {
            "workspace_mutation": "none", "stability": "stable", "files": [],
        },
        # These private fields must never cross the portable boundary.
        "prompt": "private operator prompt", "result": "private model response",
        "session_id": "private-session",
    }


def _accepted_stale_projection() -> dict:
    now = 2_000_000
    packet = {
        "schema": "summon.deliberation-context/v1",
        "source": {
            "kind": "git_commit", "revision": "a" * 40,
            "source_digest": "1" * 64, "captured_at_unix_ms": now - 5_000,
        },
        "freshness_policy": {
            "fresh_max_age_ms": 1_000, "hard_max_age_ms": 20_000,
            "hard_max_revision_delta": 3,
        },
        "entries": [{
            "id": "constraint-1", "kind": "constraint",
            "body": "Keep the public API stable.",
            "provenance": {"kind": "authored", "source_sha256": "2" * 64},
        }],
    }
    parsed = durable_context.parse_context_packet(packet)
    observation = {
        "schema": "summon.context-source-observation/v1",
        "kind": "git_commit", "captured_revision": "a" * 40,
        "current_revision": "a" * 40, "current_source_digest": "1" * 64,
        "relation": "same", "revision_delta": 0,
        "verification_method": "git-readback", "observed_at_unix_ms": now,
    }
    acceptance = {
        "schema": "summon.accept-stale-intent/v2",
        "packet_sha256": parsed.packet_sha256,
        "source_revision_sha256": parsed.source_revision_sha256,
        "runs_root_sha256": "3" * 64, "run_id": "operator-golden",
        "decision_id": "phase1-closure",
        "actor": {"kind": "human", "id": "operator"},
        "reason": "Reviewed the bounded source age.",
        "scope": {"entry_ids": ["constraint-1"], "use": "deliberation_prompt"},
        "expires_at_unix_ms": now + 5_000, "max_age_ms": 10_000,
        "max_revision_delta": 2,
    }
    bound = durable_context.bind_context(
        packet, observation, run_id="operator-golden",
        decision_id="phase1-closure", unix_now_ms=now,
        runs_root_sha256="3" * 64, accept_stale=acceptance)
    return durable_context.public_projection(bound)


def test_phase1_operator_workflow_is_coherent_provider_inert_and_private(
        tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("SUMMON_"):
            monkeypatch.delenv(key, raising=False)
    bin_dir = tmp_path / "bin"
    _fake_claude(bin_dir)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text(
        "---\nrun-agent: claude\nprovider: anthropic\n"
        "permission: read-only\nmodel: frontier-alpha\n"
        "model-policy: exact\nlifecycle: active\n---\n"
        "# Reviewer\nReview the bounded task.\n",
        encoding="utf-8", newline="\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SUMMON_")}
    env.update({"HOME": str(profile), "USERPROFILE": str(profile)})
    path_parts = [str(bin_dir), str(Path(sys.executable).parent)]
    if os.name == "nt":
        path_parts.append(str(Path(os.environ["COMSPEC"]).parent))
    else:
        path_parts.extend(["/usr/bin", "/bin"])
    env["PATH"] = os.pathsep.join(dict.fromkeys(path_parts))
    env["SUMMON_FLEET_APPROVAL_STORE"] = str(tmp_path / "private" / "store.json")
    env["SUMMON_FLEET_APPROVAL_KEY"] = str(tmp_path / "private" / "store.key")

    fleet = tmp_path / "fleet.json"
    proposed = _run([
        "fleet", "propose", "review", "--seats", "reviewer",
        "--permission-ceiling", "read-only", "--data-boundary", "local_sanitized",
        "--allow-subscription", "--max-provider-contacts", "1",
        "--max-billable-attempts", "0", "--max-parallel", "1",
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--out", str(fleet), "--json",
    ], env=env)
    assert proposed["provider_contacted"] is False
    assert fleet.is_file()
    for action in ("validate", "inspect"):
        args = ["fleet", action, str(fleet)]
        if action == "validate":
            args += ["--cwd", str(tmp_path), "--agents-dir", str(agents)]
        assert _run([*args, "--json"], env=env)["provider_contacted"] is False
    explained = _run([
        "fleet", "explain", str(fleet), "review", "--cwd", str(tmp_path),
        "--agents-dir", str(agents), "--json",
    ], env=env)
    assert explained["selection"]["status"] == "not_authorized"
    assert explained["selection"]["seat"] is None

    approved = _run([
        "fleet", "approval", "approve", str(fleet), "review",
        "--expires-in", "1h", "--expect-generation", "0",
        "--cwd", str(tmp_path), "--agents-dir", str(agents), "--json",
    ], env=env)
    assert approved["provider_contacted"] is False
    assert approved["dispatch_available"] is False
    approval_id = approved["approval"]["approval_id"]
    prompt = tmp_path / "task.txt"
    prompt.write_text("Review the provider-inert integration fixture.", encoding="utf-8")
    activation = _run([
        "dispatch", "--lane", "review", "--fleet-file", str(fleet),
        "--fleet-approval-id", approval_id, "--fleet-data-proof",
        "operator_attested", "--prompt-file", str(prompt),
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--dry-run", "--json",
    ], env=env)
    assert activation["provider_contacted"] is False
    assert activation["fleet_dispatch"]["authorization"] == "approved_lane"
    assert activation["fleet_dispatch"]["attempt_policy"]["max_physical_attempts"] == 1

    usage_example = tmp_path / "usage-example.json"
    assert _run([
        "usage", "example", "--out", str(usage_example), "--json",
    ], env=env)["provider_contacted"] is False
    usage_cache = tmp_path / "usage-cache.json"
    imported = _run([
        "usage", "import", "--from", str(usage_example),
        "--cache", str(usage_cache), "--json",
    ], env=env)
    assert imported["provider_contacted"] is False and imported["imported"] > 0
    usage_status = _run([
        "usage", "status", "--cache", str(usage_cache), "--json",
    ], env=env)
    assert usage_status["provider_contacted"] is False
    assert usage_status["selection_advice"] == {
        "routing_changed": False,
        "provider_contacted": False,
        "reason": "advisory_only_exact_requests_preserved",
        "fresh_observations": 0,
        "stale_observations": len(usage_status["observations"]),
    }
    assert all(item["freshness"] == "stale"
               for item in usage_status["observations"])
    usage_export = tmp_path / "usage-export.json"
    _run([
        "usage", "export", "--cache", str(usage_cache),
        "--out", str(usage_export), "--json",
    ], env=env)
    exported_text = usage_export.read_text(encoding="utf-8")
    assert str(tmp_path) not in exported_text
    refresh = _run([
        "usage", "refresh", "--providers", "codex",
        "--allow-account-usage-read", "--dry-run", "--json",
    ], env=env)
    assert refresh["provider_contacted"] is False and refresh["attempts"] == 0

    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps({
        "schema": "summon.context-input/v1",
        "blocks": [
            {"id": "note-a", "plane": "payload", "kind": "immutable_artifact",
             "body": "same", "immutable": True, "stable": False},
            {"id": "note-b", "plane": "payload", "kind": "immutable_artifact",
             "body": "same", "immutable": True, "stable": False},
        ],
    }), encoding="utf-8")
    safe = _run([
        "dispatch", "--agent", "reviewer", "--prompt-file", str(prompt),
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--context-input-file", str(context_file), "--context-profile", "safe",
        "--dry-run", "--json",
    ], env=env)
    assert safe["provider_contacted"] is False
    assert safe["context_compilation"]["profile"] == "safe"
    assert safe["context_compilation"]["actions"]["deduplicated"] == 1
    off = _run([
        "dispatch", "--agent", "reviewer", "--prompt-file", str(prompt),
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--context-input-file", str(context_file), "--context-profile", "off",
        "--dry-run", "--json",
    ], env=env)
    assert off["context_compilation"]["profile"] == "off"
    assert off["context_compilation"]["compiled_sha256"] == hashlib.sha256(
        context_file.read_bytes()).hexdigest()

    stale = _accepted_stale_projection()
    assert stale["state"] == "accepted_stale"
    assert stale["routing_authority"] is False

    report = "STATUS: DONE\nSUMMARY: fake\nFOLLOW-UP: none\nHANDOFF: none"
    terminal = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": report, "session_id": "fixture-session",
    })
    process = subprocess.Popen(
        [sys.executable, "-c", f"print({terminal!r})"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    live = _executor._drive_process(
        process, "claude", 5_000, parse_stream=True,
        attempt_id="f" * 32, first_event_ms=1_000, idle_ms=1_000,
        finalization_ms=1_000)
    assert live["status"] == "success"
    assert live["liveness"]["phase"] == "terminal"
    assert live["liveness"]["counts"]["trusted"] >= 1

    # Exercise the documented public control surfaces against one authenticated,
    # provider-inert live-job record. The current pytest process is only a
    # liveness witness; cancellation is queued for the Summon child to consume
    # and never signals this process.
    job_dir = tmp_path / "jobs"
    job_id = "d" * 32
    _jobs.write_prepared(
        str(job_dir), job_id, nonce="operator-workflow", agent="reviewer",
        prompt_sha256="e" * 64, cwd=str(tmp_path), flags={"adaptive_timeout": True},
        summon={"version": "3.2.1", "scripts_sha256": "f" * 64})
    _jobs.update_spawned(str(job_dir), job_id, os.getpid())
    status = _run([
        "jobs", "status", job_id, "--job-dir", str(job_dir), "--json",
    ], env=env)
    assert status["state"] == "running" and status["liveness"] == "alive"
    extended = _run([
        "jobs", "extend", job_id, "--duration", "1m",
        "--job-dir", str(job_dir), "--json",
    ], env=env)
    steered = _run([
        "jobs", "steer", job_id, "--message", "Inspect the fixture first.",
        "--job-dir", str(job_dir), "--json",
    ], env=env)
    cancelled = _run([
        "jobs", "cancel", job_id, "--job-dir", str(job_dir), "--json",
    ], env=env)
    assert extended["action"] == "extend" and extended["provider_contacted"] is False
    assert steered["action"] == "steer" and steered["provider_contacted"] is False
    assert steered["steering_mode"] == "queued_for_resume"
    assert cancelled["action"] == "cancel" and cancelled["provider_contacted"] is False
    controlled = _run([
        "jobs", "status", job_id, "--job-dir", str(job_dir), "--json",
    ], env=env)
    assert controlled["control"]["counts"] == {
        "extend": 1, "cancel": 1, "steer": 1}

    private = tmp_path / "private-envelope.json"
    private.write_text(json.dumps(_private_dispatch_envelope()), encoding="utf-8")
    portable = tmp_path / "portable.json"
    projected = _run([
        "result", "project", "--kind", "dispatch", "--from", str(private),
        "--repo-root", str(tmp_path), "--out", str(portable), "--json",
    ], env=env)
    assert projected["source"]["surface"] == "dispatch"
    validated = _run([
        "result", "validate", str(portable), "--json",
    ], env=env)
    consumed = _run([
        "result", "consume", str(portable), "--adapter", "reference", "--json",
    ], env=env)
    assert validated["authority_granted"] is False
    assert consumed["status"] == "accepted" and consumed["authority_granted"] is False
    external = _standalone_consume(portable)
    assert json.loads(external.stdout)["projection_sha256"] == (
        projected["integrity"]["projection_sha256"])
    public_text = portable.read_text(encoding="utf-8")
    for private_value in (
        str(tmp_path), "private operator prompt", "private model response",
        "private-session",
    ):
        assert private_value not in public_text


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_wrapper_emits_doctor_json_and_preserves_multiline_prompt(tmp_path):
    bin_dir = tmp_path / "bin"
    _fake_claude(bin_dir)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text(
        "---\nrun-agent: claude\npermission: read-only\n"
        "model: frontier-alpha\n---\n# Reviewer\nReview.\n",
        encoding="utf-8", newline="\n")
    profile = tmp_path / "profile"
    profile.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("SUMMON_")}
    env.update({"HOME": str(profile), "USERPROFILE": str(profile)})
    # Keep the launcher test deterministic and fast. Doctor probes every CLI on
    # PATH, and a developer machine may contain several cold-starting clients.
    # The wrapper itself needs the Windows shell and Python launcher only.
    py_launcher = shutil.which("py")
    assert py_launcher is not None
    env["PATH"] = os.pathsep.join((
        str(bin_dir), str(Path(py_launcher).parent),
        str(Path(os.environ["COMSPEC"]).parent),
    ))
    wrapper = HERE / "summon.cmd"
    base = ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper)]

    doctor = subprocess.run(
        [*base, "doctor", "--json"], capture_output=True, text=True,
        encoding="utf-8", timeout=90, env=env)
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert json.loads(doctor.stdout)["platform"].startswith("Windows")

    prompt = "first paragraph\n\nsecond paragraph & symbols"
    unsafe = subprocess.run([
        *base, "dispatch", "--agent", "reviewer", "--prompt", prompt,
        "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--dry-run", "--json",
    ], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    assert unsafe.returncode == 1
    refusal = json.loads(unsafe.stdout)
    assert refusal["error_kind"] == "prompt_transport_unsafe"
    assert refusal["provider_contacted"] is False
    assert refusal["attempts"] == 0

    prompt_file = tmp_path / "multiline-prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8", newline="\n")
    dry = subprocess.run([
        *base, "dispatch", "--agent", "reviewer", "--prompt-file",
        str(prompt_file), "--cwd", str(tmp_path), "--agents-dir", str(agents),
        "--dry-run", "--json",
    ], capture_output=True, text=True, encoding="utf-8", timeout=60, env=env)
    assert dry.returncode == 0, dry.stdout + dry.stderr
    envelope = json.loads(dry.stdout)
    assert envelope["args"][-1] == prompt
    assert envelope["provider_contacted"] is False


def test_standalone_portable_consumer_matches_golden_and_rejects_forgery(tmp_path):
    sample = PHASE1_EXAMPLES / "portable-result.sample.json"
    expected = json.loads((PHASE1_EXAMPLES / "portable-consumer.expected.json").read_text(
        encoding="utf-8"))
    completed = _standalone_consume(sample)
    assert json.loads(completed.stdout) == expected

    forged = json.loads(sample.read_text(encoding="utf-8"))
    forged["model"]["served"] = None
    forged["model"]["named_model_verified"] = True
    _seal_portable(forged)
    forged_path = tmp_path / "forged.json"
    forged_path.write_text(json.dumps(forged), encoding="utf-8")
    rejected = _standalone_consume(forged_path, expected=1)
    assert rejected.stdout == ""
    assert rejected.stderr.strip() == "portable result rejected"

    deeply_nested = tmp_path / "deeply-nested.json"
    deeply_nested.write_text("[" * 1200 + "0" + "]" * 1200, encoding="utf-8")
    rejected_nested = _standalone_consume(deeply_nested, expected=1)
    assert rejected_nested.stdout == ""
    assert rejected_nested.stderr.strip() == "portable result rejected"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (256 * 1024))
    rejected_oversized = _standalone_consume(oversized, expected=1)
    assert rejected_oversized.stdout == ""
    assert rejected_oversized.stderr.strip() == "portable result rejected"


def test_standalone_consumer_has_no_summon_import_or_path_injection():
    source = (PHASE1_EXAMPLES / "consume_portable_result.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
        elif (isinstance(node, ast.Attribute) and node.attr == "path"
              and isinstance(node.value, ast.Name) and node.value.id == "sys"):
            raise AssertionError("standalone consumer may not mutate or inspect sys.path")
    assert imported <= {
        "__future__", "hashlib", "json", "os", "pathlib", "re", "stat", "sys",
    }


@pytest.mark.parametrize(("mutator", "label"), [
    (lambda value: value["outcome"].__setitem__("execution_status", "invented"),
     "invalid execution enum"),
    (lambda value: value["artifacts"]["files"].append({
        "sha256": "b" * 64, "bytes": -1}), "invalid artifact bytes"),
    (lambda value: value["attestation"].__setitem__("transport", "magic"),
     "invalid transport"),
])
def test_standalone_consumer_rejects_resealed_contract_forgeries(
        tmp_path, mutator, label):
    value = json.loads((PHASE1_EXAMPLES / "portable-result.sample.json").read_text(
        encoding="utf-8"))
    mutator(value)
    _seal_portable(value)
    path = tmp_path / f"forged-{label.replace(' ', '-')}.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    rejected = _standalone_consume(path, expected=1)
    assert rejected.stderr.strip() == "portable result rejected"


def test_standalone_consumer_accepts_nullable_unknown_no_contact_and_job_shapes(
        tmp_path):
    sample = json.loads((PHASE1_EXAMPLES / "portable-result.sample.json").read_text(
        encoding="utf-8"))

    unknown = json.loads(json.dumps(sample))
    unknown["outcome"].update({
        "status": "unknown", "execution_status": "unknown", "attempts": None,
        "attempt_status": "unknown", "report_ok": None, "result_usable": None,
        "verdict": "unknown", "raw_backend_exit_code": None,
        "normalized_exit_code": None,
    })
    unknown["contact"]["provider_contacted"] = None
    unknown["model"].update({
        "served": None, "served_model_evidence": "inferred",
        "model_match": None, "named_model_verified": False,
    })
    unknown["artifacts"].update({
        "workspace_mutation": "unknown", "artifact_stability": "unknown",
        "bytes": None, "files": [],
    })

    no_contact = json.loads(json.dumps(sample))
    no_contact["outcome"].update({
        "status": "blocked", "execution_status": "not_run", "attempts": 0,
        "attempt_status": "not_run", "report_ok": False,
        "result_usable": False, "verdict": None,
        "raw_backend_exit_code": None, "normalized_exit_code": 1,
    })
    no_contact["contact"].update({
        "provider_contacted": False, "retry_or_fallback": "none"})
    no_contact["model"].update({
        "served": None, "served_model_evidence": "absent",
        "model_match": None, "named_model_verified": False,
    })
    no_contact["artifacts"].update({
        "workspace_mutation": "unknown", "artifact_stability": "unknown",
        "bytes": None, "files": [],
    })

    job = json.loads(json.dumps(sample))
    job["source"].update({"surface": "job", "binding_sha256": "c" * 64})

    for label, value in (("unknown", unknown), ("no-contact", no_contact),
                         ("job", job)):
        path = tmp_path / f"{label}.json"
        path.write_text(json.dumps(_seal_portable(value)), encoding="utf-8")
        accepted = _standalone_consume(path)
        assert json.loads(accepted.stdout)["status"] == "accepted"


@pytest.mark.parametrize("digest_field", (
    "receipt", "job-binding", "artifact", "scripts",
))
def test_standalone_consumer_matches_canonical_case_insensitive_sha_contract(
        tmp_path, digest_field):
    value = json.loads((PHASE1_EXAMPLES / "portable-result.sample.json").read_text(
        encoding="utf-8"))
    if digest_field == "receipt":
        value["source"]["receipt_sha256"] = value["source"]["receipt_sha256"].upper()
    elif digest_field == "job-binding":
        value["source"].update({"surface": "job", "binding_sha256": "C" * 64})
    elif digest_field == "artifact":
        value["artifacts"]["files"][0]["sha256"] = "B" * 64
    else:
        value["integrity"]["scripts_sha256"] = "A" * 64
    path = tmp_path / f"uppercase-{digest_field}.json"
    path.write_text(json.dumps(_seal_portable(value)), encoding="utf-8")
    accepted = _standalone_consume(path)
    assert json.loads(accepted.stdout)["status"] == "accepted"

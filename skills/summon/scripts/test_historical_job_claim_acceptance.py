"""Exact historical reader-shape acceptance, using synthetic authenticated records.

These are projections of current fixture output into explicitly supported reader
field sets, not recovered historical producer bytes or live CLI qualification.
"""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Explicit historical shapes: do not derive expected keys from runtime constants.
LEDGER_CLAIM_KEYS = {
    "generation", "request_id", "request_sha256", "claim_id", "successor_job_id",
    "prompt_contract", "prompt_sha256", "steering_generations", "steering_sha256",
    "control_generation", "control_sha256", "permission", "gate_with", "allow_credit",
    "allow_payg", "timeout_ms", "gate_timeout_ms", "max_runtime_ms", "parent_phase",
    "gate_phase", "provider_phase", "provider_contacted", "successor_record_sha256",
    "bundle_sha256", "claim_sha256", "pid", "gate_pid", "provider_pid",
    "gate_decision_sha256", "terminal_sha256", "terminalization_error_kind",
    "created_at", "updated_at",
}
CHILD_KEYS = {
    "schema", "source_job_id", "source_attempt_id", "source_result_binding_sha256",
    "source_sha256", "request_id", "request_sha256", "claim_id", "successor_job_id",
    "successor_attempt_id", "successor_record_sha256", "bundle_sha256", "prompt_sha256",
    "steering", "authority", "gate", "spend", "timeout", "created_at", "auth",
}
PUBLIC_KEYS = {
    "schema", "request_id", "claim_id", "successor_job_id", "parent_phase",
    "gate_phase", "provider_phase", "provider_contacted", "terminal_result_present",
    "terminalization_error_kind", "consumed_steering_generations", "recovery_required",
}


@pytest.fixture(autouse=True)
def inert_boundaries(tmp_path, monkeypatch):
    system = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in system.items():
        monkeypatch.setenv(key, value)
    home = tmp_path / "synthetic-home"
    home.mkdir()
    for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
        monkeypatch.setenv(key, str(home))
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("SUMMON_TELEMETRY", "0")
    hits = []
    def forbidden(*args, **kwargs):
        hits.append("forbidden_boundary")
        raise AssertionError("process/dispatch/credential/network boundary was reached")
    # Process and network fencing precedes imports of runtime/helper modules.
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    import _windows_credentials, _nous_credentials
    for module, names in (
        (_windows_credentials, ("read_credential", "_read_hermes_env_key", "resolve_openrouter_api_key")),
        (_nous_credentials, ("_candidate_paths", "_parse_key", "resolve_nous_api_key")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    import _auth, _executor, _background, run_subagent
    for module, name in (
        (_auth, "run_auth_action"), (_executor, "execute_agent"),
        (_background, "spawn_background"), (run_subagent, "_dispatch_with_retries"),
        (run_subagent, "execute_agent"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    yield
    assert not hits, "a caught exception concealed a forbidden boundary"


def _files(root):
    return {p.relative_to(root): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


def _packet(tmp_path, *, historical=True):
    import _job_resume as resume
    import _jobs
    from test_job_resume import _prepare
    root, source_id, reservation, prepared = _prepare(tmp_path)
    child_path = Path(prepared["claim_file"])
    ledger_path = Path(resume.ledger_path(root, source_id))
    child = json.loads(child_path.read_text(encoding="utf-8"))
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if historical:
        child = {k: v for k, v in child.items() if k in CHILD_KEYS}
        ledger["claims"][0] = {k: v for k, v in ledger["claims"][0].items() if k in LEDGER_CLAIM_KEYS}
        assert set(child) == CHILD_KEYS
        assert set(ledger["claims"][0]) == LEDGER_CLAIM_KEYS
    packet = dict(root=root, source_id=source_id, reservation=reservation,
                  child_path=child_path, ledger_path=ledger_path, child=child, ledger=ledger,
                  job_file=_jobs.result_path(root, reservation.successor_job_id))
    _seal(packet)
    return packet


def _seal(packet):
    import _job_resume as resume
    _, source_nonce = resume._source_record(packet["root"], packet["source_id"])
    _, successor_nonce = resume._source_record(packet["root"], packet["reservation"].successor_job_id)
    child_body = {k: v for k, v in packet["child"].items() if k != "auth"}
    child = resume._write_authenticated(
        str(packet["child_path"]), resume._claim_key(source_nonce, successor_nonce),
        resume._claim_domain(packet["source_id"], packet["reservation"].successor_job_id), child_body)
    packet["child"] = child
    packet["ledger"]["claims"][0]["claim_sha256"] = resume._digest(child)
    body = {k: v for k, v in packet["ledger"].items() if k != "auth"}
    packet["ledger"] = resume._write_authenticated(
        str(packet["ledger_path"]), source_nonce, resume._ledger_domain(packet["source_id"]), body)


@pytest.mark.parametrize("historical", [True, False], ids=["historical", "current"])
def test_exact_claim_readers_preserve_bytes_and_public_projection(tmp_path, historical):
    import _job_resume as resume
    from test_job_resume import _launch_evidence
    packet = _packet(tmp_path, historical=historical)
    before = _files(packet["root"])
    claim = resume.get_claim(packet["reservation"])
    context = resume.load_child_context(packet["job_file"], str(packet["child_path"]))
    assert context.claim_id == claim["claim_id"] == packet["reservation"].claim_id
    assert context.successor_job_id == claim["successor_job_id"]
    assert context.claim_sha256 == claim["claim_sha256"]
    assert context.prompt.endswith("continue") and context.resume_handle == "session-private"
    public = resume.public_projection(claim)
    assert set(public) == PUBLIC_KEYS
    assert public["schema"] == "summon.job-resume/v1"
    assert public["parent_phase"] == "successor_prepared" and public["provider_phase"] == "pending"
    for secret in ("session-private", "continue", "review-profile", str(tmp_path)):
        assert secret not in json.dumps(public)
    if historical:
        assert context.launch_binding is None and context.launch_qualification is None
        import _executor
        with pytest.raises(_executor.ProviderLaunchRefusal) as refused:
            resume.provider_launch_control(context).before_provider_launch(_launch_evidence())
        assert refused.value.error_kind == "resume_launch_qualification_missing"
    else:
        # Validate evidence only. Never consume a launch claim or call a provider.
        assert resume._validate_launch_observation(context, _launch_evidence(), gate=False) == context.launch_binding
    assert _files(packet["root"]) == before


@pytest.mark.parametrize("change", [
    "missing_schema", "future_schema", "unknown_key", "missing_timeout",
    "partial_modern_fields", "bad_phase", "bool_generation", "integer_contact",
], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007', 'p002_case_008'])
def test_historical_ledger_refuses_authenticated_malformed_shape(tmp_path, change):
    import _job_resume as resume
    packet = _packet(tmp_path)
    ledger = packet["ledger"]
    claim = ledger["claims"][0]
    if change == "missing_schema": ledger.pop("schema")
    elif change == "future_schema": ledger["schema"] = "summon.job-resume-ledger/v2"
    elif change == "unknown_key": claim["future_authority"] = None
    elif change == "missing_timeout": claim.pop("timeout_ms")
    elif change == "partial_modern_fields": claim["launch_binding"] = None
    elif change == "bad_phase": claim["provider_phase"] = "future_phase"
    elif change == "bool_generation": claim["generation"] = True
    elif change == "integer_contact": claim["provider_contacted"] = 1
    _seal(packet)
    before = _files(packet["root"])
    # Auth is valid; each assertion targets field/shape validation, not HMAC failure.
    with pytest.raises(resume.ResumeError) as refused:
        resume.get_claim(packet["reservation"])
    assert refused.value.kind == "resume_claim_untrusted"
    assert _files(packet["root"]) == before


@pytest.mark.parametrize("change", [
    "missing_schema", "future_schema", "unknown_key", "missing_timeout",
    "partial_modern_fields", "wrong_source_attempt", "wrong_successor_attempt",
    "malformed_created_at",
], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007', 'p003_case_008'])
def test_historical_child_refuses_authenticated_malformed_shape(tmp_path, change):
    import _job_resume as resume
    packet = _packet(tmp_path)
    child = packet["child"]
    if change == "missing_schema": child.pop("schema")
    elif change == "future_schema": child["schema"] = "summon.job-resume-claim/v2"
    elif change == "unknown_key": child["future_authority"] = None
    elif change == "missing_timeout": child.pop("timeout")
    elif change == "partial_modern_fields": child["launch_binding"] = None
    elif change == "wrong_source_attempt": child["source_attempt_id"] = "f" * 32
    elif change == "wrong_successor_attempt": child["successor_attempt_id"] = "f" * 32
    elif change == "malformed_created_at": child["created_at"] = {"unsupported": "timestamp"}
    _seal(packet)
    before = _files(packet["root"])
    with pytest.raises(resume.ResumeError) as refused:
        resume.load_child_context(packet["job_file"], str(packet["child_path"]))
    assert refused.value.kind in {"resume_claim_untrusted", "resume_source_changed"}
    assert _files(packet["root"]) == before



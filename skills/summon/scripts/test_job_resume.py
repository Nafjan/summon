"""Provider-inert tests for authenticated background continuation claims."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import _job_continuation as continuation
import _job_resume as resume
import _jobs
import _background
import _cli
import _executor
import run_subagent as dispatcher
from _receipt import scripts_sha256
from test_job_continuation import _fixture, _publish_result


def _eligible_source(tmp_path, *, commands=(), allow_text_only=False,
                     require_tools=False):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    invocation = replace(
        invocation, profile="review-profile",
        profile_env={"CLAUDE_CONFIG_DIR": str(profile_dir.resolve())})
    args.allow_text_only = allow_text_only
    args.require_tools = require_tools
    result["profile"] = {
        "name": "review-profile", "cli": "claude",
        "path_sha256": hashlib.sha256(
            str(profile_dir.resolve()).encode("utf-8")).hexdigest()[:32],
        "registry_sha256": "a" * 32, "command_sha256": None,
    }
    continuation.write_private_source(job_file, result, invocation, args)
    if commands:
        from _job_control import queue_command
        for action, value in commands:
            if action == "steer":
                queue_command(root, job_id, action, message=value)
            else:
                queue_command(root, job_id, action, duration_ms=value)
    _publish_result(job_file, result)
    return root, job_id


def test_reservation_is_idempotent_and_private(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    first = resume.reserve_request(root, job_id, message="finish the review",
                                   request_id="1" * 32)
    second = resume.reserve_request(root, job_id, message="finish the review",
                                    request_id="1" * 32)
    assert first.successor_job_id == second.successor_job_id
    assert first.claim_id == second.claim_id
    ledger = _jobs.read_json(resume.ledger_path(root, job_id))
    assert ledger["generation"] == 1 and len(ledger["claims"]) == 1
    public = json.dumps(resume.public_projection(ledger["claims"][0]))
    assert "finish the review" not in public
    assert "session-private" not in public
    assert str(tmp_path) not in public


def test_conflicting_request_id_and_second_source_use_fail_closed(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    resume.reserve_request(root, job_id, message="first", request_id="2" * 32)
    with pytest.raises(resume.ResumeError) as conflict:
        resume.reserve_request(root, job_id, message="different", request_id="2" * 32)
    assert conflict.value.kind == "resume_claim_conflict"
    with pytest.raises(resume.ResumeError) as consumed:
        resume.reserve_request(root, job_id, message="second", request_id="3" * 32)
    assert consumed.value.kind == "resume_source_consumed"


def test_prompt_renderer_change_cannot_reuse_old_request(tmp_path, monkeypatch):
    root, job_id = _eligible_source(tmp_path)
    original = resume.reserve_request(root, job_id, message="same",
                                      request_id="6" * 32)
    render = resume._compose_prompt
    monkeypatch.setattr(resume, "_compose_prompt",
                        lambda message, steering: render(message, steering) + "\nnew contract")
    with pytest.raises(resume.ResumeError) as error:
        resume.reserve_request(root, job_id, message="same",
                               request_id="6" * 32)
    assert error.value.kind == "resume_claim_conflict"
    assert hashlib.sha256(original.prompt.encode()).hexdigest() == original.prompt_sha256


def test_concurrent_identical_reservations_have_one_successor(tmp_path):
    root, job_id = _eligible_source(tmp_path)

    def reserve(_):
        return resume.reserve_request(root, job_id, message="same",
                                      request_id="4" * 32).successor_job_id

    with ThreadPoolExecutor(max_workers=20) as pool:
        ids = list(pool.map(reserve, range(20)))
    assert len(set(ids)) == 1
    ledger = _jobs.read_json(resume.ledger_path(root, job_id))
    assert ledger["generation"] == 1


def test_authenticated_steering_is_snapshotted_once(tmp_path):
    root, job_id = _eligible_source(tmp_path, commands=(
        ("steer", "inspect the failing parser"),
        ("extend", 60_000),
        ("steer", "also verify Windows quoting"),
        ("extend", 30_000),
    ))
    reservation = resume.reserve_request(root, job_id, message="finish")
    assert [item["generation"] for item in reservation.steering] == [1, 3]
    ledger = _jobs.read_json(resume.ledger_path(root, job_id))
    claim = ledger["claims"][0]
    assert claim["steering_generations"] == [1, 3]
    assert claim["control_generation"] == 4
    assert len(claim["control_sha256"]) == 64
    assert "inspect the failing parser" not in json.dumps(ledger)


def test_ledger_and_source_tampering_are_rejected(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    resume.reserve_request(root, job_id, message="same")
    path = Path(resume.ledger_path(root, job_id))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["claims"][0]["permission"] = "yolo"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(resume.ResumeError) as error:
        resume.reserve_request(root, job_id, message="same")
    assert error.value.kind == "resume_claim_untrusted"


def test_phase_cas_is_single_use_and_cannot_mutate_authority(tmp_path):
    root, job_id, reservation, _prepared = _prepare(tmp_path)
    updated = resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    assert updated["parent_phase"] == "child_launch_claimed"
    with pytest.raises(resume.ResumeError) as duplicate:
        resume.claim_transition(
            root, job_id, reservation.claim_id, field="parent_phase",
            expected="successor_prepared", target="child_launch_claimed")
    assert duplicate.value.kind == "resume_launch_claimed"
    with pytest.raises(resume.ResumeError) as authority:
        resume.claim_transition(
            root, job_id, reservation.claim_id, field="parent_phase",
            expected="child_launch_claimed", target="spawned",
            updates={"permission": "yolo"})
    assert authority.value.kind == "resume_transition_invalid"


def _prepare(tmp_path, *, gate_with=None):
    root, job_id = _eligible_source(tmp_path)
    reservation = resume.reserve_request(root, job_id, message="continue",
                                         request_id="5" * 32,
                                         gate_with=gate_with)
    source = reservation.source
    successor_nonce = "successor-nonce"
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce=successor_nonce,
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id,
        resume_lineage={
            "source_job_id": job_id, "request_id": reservation.request_id,
            "claim_id": reservation.claim_id,
            "request_sha256": reservation.request_sha256,
        })
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    return root, job_id, reservation, prepared


def _canonical_terminal_result(root, source_job_id, reservation, *, contacted):
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    claim = resume.get_claim(reservation)
    return {
        "status": "error", "execution_status": "error",
        "attempt_status": "completed" if contacted else "not_run",
        "attempts": 1 if contacted else 0,
        "attempt_id": reservation.successor_job_id if contacted else None,
        "job_nonce": record["nonce"], "provider_contacted": contacted,
        "summon": record["summon"], "prompt_sha256": record["prompt_sha256"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": source_job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": claim["claim_sha256"],
        },
    }


def test_successor_claim_and_private_prompt_round_trip(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    # Preparation is idempotent before the launch claim.
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    again = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    assert again["claim_sha256"] == prepared["claim_sha256"]
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    context = resume.load_child_context(job_file, prepared["claim_file"])
    assert context.prompt.endswith("continue")
    assert context.resume_handle == "session-private"
    public = json.dumps(resume.public_projection(prepared["claim"]))
    for secret in ("session-private", "continue", str(tmp_path), "review-profile"):
        assert secret not in public


def test_mutable_reservation_cannot_escalate_authenticated_authority(tmp_path):
    root, _job_id = _eligible_source(tmp_path)
    reservation = resume.reserve_request(
        root, _job_id, message="continue", max_permission="read-only",
        allow_credit=False, allow_payg=False, request_id="7" * 32)
    forged = replace(
        reservation, effective_permission="yolo", allow_credit=True,
        allow_payg=True)
    source = reservation.source
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce="successor-nonce",
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    with pytest.raises(resume.ResumeError) as error:
        resume.prepare_successor(
            forged, successor_record=record, bundle_sha256="9" * 64)
    assert error.value.kind == "resume_reservation_changed"


def test_fresh_consent_ignores_ambient_fable_compatibility(monkeypatch):
    from _builder import credit_spend_allowed

    monkeypatch.setenv("SUMMON_FRESH_CONSENT_ONLY", "1")
    monkeypatch.setenv("SUMMON_ALLOW_FABLE", "1")
    monkeypatch.delenv("SUMMON_ALLOW_CREDIT", raising=False)
    assert credit_spend_allowed() is False
    monkeypatch.setenv("SUMMON_ALLOW_CREDIT", "1")
    assert credit_spend_allowed() is True


def test_fresh_payg_consent_requires_claim_bound_flag(tmp_path, monkeypatch):
    from _apibackend import payg_consent_allowed

    agents = tmp_path / ".agents"
    agents.mkdir()
    (agents / "summon.json").write_text(
        json.dumps({"allow_byteplus_payg": True}), encoding="utf-8")
    original_expanduser = os.path.expanduser
    monkeypatch.setattr(
        os.path, "expanduser",
        lambda value: str(tmp_path) if value == "~" else original_expanduser(value))
    monkeypatch.setenv("SUMMON_FRESH_CONSENT_ONLY", "1")
    monkeypatch.setenv("SUMMON_ALLOW_BYTEPLUS_PAYG", "1")
    assert payg_consent_allowed(False) is False
    assert payg_consent_allowed(True) is True
    monkeypatch.delenv("SUMMON_ALLOW_BYTEPLUS_PAYG")
    assert payg_consent_allowed(False) is False
    monkeypatch.delenv("SUMMON_FRESH_CONSENT_ONLY")
    assert payg_consent_allowed(False) is True


@pytest.mark.parametrize("target", ["prompt", "claim", "record"])
def test_child_context_tampering_is_refused(tmp_path, target):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    if target == "prompt":
        Path(prepared["prompt_file"]).write_text("different", encoding="utf-8")
    elif target == "claim":
        value = _jobs.read_json(prepared["claim_file"])
        value["authority"]["permission"] = "yolo"
        _jobs._atomic_write_json(prepared["claim_file"], value)
    else:
        record_file = _jobs.record_path(root, reservation.successor_job_id)
        value = _jobs.read_json(record_file)
        value["flags"]["model"] = "different"
        _jobs._atomic_write_json(record_file, value)
    with pytest.raises(resume.ResumeError):
        resume.load_child_context(
            _jobs.result_path(root, reservation.successor_job_id),
            prepared["claim_file"])


def test_provider_launch_cas_allows_one_boundary(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id), prepared["claim_file"])
    control = resume.provider_launch_control(context)
    control.before_provider_launch({"backend": "claude"})
    with pytest.raises(Exception, match="single-use"):
        control.before_provider_launch({"backend": "claude"})
    ledger = _jobs.read_json(resume.ledger_path(root, job_id))
    assert ledger["claims"][0]["provider_phase"] == "launch_claimed"


def test_two_child_controls_race_one_provider_claim(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id), prepared["claim_file"])

    def cross(_):
        try:
            resume.provider_launch_control(context).before_provider_launch(
                {"backend": "claude"})
            return "won"
        except Exception:
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(cross, range(2)))
    assert outcomes.count("won") == 1 and outcomes.count("lost") == 1


def test_provider_launch_requires_parent_and_gate_prerequisites(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    reservation = resume.reserve_request(
        root, job_id, message="continue", gate_with="security-auditor",
        request_id="8" * 32)
    source = reservation.source
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce="successor-nonce",
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    with pytest.raises(resume.ResumeError) as parent:
        resume.provider_launch_control(context).before_provider_launch({})
    assert parent.value.kind == "resume_parent_not_launched"
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    with pytest.raises(resume.ResumeError) as gate:
        resume.provider_launch_control(context).before_provider_launch({})
    assert gate.value.kind == "resume_gate_not_approved"
    gate_control = resume.provider_launch_control(context, gate=True)
    gate_control.before_provider_launch({})
    gate_control.spawned(type("Process", (), {"pid": 4101})())
    gate_control.reaped(type("Process", (), {"pid": 4101})())
    resume.mark_gate_terminal(
        context, approved=True, decision_sha256="d" * 64)
    resume.provider_launch_control(context).before_provider_launch({})


def test_terminalization_reads_canonical_result_and_rejects_forgery(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    control = resume.provider_launch_control(context)
    control.before_provider_launch({})
    process = type("Process", (), {"pid": 4102})()
    control.spawned(process)
    control.reaped(process)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    result = {
        "status": "success", "execution_status": "success",
        "attempt_status": "completed", "attempts": 1,
        "attempt_id": reservation.successor_job_id,
        "job_nonce": record["nonce"], "provider_contacted": True,
        "summon": record["summon"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": context.claim_sha256,
        },
    }
    _jobs._atomic_write_json(
        _jobs.result_path(root, reservation.successor_job_id), result)
    forged = dict(result, status="error")
    with pytest.raises(resume.ResumeError) as error:
        resume.mark_terminal(context, forged)
    assert error.value.kind == "resume_terminal_untrusted"
    claim = resume.mark_terminal(context, result)
    assert claim["parent_phase"] == "terminal"
    assert claim["provider_phase"] == "terminal"
    assert claim["provider_contacted"] is True
    assert len(claim["terminal_sha256"]) == 64
    assert resume.mark_terminal(context, result)["terminal_sha256"] == claim["terminal_sha256"]
    assert resume.reconcile_terminal(reservation)["terminal_sha256"] == claim["terminal_sha256"]


def test_gate_denial_terminalizes_without_provider_attempt(tmp_path):
    root, job_id = _eligible_source(tmp_path)
    reservation = resume.reserve_request(
        root, job_id, message="continue", gate_with="security-auditor",
        request_id="a" * 32)
    source = reservation.source
    _jobs.write_prepared(
        root, reservation.successor_job_id, nonce="successor-nonce",
        agent=source["agent"]["requested"],
        prompt_sha256=reservation.prompt_sha256,
        cwd=source["workspace"]["path"],
        flags={"cli": "claude", "model": source["model"]["targeted"]},
        summon={"version": "3.2.1", "scripts_sha256": "9" * 64},
        attempt_id=reservation.successor_job_id,
        resume_lineage={
            "source_job_id": job_id, "request_id": reservation.request_id,
            "claim_id": reservation.claim_id,
            "request_sha256": reservation.request_sha256,
        })
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    prepared = resume.prepare_successor(
        reservation, successor_record=record, bundle_sha256="9" * 64)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    gate = resume.provider_launch_control(context, gate=True)
    gate.before_provider_launch({})
    process = type("Process", (), {"pid": 4104})()
    gate.spawned(process)
    gate.reaped(process)
    resume.mark_gate_terminal(
        context, approved=False, decision_sha256="e" * 64)
    lineage = resume.authenticated_lineage(
        _jobs.result_path(root, reservation.successor_job_id))
    result = {
        "status": "blocked", "execution_status": "not_run",
        "attempt_status": "not_run", "attempts": 0, "attempt_id": None,
        "job_nonce": record["nonce"], "provider_contacted": False,
        "summon": record["summon"], "lineage": lineage,
    }
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    _jobs._atomic_write_json(job_file, result)
    claim = resume.mark_terminal_for_job(job_file, result)
    assert claim["parent_phase"] == "terminal"
    assert claim["provider_phase"] == "terminal"
    assert claim["provider_contacted"] is False


def test_dispatcher_emit_terminalizes_governed_preprovider_refusal(tmp_path, monkeypatch):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    lineage = resume.authenticated_lineage(job_file)
    monkeypatch.setenv("SUMMON_JOB_NONCE", record["nonce"])
    monkeypatch.setenv("SUMMON_JOB_ID", reservation.successor_job_id)
    old_file = dispatcher._JOB_FILE
    old_lineage = dispatcher._GOVERNED_RESUME_LINEAGE
    try:
        dispatcher._JOB_FILE = job_file
        dispatcher._GOVERNED_RESUME_LINEAGE = lineage
        dispatcher._emit({
            "status": "blocked", "execution_status": "not_run",
            "attempt_status": "not_run", "attempts": 0,
            "provider_contacted": False, "exit_code": 1,
            "summon": record["summon"],
        }, operation="resume")
    finally:
        dispatcher._JOB_FILE = old_file
        dispatcher._GOVERNED_RESUME_LINEAGE = old_lineage
    stored = _jobs.read_json(job_file)
    assert stored["lineage"] == lineage
    claim = resume.get_claim(reservation)
    assert claim["parent_phase"] == "terminal"
    assert claim["provider_phase"] == "terminal"
    assert claim["provider_contacted"] is False


def test_dispatcher_emit_records_terminalization_failure_without_rewriting_result(
        tmp_path, monkeypatch):
    root, job_id, reservation, _prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    lineage = resume.authenticated_lineage(job_file)
    monkeypatch.setenv("SUMMON_JOB_NONCE", record["nonce"])
    monkeypatch.setenv("SUMMON_JOB_ID", reservation.successor_job_id)
    monkeypatch.setattr(
        resume, "mark_terminal_for_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            resume.ResumeError("resume_terminal_state_invalid", "forced")))
    old_file = dispatcher._JOB_FILE
    old_lineage = dispatcher._GOVERNED_RESUME_LINEAGE
    try:
        dispatcher._JOB_FILE = job_file
        dispatcher._GOVERNED_RESUME_LINEAGE = lineage
        dispatcher._emit({
            "status": "blocked", "execution_status": "not_run",
            "attempt_status": "not_run", "attempts": 0,
            "provider_contacted": False, "exit_code": 1,
            "summon": record["summon"],
        }, operation="resume")
    finally:
        dispatcher._JOB_FILE = old_file
        dispatcher._GOVERNED_RESUME_LINEAGE = old_lineage
    stored = _jobs.read_json(job_file)
    assert stored["lineage"] == lineage
    assert "terminalization_error_kind" not in stored
    claim = resume.get_claim(reservation)
    projection = resume.public_projection(claim)
    assert claim["parent_phase"] == "indeterminate"
    assert projection["terminal_result_present"] is True
    assert projection["terminalization_error_kind"] == "resume_terminal_state_invalid"
    assert projection["recovery_required"] is True


def test_tampered_private_claim_can_terminalize_auth_refusal_without_contact(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    claim_file = Path(prepared["claim_file"])
    private = json.loads(claim_file.read_text(encoding="utf-8"))
    private["authority"]["permission"] = "yolo"
    _jobs._atomic_write_json(str(claim_file), private)
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    lineage = resume.authenticated_lineage(job_file)
    record = _jobs.read_json(_jobs.record_path(root, reservation.successor_job_id))
    result = {
        "status": "blocked", "execution_status": "not_run",
        "attempt_status": "not_run", "attempts": 0, "attempt_id": None,
        "job_nonce": record["nonce"], "provider_contacted": False,
        "summon": record["summon"], "lineage": lineage,
    }
    _jobs._atomic_write_json(job_file, result)
    terminal = resume.mark_terminal_for_job(job_file, result)
    assert terminal["parent_phase"] == "terminal"
    assert terminal["provider_contacted"] is False


def test_dispatcher_main_terminalizes_real_tampered_claim_refusal(tmp_path, monkeypatch):
    """The production child-auth refusal must carry its frozen bundle identity.

    This drives ``main`` rather than hand-constructing the refusal envelope.  It
    catches the case where both the terminal seal and its failure recorder reject
    an otherwise canonical result because the early envelope omitted ``summon``.
    """
    root, _job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, reservation.source_job_id, reservation.claim_id,
        field="parent_phase", expected="successor_prepared",
        target="child_launch_claimed")
    claim_file = Path(prepared["claim_file"])
    private = json.loads(claim_file.read_text(encoding="utf-8"))
    private["authority"]["permission"] = "yolo"
    _jobs._atomic_write_json(str(claim_file), private)

    job_file = _jobs.result_path(root, reservation.successor_job_id)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    monkeypatch.setenv("SUMMON_RESUME_CLAIM_FILE", str(claim_file))
    monkeypatch.setenv("SUMMON_JOB_NONCE", record["nonce"])
    monkeypatch.setenv("SUMMON_JOB_ID", reservation.successor_job_id)
    monkeypatch.setattr(
        dispatcher, "_receipt_base", lambda: {"summon": record["summon"]})
    monkeypatch.setattr(sys, "argv", [
        "run_subagent.py", "--agent", record["agent"],
        "--prompt", "governed resume placeholder",
        "--cwd", reservation.source["workspace"]["path"],
        "--job-file", job_file,
    ])
    old_file = dispatcher._JOB_FILE
    old_operation = dispatcher._EMIT_OPERATION
    old_lineage = dispatcher._GOVERNED_RESUME_LINEAGE
    try:
        with pytest.raises(SystemExit) as stopped:
            dispatcher.main()
    finally:
        dispatcher._JOB_FILE = old_file
        dispatcher._EMIT_OPERATION = old_operation
        dispatcher._GOVERNED_RESUME_LINEAGE = old_lineage

    assert stopped.value.code == 1
    stored = _jobs.read_json(job_file)
    assert stored["status"] == "blocked"
    assert stored["summon"] == record["summon"]
    assert stored["attempts"] == 0
    assert stored["provider_contacted"] is False
    terminal = resume.get_claim(reservation)
    assert terminal["parent_phase"] == "terminal"
    assert terminal["provider_phase"] == "terminal"
    assert terminal["provider_contacted"] is False


def test_provider_popen_outcomes_distinguish_proven_failure_from_ambiguity(tmp_path):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    control = resume.provider_launch_control(context)
    control.before_provider_launch({})
    control.pre_spawn_failed(OSError("no process"))
    claim = resume.get_claim(reservation)
    assert claim["provider_phase"] == "spawn_failed"
    assert claim["provider_contacted"] is False

    other = tmp_path / "other"
    other.mkdir()
    root2, job_id2, reservation2, prepared2 = _prepare(other)
    resume.claim_transition(
        root2, job_id2, reservation2.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context2 = resume.load_child_context(
        _jobs.result_path(root2, reservation2.successor_job_id),
        prepared2["claim_file"])
    ambiguous = resume.provider_launch_control(context2)
    ambiguous.before_provider_launch({})
    ambiguous.launch_indeterminate(RuntimeError("unknown"))
    claim2 = resume.get_claim(reservation2)
    assert claim2["provider_phase"] == "indeterminate"
    assert resume.public_projection(claim2)["recovery_required"] is True


@pytest.mark.parametrize("contacted", [False, True])
def test_indeterminate_provider_terminal_preserves_unknown_contact(
        tmp_path, contacted):
    root, job_id, reservation, prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    control = resume.provider_launch_control(context)
    control.before_provider_launch({})
    control.launch_indeterminate(RuntimeError("unknown"))
    result = _canonical_terminal_result(
        root, job_id, reservation, contacted=contacted)
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    _jobs._atomic_write_json(job_file, result)
    terminal = resume.mark_terminal(context, result)
    assert terminal["parent_phase"] == "terminal"
    assert terminal["provider_phase"] == "indeterminate"
    assert terminal["provider_contacted"] is None
    projection = resume.public_projection(terminal)
    assert projection["terminal_result_present"] is True
    assert projection["recovery_required"] is True
    repeated = resume.mark_terminal(context, result)
    for key in ("parent_phase", "provider_phase", "provider_contacted",
                "terminal_sha256", "terminalization_error_kind"):
        assert repeated[key] == terminal[key]


def _resume_background_fixture(tmp_path, *, allow_credit=False, allow_payg=False,
                               allow_text_only=False, require_tools=False):
    root, job_id = _eligible_source(
        tmp_path, allow_text_only=allow_text_only, require_tools=require_tools)
    reservation = resume.reserve_request(
        root, job_id, message="continue", request_id="9" * 32,
        allow_credit=allow_credit, allow_payg=allow_payg)
    parser = _cli.build_parser("3.2.1", 1)
    args = parser.parse_args([
        "--agent", "researcher", "--prompt", "placeholder",
        "--cwd", str(tmp_path), "--background",
    ])
    child = _background._resume_dispatch_args(args, reservation)
    entry = str((Path(__file__).parent / "run_subagent.py").resolve())
    summon = {
        "version": "3.2.1", "script": entry,
        "scripts_sha256": scripts_sha256(str(Path(entry).parent)),
    }
    return root, job_id, reservation, child, entry, summon


@pytest.mark.parametrize("allow_credit,allow_payg", [
    (False, False), (True, True),
])
def test_resume_child_receives_only_claim_bound_spend_consent(
        tmp_path, monkeypatch, allow_credit, allow_payg):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(
        tmp_path, allow_credit=allow_credit, allow_payg=allow_payg)
    monkeypatch.setenv("SUMMON_ALLOW_CREDIT", "1")
    monkeypatch.setenv("SUMMON_ALLOW_FABLE", "1")
    monkeypatch.setenv("SUMMON_ALLOW_BYTEPLUS_PAYG", "1")
    monkeypatch.setenv("SUMMON_ALLOW_TEXT_ONLY", "1")
    captured = {}

    class Process:
        pid = 4102

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(_background.subprocess, "Popen", fake_popen)
    _background.spawn_background(
        child, entry, summon, resume_reservation=reservation)
    env = captured["env"]
    command = captured["cmd"]
    assert env["SUMMON_FRESH_CONSENT_ONLY"] == "1"
    assert env.get("SUMMON_ALLOW_CREDIT") == ("1" if allow_credit else None)
    assert "SUMMON_ALLOW_FABLE" not in env
    assert "SUMMON_ALLOW_BYTEPLUS_PAYG" not in env
    assert "SUMMON_ALLOW_TEXT_ONLY" not in env
    assert ("--allow-credit" in command) is allow_credit
    assert ("--allow-payg" in command) is allow_payg


@pytest.mark.parametrize("allow_text_only,require_tools", [
    (False, False), (True, False), (False, True),
])
def test_resume_child_preserves_claim_bound_text_capability(
        tmp_path, monkeypatch, allow_text_only, require_tools):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(
        tmp_path, allow_text_only=allow_text_only, require_tools=require_tools)
    monkeypatch.setenv("SUMMON_ALLOW_TEXT_ONLY", "1")
    captured = {}

    class Process:
        pid = 4105

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(_background.subprocess, "Popen", fake_popen)
    _background.spawn_background(
        child, entry, summon, resume_reservation=reservation)
    assert "SUMMON_ALLOW_TEXT_ONLY" not in captured["env"]
    assert ("--allow-text-only" in captured["cmd"]) is allow_text_only
    assert ("--require-tools" in captured["cmd"]) is require_tools
    prepared = resume.get_claim(reservation)
    assert prepared["parent_phase"] == "spawned"


@pytest.mark.parametrize("field", ["allow_text_only", "require_tools"])
def test_resume_child_text_capability_drift_refuses_before_launch(
        tmp_path, field):
    root, _job_id, reservation, prepared = _prepare(tmp_path)
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    context = resume.load_child_context(job_file, prepared["claim_file"])
    source = context.source
    invocation = _executor.AgentInvocation(
        cli="claude", prompt=context.prompt, cwd=source["workspace"]["path"],
        agent_file=source["agent"]["file"], permission=context.effective_permission,
        transport="subprocess", model=source["model"]["targeted"],
        model_exact_required=True, model_exact_source="named-seat",
        effort=source["authority"]["effort"],
        read_roots=tuple(source["authority"]["read_roots"]),
        isolated_lane=source["authority"]["isolated_lane"],
        allow_tool_credentials=source["authority"]["allow_tool_credentials"],
        profile=source["backend"]["profile"], resume_id=context.resume_handle,
    )
    args = SimpleNamespace(
        agent=source["agent"]["requested"],
        _resolved_agent=source["agent"]["resolved"], _role_provenance={},
        agents_dir=source["agent"]["agents_dir"],
        strict_agents_dir=source["authority"]["strict_agents_dir"],
        enable_roles=source["authority"]["enable_roles"],
        allow_credit=context.allow_credit, allow_payg=context.allow_payg,
        allow_text_only=source["authority"]["allow_text_only"],
        require_tools=source["authority"]["require_tools"],
        gate_with=context.gate_with, gate_timeout=context.gate_timeout_ms,
        timeout=context.timeout_ms, max_runtime=context.max_runtime_ms,
        retries=0, no_contract_repair=True, no_acp_fallback=True,
    )
    receipt = {
        "agent_def": {"sha256": source["agent"]["definition_sha256"]},
        "profile": {
            "name": source["backend"]["profile"],
            "path_sha256": source["backend"]["profile_path_sha256"],
            "registry_sha256": source["backend"]["profile_registry_sha256"],
            "command_sha256": source["backend"]["profile_command_sha256"],
        },
    }
    resume.validate_loaded_invocation(context, invocation, args, receipt)
    setattr(args, field, not getattr(args, field))
    with pytest.raises(resume.ResumeError) as refused:
        resume.validate_loaded_invocation(context, invocation, args, receipt)
    assert refused.value.kind == "resume_child_drift"
    assert resume.get_claim(reservation)["provider_phase"] == "pending"


def test_proven_popen_failure_reuses_same_frozen_successor(tmp_path, monkeypatch):
    root, job_id, reservation, child, entry, summon = _resume_background_fixture(tmp_path)
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError("proven pre-spawn failure")

    monkeypatch.setattr(_background.subprocess, "Popen", fail)
    with pytest.raises(OSError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation)
    failed = resume.get_claim(reservation)
    assert failed["parent_phase"] == "spawn_failed"
    assert failed["provider_contacted"] is False
    bundle_dirs = list(Path(root).glob(f".summon-bundle-{reservation.successor_job_id}-*"))
    assert len(bundle_dirs) == 1

    class Process:
        pid = 4103

    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (calls.append((args, kwargs)) or Process()))
    handle = _background.spawn_background(
        child, entry, summon, resume_reservation=reservation,
        resume_recovery=True)
    assert handle["job_id"] == reservation.successor_job_id
    assert handle["pid"] == 4103
    assert len(calls) == 2
    assert len(list(Path(root).glob(
        f".summon-bundle-{reservation.successor_job_id}-*"))) == 1
    claim = resume.get_claim(reservation)
    assert claim["parent_phase"] == "spawned"
    assert claim["pid"] == 4103


def test_ambiguous_popen_failure_is_not_recoverable(tmp_path, monkeypatch):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(tmp_path)
    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous")))
    with pytest.raises(RuntimeError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation)
    claim = resume.get_claim(reservation)
    assert claim["parent_phase"] == "indeterminate"
    with pytest.raises(ValueError, match="not safely recoverable"):
        _background._load_resume_recovery(root, reservation)


def test_recovery_ambiguity_clears_stale_pre_spawn_contact_claim(tmp_path, monkeypatch):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(tmp_path)
    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no process")))
    with pytest.raises(OSError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation)
    assert resume.get_claim(reservation)["provider_contacted"] is False

    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ambiguous")))
    with pytest.raises(RuntimeError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation,
            resume_recovery=True)
    claim = resume.get_claim(reservation)
    assert claim["parent_phase"] == "indeterminate"
    assert claim["provider_contacted"] is None


def test_concurrent_recovery_launches_same_successor_once(tmp_path, monkeypatch):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(tmp_path)
    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no process")))
    with pytest.raises(OSError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation)

    calls = []
    barrier = __import__("threading").Barrier(2)

    class Process:
        pid = 4105

    def popen(*args, **kwargs):
        calls.append(1)
        return Process()

    monkeypatch.setattr(_background.subprocess, "Popen", popen)

    def recover(_):
        barrier.wait()
        try:
            _background.spawn_background(
                child, entry, summon, resume_reservation=reservation,
                resume_recovery=True)
            return "won"
        except (resume.ResumeError, ValueError):
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(recover, range(2)))
    assert outcomes.count("won") == 1 and outcomes.count("lost") == 1
    assert len(calls) == 1
    claim = resume.get_claim(reservation)
    assert claim["parent_phase"] == "spawned"


def test_concurrent_initial_resume_returns_one_successor_without_record_error(
        tmp_path, monkeypatch, capsys):
    root, source_job_id = _eligible_source(tmp_path)
    entry = str((Path(__file__).parent / "run_subagent.py").resolve())
    summon = {
        "version": "3.2.1", "script": entry,
        "scripts_sha256": scripts_sha256(str(Path(entry).parent)),
    }
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "alive")
    invoke_barrier = __import__("threading").Barrier(2)
    popen_calls = []

    class Process:
        pid = 4106

    def popen(*args, **kwargs):
        popen_calls.append(1)
        return Process()

    monkeypatch.setattr(_background.subprocess, "Popen", popen)
    parser = _cli.build_parser("3.2.1", 1)

    def invoke(_):
        invoke_barrier.wait(timeout=10)
        args = parser.parse_args([
            "--jobs-resume", source_job_id,
            "--job-message", "continue",
            "--job-request-id", "8" * 32,
            "--job-dir", root,
        ])
        errors = []
        code = _background.run_jobs_query(
            args, lambda message, **kwargs: errors.append((message, kwargs)),
            entry_path=entry, summon=summon)
        return code, errors

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(invoke, index) for index in range(2)]
        outcomes = [future.result(timeout=60) for future in futures]
    assert outcomes == [(0, []), (0, [])]
    assert len(popen_calls) == 1
    ledger = _jobs.read_json(resume.ledger_path(root, source_job_id))
    assert len(ledger["claims"]) == 1
    claim = ledger["claims"][0]
    assert claim["parent_phase"] == "spawned"
    assert os.path.lexists(_jobs.record_path(root, claim["successor_job_id"]))
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 2
    assert {report["job_id"] for report in reports} == {claim["successor_job_id"]}


@pytest.mark.parametrize(
    "boundary", ["freeze", "record", "record_after", "claim", "claim_after"])
def test_preparation_fault_keeps_exact_request_recoverable(tmp_path, monkeypatch, boundary):
    root, _job_id, reservation, child, entry, summon = _resume_background_fixture(tmp_path)
    original_freeze = _background._freeze_background_bundle
    original_write = _jobs.write_prepared
    original_prepare = resume.prepare_successor
    if boundary == "freeze":
        monkeypatch.setattr(
            _background, "_freeze_background_bundle",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("freeze fault")))
    elif boundary == "record":
        monkeypatch.setattr(
            _jobs, "write_prepared",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("record fault")))
    elif boundary == "record_after":
        def write_then_fail(*args, **kwargs):
            original_write(*args, **kwargs)
            raise OSError("record post-rename fault")
        monkeypatch.setattr(_jobs, "write_prepared", write_then_fail)
    elif boundary == "claim":
        monkeypatch.setattr(
            resume, "prepare_successor",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("claim fault")))
    else:
        def prepare_then_fail(*args, **kwargs):
            original_prepare(*args, **kwargs)
            raise OSError("claim post-rename fault")
        monkeypatch.setattr(resume, "prepare_successor", prepare_then_fail)
    with pytest.raises(ValueError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation)
    claim = resume.get_claim(reservation)
    expected_phase = "successor_prepared" if boundary == "claim_after" else "reserved"
    assert claim["parent_phase"] == expected_phase
    assert claim["provider_contacted"] is None

    monkeypatch.setattr(_background, "_freeze_background_bundle", original_freeze)
    monkeypatch.setattr(_jobs, "write_prepared", original_write)
    monkeypatch.setattr(resume, "prepare_successor", original_prepare)
    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("proven no process")))
    has_record = os.path.lexists(
        _jobs.record_path(root, reservation.successor_job_id))
    with pytest.raises(OSError):
        _background.spawn_background(
            child, entry, summon, resume_reservation=reservation,
            resume_recovery=boundary == "claim_after",
            resume_prepare_recovery=has_record and boundary != "claim_after")
    recovered = resume.get_claim(reservation)
    assert recovered["parent_phase"] == "spawn_failed"
    assert recovered["provider_contacted"] is False


def _query_reserved_resume(root, source_job_id, entry, summon, monkeypatch, capsys,
                           *, request_id="9" * 32, gate_with=None):
    parser = _cli.build_parser("3.2.1", 1)
    argv = [
        "--jobs-resume", source_job_id,
        "--job-message", "continue",
        "--job-request-id", request_id,
        "--job-dir", root,
    ]
    if gate_with is not None:
        argv += ["--gate-with", gate_with]
    args = parser.parse_args(argv)
    errors = []
    code = _background.run_jobs_query(
        args, lambda message, **kwargs: errors.append((message, kwargs)),
        entry_path=entry, summon=summon)
    output = capsys.readouterr().out
    return code, errors, json.loads(output) if output else None


def _spawned_resume_fixture(tmp_path, monkeypatch):
    root, source_job_id, reservation, child, entry, summon = _resume_background_fixture(
        tmp_path)
    class Process:
        pid = 4107
    monkeypatch.setattr(_background.subprocess, "Popen", lambda *a, **k: Process())
    _background.spawn_background(
        child, entry, summon, resume_reservation=reservation)
    return root, source_job_id, reservation, entry, summon


def test_dead_spawned_successor_becomes_indeterminate_not_background(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, entry, summon = _spawned_resume_fixture(
        tmp_path, monkeypatch)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "dead")
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys)
    assert code == 1 and not errors
    assert report["status"] == "blocked"
    assert report["resume"]["parent_phase"] == "indeterminate"
    assert report["resume"]["recovery_required"] is True
    assert resume.get_claim(reservation)["parent_phase"] == "indeterminate"


@pytest.mark.parametrize("liveness", ["alive", "dead"])
@pytest.mark.parametrize("defect", ["wrong_nonce", "identity_mismatch"])
def test_untrusted_spawned_result_becomes_indeterminate(
        tmp_path, monkeypatch, capsys, liveness, defect):
    root, source_job_id, reservation, entry, summon = _spawned_resume_fixture(
        tmp_path, monkeypatch)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    claim = resume.get_claim(reservation)
    result = {
        "status": "blocked", "execution_status": "not_run",
        "attempt_status": "not_run", "attempts": 0, "attempt_id": None,
        "job_nonce": record["nonce"], "provider_contacted": False,
        "summon": record["summon"], "prompt_sha256": record["prompt_sha256"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": source_job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": claim["claim_sha256"],
        },
    }
    if defect == "wrong_nonce":
        result["job_nonce"] = "wrong-nonce"
    else:
        result["summon"] = dict(record["summon"], scripts_sha256="f" * 64)
    _jobs._atomic_write_json(
        _jobs.result_path(root, reservation.successor_job_id), result)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: liveness)
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys)
    assert code == 1 and not errors
    assert report["status"] == "blocked"
    assert report["resume"]["parent_phase"] == "indeterminate"
    assert report["resume"]["recovery_required"] is True


def test_trusted_spawned_terminal_result_reconciles_complete(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, entry, summon = _spawned_resume_fixture(
        tmp_path, monkeypatch)
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    claim = resume.get_claim(reservation)
    result = {
        "status": "blocked", "execution_status": "not_run",
        "attempt_status": "not_run", "attempts": 0, "attempt_id": None,
        "job_nonce": record["nonce"], "provider_contacted": False,
        "summon": record["summon"], "prompt_sha256": record["prompt_sha256"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": source_job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": claim["claim_sha256"],
        },
    }
    _jobs._atomic_write_json(
        _jobs.result_path(root, reservation.successor_job_id), result)
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys)
    assert code == 0 and not errors
    assert report["status"] == "complete"
    assert report["resume"]["parent_phase"] == "terminal"
    assert report["resume"]["recovery_required"] is False


def test_terminal_result_with_indeterminate_gate_remains_blocked(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, prepared = _prepare(
        tmp_path, gate_with="security-auditor")
    resume.claim_transition(
        root, source_job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    context = resume.load_child_context(
        _jobs.result_path(root, reservation.successor_job_id),
        prepared["claim_file"])
    gate = resume.provider_launch_control(context, gate=True)
    gate.before_provider_launch({})
    gate.launch_indeterminate(RuntimeError("unknown"))
    result = _canonical_terminal_result(
        root, source_job_id, reservation, contacted=False)
    job_file = _jobs.result_path(root, reservation.successor_job_id)
    _jobs._atomic_write_json(job_file, result)
    terminal = resume.mark_terminal(context, result)
    assert terminal["parent_phase"] == "terminal"
    assert terminal["gate_phase"] == "indeterminate"
    entry = str((Path(__file__).parent / "run_subagent.py").resolve())
    summon = {
        "version": "3.2.1", "script": entry,
        "scripts_sha256": scripts_sha256(str(Path(entry).parent)),
    }
    monkeypatch.setattr(
        _background.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("recovery-required terminal must not relaunch"))
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32, gate_with="security-auditor")
    assert code == 1 and not errors
    assert report["status"] == "blocked"
    assert report["resume"]["terminal_result_present"] is True
    assert report["resume"]["recovery_required"] is True


def _claimed_resume_fixture(tmp_path):
    root, source_job_id, reservation, _prepared = _prepare(tmp_path)
    resume.claim_transition(
        root, source_job_id, reservation.claim_id, field="parent_phase",
        expected="successor_prepared", target="child_launch_claimed")
    entry = str((Path(__file__).parent / "run_subagent.py").resolve())
    summon = {
        "version": "3.2.1", "script": entry,
        "scripts_sha256": scripts_sha256(str(Path(entry).parent)),
    }
    return root, source_job_id, reservation, entry, summon


def _claimed_terminal_result(root, source_job_id, reservation):
    record = _jobs.read_json(
        _jobs.record_path(root, reservation.successor_job_id))
    claim = resume.get_claim(reservation)
    return {
        "status": "blocked", "execution_status": "not_run",
        "attempt_status": "not_run", "attempts": 0, "attempt_id": None,
        "job_nonce": record["nonce"], "provider_contacted": False,
        "summon": record["summon"], "prompt_sha256": record["prompt_sha256"],
        "lineage": {
            "kind": "governed_resume", "source_job_id": source_job_id,
            "claim_id": reservation.claim_id,
            "claim_sha256": claim["claim_sha256"],
        },
    }


def test_child_launch_claimed_live_record_repairs_to_spawned_background(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, entry, summon = _claimed_resume_fixture(tmp_path)
    _jobs.update_spawned(root, reservation.successor_job_id, 4108)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "alive")
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32)
    assert code == 0 and not errors
    assert report["status"] == "background"
    assert report["resume"]["parent_phase"] == "spawned"
    assert resume.get_claim(reservation)["pid"] == 4108


def test_child_launch_claimed_dead_record_becomes_indeterminate(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, entry, summon = _claimed_resume_fixture(tmp_path)
    _jobs.update_spawned(root, reservation.successor_job_id, 4109)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "dead")
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32)
    assert code == 1 and not errors
    assert report["status"] == "blocked"
    assert report["resume"]["parent_phase"] == "indeterminate"
    assert report["resume"]["recovery_required"] is True


@pytest.mark.parametrize("defect", ["wrong_nonce", "identity_mismatch"])
def test_child_launch_claimed_untrusted_result_becomes_indeterminate(
        tmp_path, monkeypatch, capsys, defect):
    root, source_job_id, reservation, entry, summon = _claimed_resume_fixture(tmp_path)
    _jobs.update_spawned(root, reservation.successor_job_id, 4110)
    result = _claimed_terminal_result(root, source_job_id, reservation)
    if defect == "wrong_nonce":
        result["job_nonce"] = "wrong-nonce"
    else:
        result["summon"] = dict(result["summon"], scripts_sha256="e" * 64)
    _jobs._atomic_write_json(
        _jobs.result_path(root, reservation.successor_job_id), result)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "alive")
    code, errors, report = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32)
    assert code == 1 and not errors
    assert report["resume"]["parent_phase"] == "indeterminate"
    assert report["resume"]["recovery_required"] is True


def test_trusted_terminal_can_close_prior_indeterminate_claim(
        tmp_path, monkeypatch, capsys):
    root, source_job_id, reservation, entry, summon = _claimed_resume_fixture(tmp_path)
    _jobs.update_spawned(root, reservation.successor_job_id, 4111)
    monkeypatch.setattr(_jobs, "_pid_liveness", lambda _pid: "dead")
    code, errors, first = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32)
    assert code == 1 and not errors
    assert first["resume"]["parent_phase"] == "indeterminate"
    result = _claimed_terminal_result(root, source_job_id, reservation)
    _jobs._atomic_write_json(
        _jobs.result_path(root, reservation.successor_job_id), result)
    code, errors, second = _query_reserved_resume(
        root, source_job_id, entry, summon, monkeypatch, capsys,
        request_id="5" * 32)
    assert code == 0 and not errors
    assert second["status"] == "complete"
    assert second["resume"]["parent_phase"] == "terminal"


def test_default_ambient_profile_is_not_certified(tmp_path):
    root, job_id, job_file, invocation, args, result = _fixture(tmp_path)
    continuation.write_private_source(job_file, result, invocation, args)
    _publish_result(job_file, result)
    with pytest.raises(resume.ResumeError) as error:
        resume.reserve_request(root, job_id, message="continue")
    assert error.value.kind == "resume_profile_unverified"

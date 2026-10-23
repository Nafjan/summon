"""Tests for Phase 5 context-aware handoff, structured notes, and headroom routing."""

from __future__ import annotations

import copy
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _context_handoff
import _executor
import _job_control
import _job_resume
import _jobs
from _stream import StreamProcessor


class ManualClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = float(initial)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _write_launch_record(root: str, job_id: str, nonce: str = "nonce-secret-123456") -> dict:
    rec_path = _jobs.record_path(root, job_id)
    os.makedirs(os.path.dirname(rec_path), exist_ok=True)
    record = {
        "schema": "summon.job-launch/v2",
        "job_id": job_id,
        "nonce": nonce,
        "created_at": 1000.0,
    }
    _jobs._atomic_write_json(rec_path, record)
    return record


def test_window_scaled_thresholds_and_failsafe_missing_evidence(monkeypatch):
    monkeypatch.delenv("SUMMON_CONTEXT_HANDOFF_THRESHOLD", raising=False)

    # Window scaling: <= 200k -> 0.75, >= 1M -> 0.50, 600k -> 0.625
    assert _context_handoff.handoff_threshold_for_window(128_000) == 0.75
    assert _context_handoff.handoff_threshold_for_window(200_000) == 0.75
    assert _context_handoff.handoff_threshold_for_window(600_000) == 0.625
    assert _context_handoff.handoff_threshold_for_window(1_000_000) == 0.50
    assert _context_handoff.handoff_threshold_for_window(2_000_000) == 0.50

    # Explicit override and environment override
    assert _context_handoff.handoff_threshold_for_window(200_000, override=0.60) == 0.60
    monkeypatch.setenv("SUMMON_CONTEXT_HANDOFF_THRESHOLD", "0.65")
    assert _context_handoff.threshold_from_environment() == 0.65
    monkeypatch.setenv("SUMMON_CONTEXT_HANDOFF_THRESHOLD", "invalid")
    assert _context_handoff.threshold_from_environment() is None
    monkeypatch.setenv("SUMMON_CONTEXT_HANDOFF_THRESHOLD", "0.02")
    assert _context_handoff.threshold_from_environment() is None

    # Fail-safe when occupancy evidence != "reported"
    for bad_occ in (
        None,
        {},
        {"evidence": "absent", "occupied_tokens": None, "window_tokens": None, "ratio": None},
        {"evidence": "occupied_without_window", "occupied_tokens": 180_000, "window_tokens": None, "ratio": None},
        {"evidence": "ambiguous_cache_semantics", "occupied_tokens": None, "window_tokens": 200_000, "ratio": None},
        {"evidence": "incomplete", "occupied_tokens": 190_000, "window_tokens": 200_000, "ratio": None},
        # Forged/inconsistent ratio fails closed to incomplete
        {"evidence": "reported", "occupied_tokens": 50_000, "window_tokens": 200_000, "ratio": 0.95},
    ):
        res = _context_handoff.evaluate_context_handoff(bad_occ, mode="enforce")
        assert res["schema"] == _context_handoff.SCHEMA
        assert res["triggered"] is False
        assert res["newly_triggered"] is False
        assert res["enforced"] is False
        assert res["action"] == "none"
        assert res["reason"] == "occupancy_evidence_unavailable"
        assert _context_handoff.validate_projection(res)


def test_enforce_mode_latches_at_most_once_and_queues_steering(tmp_path):
    root = str(tmp_path)
    job_id = "a" * 32
    nonce = "nonce-handoff-enforce-001"
    _write_launch_record(root, job_id, nonce)

    clock = ManualClock(1000.0)
    rc = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id,
        nonce=nonce,
        checkpoint_ms=60_000,
        max_runtime_ms=600_000,
        job_started_at=1000.0,
        clock=clock,
        wall_clock=clock,
    )
    tracker = _context_handoff.ContextHandoffTracker(mode="enforce")

    # 1) Below threshold (100k / 200k = 0.50 < 0.75)
    occ_low = {
        "schema": "summon.context-occupancy/v1",
        "occupied_tokens": 100_000,
        "window_tokens": 200_000,
        "ratio": 0.50,
        "evidence": "reported",
    }
    d1 = tracker.evaluate(occ_low)
    queued1 = rc.record_context_handoff(d1)
    assert queued1 is None
    assert tracker.triggered is False
    assert rc.handoff_triggered is False
    assert rc.attention_required is False
    assert len(rc.steers) == 0

    # 2) Crosses threshold on 200k window (160k / 200k = 0.80 >= 0.75)
    occ_high = {
        "schema": "summon.context-occupancy/v1",
        "occupied_tokens": 160_000,
        "window_tokens": 200_000,
        "ratio": 0.80,
        "evidence": "reported",
    }
    d2 = tracker.evaluate(occ_high)
    assert d2["triggered"] is True
    assert d2["newly_triggered"] is True
    assert d2["enforced"] is True
    assert d2["action"] == "prepare_handoff"
    assert d2["reason"] == "context_occupancy_threshold_reached"

    queued2 = rc.record_context_handoff(d2)
    assert queued2 is not None
    assert queued2["action"] == "steer"
    assert tracker.triggered is True
    assert tracker.trigger_count == 1
    assert rc.handoff_triggered is True
    assert rc.attention_required is True
    assert len(rc.steers) == 1
    assert rc.steers[0]["message"] == _context_handoff.HANDOFF_STEERING_PROMPT

    # 3) Subsequent evaluation above threshold is latched (no duplicate steer)
    occ_higher = {
        "schema": "summon.context-occupancy/v1",
        "occupied_tokens": 180_000,
        "window_tokens": 200_000,
        "ratio": 0.90,
        "evidence": "reported",
    }
    d3 = tracker.evaluate(occ_higher)
    assert d3["triggered"] is True
    assert d3["newly_triggered"] is False
    assert d3["enforced"] is False
    assert d3["action"] == "none"
    assert d3["reason"] == "handoff_already_triggered"
    assert d3["trigger_count"] == 1

    queued3 = rc.record_context_handoff(d3)
    assert queued3 is None
    assert len(rc.steers) == 1

    # 4) A new RuntimeControl + fresh tracker restores the latch from the authenticated control log
    rc_restarted = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id,
        nonce=nonce,
        checkpoint_ms=60_000,
        max_runtime_ms=600_000,
        job_started_at=1000.0,
        clock=clock,
        wall_clock=clock,
    )
    rc_restarted.refresh(force=True)
    assert rc_restarted.handoff_triggered is True
    tracker_restarted = _context_handoff.ContextHandoffTracker(mode="enforce")
    tracker_restarted.sync_from_control(rc_restarted)
    assert tracker_restarted.triggered is True
    d4 = tracker_restarted.evaluate(occ_higher)
    assert d4["newly_triggered"] is False
    assert d4["reason"] == "handoff_already_triggered"


def test_shadow_and_off_modes_never_mutate_control_or_open_attention(tmp_path):
    root = str(tmp_path)
    job_id = "b" * 32
    nonce = "nonce-handoff-shadow-001"
    _write_launch_record(root, job_id, nonce)

    clock = ManualClock(1000.0)
    rc = _job_control.RuntimeControl(
        path=_job_control.control_path(root, job_id),
        heartbeat=_job_control.heartbeat_path(root, job_id),
        job_id=job_id,
        nonce=nonce,
        checkpoint_ms=60_000,
        max_runtime_ms=600_000,
        job_started_at=1000.0,
        clock=clock,
        wall_clock=clock,
    )

    # 1M window threshold is 0.50; 550k / 1M = 0.55 triggers shadow observation
    occ_1m = {
        "schema": "summon.context-occupancy/v1",
        "occupied_tokens": 550_000,
        "window_tokens": 1_000_000,
        "ratio": 0.55,
        "evidence": "reported",
    }
    shadow_tracker = _context_handoff.ContextHandoffTracker(mode="shadow")
    d_shadow = shadow_tracker.evaluate(occ_1m)
    assert d_shadow["triggered"] is True
    assert d_shadow["newly_triggered"] is True
    assert d_shadow["enforced"] is False
    assert d_shadow["action"] == "observe_handoff"
    assert d_shadow["threshold"] == 0.50
    assert rc.record_context_handoff(d_shadow) is None
    assert rc.attention_required is False
    assert len(rc.steers) == 0

    off_tracker = _context_handoff.ContextHandoffTracker(mode="off")
    d_off = off_tracker.evaluate(occ_1m)
    assert d_off["triggered"] is False
    assert d_off["action"] == "none"
    assert d_off["reason"] == "mode_off"


def test_structured_handoff_note_normalization_redaction_and_digest():
    raw_note = {
        "state": "partial",
        "completed": [
            "Implemented _context_handoff.py",
            "Used token sk-proj-abcdef0123456789abcdef in test fixture",
        ],
        "next_steps": [
            "Wire _job_resume.py",
            "Run pytest suite",
        ],
        "open_questions": [
            "Should threshold scale linearly? api_key=supersecretvalue123",
        ],
        "artifacts_touched": [
            "skills/summon/scripts/_context_handoff.py",
            "../outside_repo.py",
            "/etc/passwd",
            "C:\\Windows\\System32\\cmd.exe",
            ".git/config",
            "PROGRA~1/foo.py",
            "skills/summon/scripts/_job_resume.py",
        ],
    }
    normalized = _context_handoff.normalize_handoff_note(raw_note)
    assert normalized is not None
    assert normalized["state"] == "partial"
    assert len(normalized["completed"]) == 2
    assert "sk-proj-" not in normalized["completed"][1]
    assert "[REDACTED]" in normalized["completed"][1]
    assert "supersecretvalue123" not in normalized["open_questions"][0]
    assert normalized["artifacts_touched"] == [
        "skills/summon/scripts/_context_handoff.py",
        "skills/summon/scripts/_job_resume.py",
    ]

    digest = _context_handoff.handoff_note_digest(normalized)
    assert isinstance(digest, str) and len(digest) == 64

    rendered = _context_handoff.format_handoff_for_resume(normalized)
    assert rendered is not None
    assert "state: partial" in rendered
    assert "sk-proj-" not in rendered
    assert "supersecretvalue123" not in rendered
    assert "skills/summon/scripts/_context_handoff.py" in rendered

    # Projection never exposes raw handoff note fields
    tracker = _context_handoff.ContextHandoffTracker(mode="shadow")
    tracker.attach_handoff_note(normalized)
    proj = tracker.projection()
    assert _context_handoff.validate_projection(proj)
    assert proj["handoff_note_present"] is True
    assert proj["handoff_note_sha256"] == digest
    assert "completed" not in proj
    assert "next_steps" not in proj


def test_job_resume_binds_handoff_note_sha256_and_preserves_none_compatibility():
    base_kwargs = {
        "source": {"result_binding_sha256": "a" * 64, "attempt_id": "1" * 32},
        "prompt_sha256": "d" * 64,
        "message": "Continue Phase 5",
        "steering": [{"generation": 1, "message": "Focus on tests", "message_sha256": "b" * 64}],
        "control_generation": 1,
        "control_sha256": "c" * 64,
        "permission": "safe-edit",
        "gate_with": None,
        "allow_credit": False,
        "allow_payg": False,
        "timeout_ms": 60_000,
        "gate_timeout_ms": 60_000,
        "max_runtime_ms": 600_000,
    }
    req_without = _job_resume._semantic_request(**base_kwargs)
    req_explicit_none = _job_resume._semantic_request(**base_kwargs, handoff_note_sha256=None)
    assert req_without == req_explicit_none
    assert "handoff_note_sha256" not in req_without

    prompt_without = _job_resume._compose_prompt(
        "Continue Phase 5",
        base_kwargs["steering"],
        handoff_note=None,
    )
    assert "Structured context handoff" not in prompt_without

    note = {
        "state": "partial",
        "completed": ["Step 1 done"],
        "next_steps": ["Step 2 next"],
        "open_questions": [],
        "artifacts_touched": ["skills/summon/scripts/_context_handoff.py"],
    }
    note_sha = _context_handoff.handoff_note_digest(note)
    req_with_note = _job_resume._semantic_request(**base_kwargs, handoff_note_sha256=note_sha)
    assert "handoff_note_sha256" in req_with_note
    assert req_with_note["handoff_note_sha256"] == note_sha

    prompt_with_note = _job_resume._compose_prompt(
        "Continue Phase 5",
        base_kwargs["steering"],
        handoff_note=note,
    )
    assert "Structured context handoff:" in prompt_with_note
    assert "Step 1 done" in prompt_with_note
    assert "Step 2 next" in prompt_with_note


def test_continuation_headroom_routing():
    # Missing evidence -> fail-safe continuation_eligible=True, prefer_fresh_lane=False
    missing = _context_handoff.evaluate_continuation_headroom(
        {"evidence": "absent", "occupied_tokens": None, "window_tokens": None, "ratio": None}
    )
    assert missing["continuation_eligible"] is True
    assert missing["prefer_fresh_lane"] is False
    assert missing["reason"] == "occupancy_evidence_unavailable"

    # Sufficient headroom on 200k window (100k / 200k = 0.50 < 0.75)
    ok = _context_handoff.evaluate_continuation_headroom(
        {"evidence": "reported", "occupied_tokens": 100_000, "window_tokens": 200_000, "ratio": 0.50}
    )
    assert ok["continuation_eligible"] is True
    assert ok["prefer_fresh_lane"] is False
    assert ok["reason"] == "sufficient_context_headroom"

    # Exceeded occupancy on 1M window (550k / 1M = 0.55 >= 0.50 threshold)
    exceeded = _context_handoff.evaluate_continuation_headroom(
        {"evidence": "reported", "occupied_tokens": 550_000, "window_tokens": 1_000_000, "ratio": 0.55}
    )
    assert exceeded["continuation_eligible"] is False
    assert exceeded["prefer_fresh_lane"] is True
    assert exceeded["reason"] == "context_occupancy_exceeded"


def test_executor_enrich_and_public_heartbeat_integration(tmp_path):
    proc = StreamProcessor(cli="claude")
    proc.last_request_usage = {"input_tokens": 160_000, "output_tokens": 5_000}
    proc.context_window = 200_000
    proc.context_handoff_tracker = _context_handoff.ContextHandoffTracker(mode="shadow")

    resp = {
        "status": "partial",
        "cli": "claude",
        "exit_code": 0,
        "files_touched": ["skills/summon/scripts/_context_handoff.py"],
        "result": (
            "Completed initial wiring.\n\n"
            "STATUS: PARTIAL\n"
            "SUMMARY: Implemented Phase 5 handoff tracker\n"
            "FINDINGS: Window threshold scaling works\n"
            "FOLLOW-UP: Run full test suite\n"
            "HANDOFF: Resume from test_context_handoff.py\n"
            "LEFT_BEHIND: none\n"
        ),
    }
    enriched = _executor._enrich(resp, proc)
    ch = enriched.get("context_handoff")
    assert isinstance(ch, dict)
    assert _context_handoff.validate_projection(ch)
    assert ch["triggered"] is True
    assert ch["ratio"] == 0.8
    assert ch["threshold"] == 0.75
    assert ch["handoff_note_present"] is True
    assert isinstance(ch["handoff_note_sha256"], str) and len(ch["handoff_note_sha256"]) == 64

    # Heartbeat projection allowlists valid context_handoff and drops tampered dicts
    hb = {
        "schema": "summon.job-heartbeat/v2",
        "job_id": "c" * 32,
        "context_handoff": copy.deepcopy(ch),
    }
    pub = _job_control.public_heartbeat(hb, "authenticated")
    assert pub is not None
    assert pub.get("context_handoff") == ch

    hb_tampered = {
        "schema": "summon.job-heartbeat/v2",
        "job_id": "c" * 32,
        "context_handoff": {**ch, "raw_prompt": "leaked secret"},
    }
    pub_tampered = _job_control.public_heartbeat(hb_tampered, "authenticated")
    assert pub_tampered is not None
    assert "context_handoff" not in pub_tampered


def test_round1_sol_review_regressions(tmp_path, monkeypatch):
    # 1. Live stream events populate context_window and _enrich re-evaluates when
    # terminal telemetry upgrades occupancy evidence to "reported".
    proc_claude = StreamProcessor(cli="claude")
    proc_claude.context_handoff_tracker = _context_handoff.ContextHandoffTracker(mode="shadow")
    proc_claude.process_line(json.dumps({
        "type": "system",
        "subtype": "init",
        "session_id": "s-live-1",
        "model": "claude-opus-5",
        "contextWindow": 200_000,
    }))
    assert proc_claude.context_window == 200_000
    proc_claude.process_line(json.dumps({
        "type": "assistant",
        "parent_tool_use_id": None,
        "session_id": "s-live-1",
        "message": {
            "id": "m-1",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 160_000, "output_tokens": 5_000},
            "content": [{"type": "text", "text": "working"}],
        },
    }))
    live_occ = proc_claude.live_context_occupancy(cli="claude")
    assert live_occ["evidence"] == "reported"
    assert live_occ["ratio"] == 0.8

    # Also verify terminal upgrade when live checkpoint had no window yet
    proc_late = StreamProcessor(cli="claude")
    proc_late.context_handoff_tracker = _context_handoff.ContextHandoffTracker(mode="shadow")
    proc_late.last_request_usage = {"input_tokens": 160_000, "output_tokens": 5_000}
    # Live checkpoint runs before terminal result supplies contextWindow
    first_dec = proc_late.context_handoff_tracker.evaluate(proc_late.live_context_occupancy(cli="claude"))
    assert first_dec["evidence"] == "occupied_without_window"
    # Terminal result arrives with modelUsage[...].contextWindow
    proc_late.context_window = 200_000
    resp_late = {"status": "success", "cli": "claude", "exit_code": 0, "result": "done"}
    _executor._enrich(resp_late, proc_late)
    assert resp_late["context_handoff"]["evidence"] == "reported"
    assert resp_late["context_handoff"]["triggered"] is True
    assert resp_late["context_handoff"]["ratio"] == 0.8

    # Codex live token_count event with model_context_window and last_token_usage
    proc_codex = StreamProcessor(cli="codex")
    proc_codex.process_line(json.dumps({"type": "thread.started", "thread_id": "t-1"}))
    proc_codex.process_line(json.dumps({
        "type": "token_count",
        "info": {
            "model_context_window": 200_000,
            "last_token_usage": {"input_tokens": 150_000, "output_tokens": 2_000},
        },
    }))
    codex_occ = proc_codex.live_context_occupancy(cli="codex")
    assert codex_occ["evidence"] == "reported"
    assert codex_occ["ratio"] == 0.75

    # 2. mode="off" projection validation rejects arbitrary objects in ratio/headroom fields
    off_valid = _context_handoff.evaluate_context_handoff(
        {"evidence": "reported", "occupied_tokens": 150_000, "window_tokens": 200_000, "ratio": 0.75},
        mode="off",
    )
    assert _context_handoff.validate_projection(off_valid) is True
    for bad_field, bad_val in (
        ("ratio", {"raw_prompt": "secret"}),
        ("headroom_tokens", {"raw_prompt": "secret"}),
        ("headroom_ratio", "leaked"),
        ("triggered", True),
        ("reason", "within_context_headroom"),
    ):
        forged = dict(off_valid, **{bad_field: bad_val})
        assert _context_handoff.validate_projection(forged) is False

    # 3 & 4. reserve_request auto-consumes source result handoff_note and enforces headroom
    import hashlib
    import time
    from test_job_continuation import _fixture, _publish_result
    from test_job_resume import _synthetic_launch_observation
    import _job_continuation as continuation
    import _launch_qualification as qualification

    sub_dir = tmp_path / "auto_note"
    sub_dir.mkdir()
    root, job_id, job_file, invocation, args, result = _fixture(sub_dir)
    profile_dir = sub_dir / "profile"
    profile_dir.mkdir()
    from dataclasses import replace
    invocation = replace(
        invocation, profile="review-profile",
        profile_env={"CLAUDE_CONFIG_DIR": str(profile_dir.resolve())})
    result["profile"] = {
        "name": "review-profile", "cli": "claude",
        "path_sha256": hashlib.sha256(
            str(profile_dir.resolve()).encode("utf-8")).hexdigest()[:32],
        "registry_sha256": "a" * 32, "command_sha256": None,
    }
    result["report"] = {
        "status": "PARTIAL",
        "summary": "Completed step 1",
        "findings": "Need step 2",
        "follow_up": "Run step 2",
        "handoff": "Continue from step 1",
        "left_behind": "none",
    }
    result["context_occupancy"] = {
        "schema": "summon.context-occupancy/v1",
        "evidence": "reported",
        "occupied_tokens": 160_000,
        "window_tokens": 200_000,
        "ratio": 0.80,
    }
    result["_private_launch_observation"] = _synthetic_launch_observation()
    continuation.write_private_source(job_file, result, invocation, args)
    record = _jobs.read_json(_jobs.record_path(root, job_id))
    obs = result["_private_launch_observation"]
    qualified = qualification.issue(
        source_job_id=job_id, source_attempt_id=record["attempt_id"],
        operation="resume", backend=obs["backend"], transport=obs["transport"],
        registry_generation=obs["registry_generation"], registry_digest=obs["registry_digest"],
        adapter=obs["adapter"], adapter_version=obs["adapter_version"],
        external_cli_version=obs["external_cli_version"],
        material_contract="summon-claude-subprocess-material/v1",
        executable_sha256=obs["executable_sha256"],
        launch_material_sha256=obs["launch_material_sha256"],
        expires_at=time.time() + 3600,
        revocation_id=qualification.revocation_id_for({
            "operation": "resume", "backend": obs["backend"],
            "transport": obs["transport"],
            "registry_generation": obs["registry_generation"],
            "registry_digest": obs["registry_digest"],
            "adapter": obs["adapter"], "adapter_version": obs["adapter_version"],
            "external_cli_version": obs["external_cli_version"],
            "material_contract": "summon-claude-subprocess-material/v1",
            "executable_sha256": obs["executable_sha256"],
            "launch_material_sha256": obs["launch_material_sha256"],
        }), source_nonce=record["nonce"])
    qualification.write(root, job_id, qualified)
    _publish_result(job_file, result)

    # In enforce mode, exhausted headroom refuses same-session resume
    monkeypatch.setenv("SUMMON_CONTEXT_HANDOFF", "enforce")
    with pytest.raises(_job_resume.ResumeError) as exc_info:
        _job_resume.reserve_request(root, job_id, message="continue")
    assert exc_info.value.kind == "resume_context_headroom_exhausted"

    # In shadow mode, resume succeeds and auto-binds the structured handoff note from source result
    monkeypatch.setenv("SUMMON_CONTEXT_HANDOFF", "shadow")
    res_auto = _job_resume.reserve_request(root, job_id, message="continue")
    assert "Structured context handoff:\n" in res_auto.prompt
    assert "Prior handoff state: partial" in res_auto.prompt
    assert "Completed step 1" in res_auto.prompt

    # 5. Quoted token assignments are redacted in handoff notes
    quoted_note = _context_handoff.normalize_handoff_note({
        "state": "partial",
        "completed": ['Used token="abcdef0123456789abcdef" and api_key=\'secret987654321\''],
    })
    assert quoted_note is not None
    assert "abcdef0123456789abcdef" not in quoted_note["completed"][0]
    assert "secret987654321" not in quoted_note["completed"][0]
    assert "[REDACTED]" in quoted_note["completed"][0]

    # 6. Handoff attention persists across subsequent active checkpoints in enforce mode
    ctrl_dir = str(tmp_path / "jobs_ctrl")
    c_job_id = "d" * 32
    c_nonce = "nonce-handoff-persist-001"
    _write_launch_record(ctrl_dir, c_job_id, c_nonce)
    clock = ManualClock(1000.0)
    rc = _job_control.RuntimeControl(
        path=_job_control.control_path(ctrl_dir, c_job_id),
        heartbeat=_job_control.heartbeat_path(ctrl_dir, c_job_id),
        job_id=c_job_id,
        nonce=c_nonce,
        checkpoint_ms=60_000,
        max_runtime_ms=300_000,
        job_started_at=1000.0,
        clock=clock,
        wall_clock=clock,
    )
    tracker = _context_handoff.ContextHandoffTracker(mode="enforce")
    occ_high = {"evidence": "reported", "occupied_tokens": 160_000, "window_tokens": 200_000, "ratio": 0.80}
    clock.advance(61.0)
    rc.checkpoint(active=True)
    rc.record_context_handoff(tracker.evaluate(occ_high))
    assert rc.attention_required is True
    # Next checkpoint is also active; attention_required must remain True while handoff is enforced
    clock.advance(61.0)
    rc.checkpoint(active=True)
    rc.record_context_handoff(tracker.evaluate(occ_high))
    assert rc.attention_required is True

    # 7. Round-2 regressions: punctuation inside quoted/unquoted tokens, cumulative codex usage,
    # files_touched receipt binding, and second-read result_binding_sha256 verification.
    punct_note = _context_handoff.normalize_handoff_note({
        "state": "partial",
        "completed": [
            'token="abcd!efgh123456" and secret=\'abcdef!ghijk1234\' and password=unquoted!secret99',
        ],
    })
    assert punct_note is not None
    assert "abcd!efgh123456" not in punct_note["completed"][0]
    assert "ghijk1234" not in punct_note["completed"][0]
    assert "unquoted!secret99" not in punct_note["completed"][0]

    # Codex cumulative total_token_usage without last_token_usage does NOT populate last_request_usage
    proc_codex_cum = StreamProcessor(cli="codex")
    proc_codex_cum.process_line(json.dumps({"type": "thread.started", "thread_id": "t-cum"}))
    proc_codex_cum.process_line(json.dumps({
        "type": "token_count",
        "info": {
            "model_context_window": 200_000,
            "total_token_usage": {"input_tokens": 160_000, "output_tokens": 5_000},
        },
    }))
    assert proc_codex_cum.last_request_usage is None
    assert proc_codex_cum.live_context_occupancy(cli="codex")["evidence"] == "absent"

    # files_touched is bound in result_binding_sha256
    r_ft_1 = dict(result, files_touched=["skills/summon/scripts/_context_handoff.py"])
    r_ft_2 = dict(result, files_touched=["skills/summon/scripts/_job_resume.py"])
    assert continuation.result_binding_sha256(r_ft_1) != continuation.result_binding_sha256(r_ft_2)

    # Snapshot swap between read_private_source and _read_strict_json in reserve_request fails closed
    real_read_strict_json = _job_resume._read_strict_json

    def _tampered_second_read(path, *a, **kw):
        doc, st = real_read_strict_json(path, *a, **kw)
        if isinstance(doc, dict) and os.path.normcase(str(path)) == os.path.normcase(str(job_file)):
            swapped = dict(doc)
            swapped["report"] = dict(doc.get("report") or {}, summary="INJECTED SUMMARY")
            return swapped, st
        return doc, st

    with monkeypatch.context() as m:
        m.setattr(_job_resume, "_read_strict_json", _tampered_second_read)
        with pytest.raises(_job_resume.ResumeError) as swap_exc:
            _job_resume.reserve_request(root, job_id, message="continue")
        assert swap_exc.value.kind == "source_result_changed"

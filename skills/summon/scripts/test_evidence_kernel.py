"""Provider-inert M1/M2 evidence, decision, and liveness contracts."""

from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

import _decision
import _evidence
import _executor
from _liveness import LivenessError, LivenessTracker


EMPTY_LIVENESS_COUNTS = {
    "trusted": 0, "meaningful": 0, "ignored": 0, "untrusted": 0,
    "duplicates": 0, "reordered": 0, "reconnects": 0, "tools": 0,
}


def test_canonical_digest_binds_schema_and_mapping_order():
    a = _evidence.digest("summon.evidence/v1", {"b": 2, "a": 1})
    b = _evidence.digest("summon.evidence/v1", {"a": 1, "b": 2})
    assert a == b
    liveness = {"phase": "startup", "elapsed_ms": 0,
                "expired": None, "counts": dict(EMPTY_LIVENESS_COUNTS)}
    assert (_evidence.digest("summon.evidence/v1", liveness)
            != _evidence.digest("summon.liveness/v1", liveness))


@pytest.mark.parametrize("raw", [
    '{"a":1,"a":2}', '{"x":NaN}', '{"x":Infinity}',
])
def test_evidence_parser_rejects_ambiguous_json(raw):
    with pytest.raises(_evidence.EvidenceError):
        _evidence.loads(raw)


def test_evidence_rejects_unknown_schema_and_unbounded_numbers():
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.evidence/v999", {})
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.evidence/v1", {"x": 1 << 64})
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.evidence/v1", {"x": math.inf})
    with pytest.raises(_evidence.EvidenceError, match="binary floats"):
        _evidence.digest("summon.evidence/v1", {"x": 1.0})
    with pytest.raises(_evidence.EvidenceError, match="Unicode"):
        _evidence.digest("summon.evidence/v1", {"x": "\ud800"})


def test_evidence_seal_verify_and_forgery_rejection():
    sealed = _evidence.seal("summon.evidence/v1", {"value": 1})
    assert _evidence.verify(sealed) == {"value": 1}
    with pytest.raises(_evidence.EvidenceError, match="seal fields"):
        _evidence.seal("summon.evidence/v1",
                       {"schema": "summon.decision/v1", "value": 1})
    forged = dict(sealed, value=2)
    with pytest.raises(_evidence.EvidenceError, match="mismatch"):
        _evidence.verify(forged)


def test_unimplemented_evidence_schemas_cannot_be_digested():
    for schema in ("summon.fleet/v1", "summon.context/v1", "summon.projection/v0"):
        with pytest.raises(_evidence.EvidenceError, match="unknown"):
            _evidence.digest(schema, {})


def test_decision_exact_agent_and_spend_constraints_win():
    result = _decision.decide(
        request={"agent": "sol-review", "lane": None, "model": "gpt-5.6-sol"},
        candidates=[
            {"seat": "cheap", "backend": "codex", "model": "gpt-5.6-luna",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True},
            {"seat": "sol-review", "backend": "codex", "model": "gpt-5.6-sol",
             "permission": "read-only", "priority": 1, "requires_spend": True,
             "gate_allowed": True, "data_boundary_satisfied": True},
        ],
        constraints={"permission_ceiling": "read-only", "spend_authorized": False,
                     "enforcement": "enforced", "unknowns": ["usage"],
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False},
    )
    assert result["provider_contacted"] is False
    assert result["resolution"]["seat"] is None
    reasons = {item["seat"]: item["losing_rules"] for item in result["candidates"]}
    assert reasons["cheap"] == ["agent_mismatch", "exact_model_mismatch"]
    assert reasons["sol-review"] == ["paid_route_not_authorized"]
    assert len(result["decision_sha256"]) == 64
    assert result["digests"] == {"roster": None, "policy": None,
                                 "project": None, "approval": None}


def test_decision_exact_agent_permission_and_gate_fail_closed():
    attacker = _decision.decide(
        request={"agent": "wanted", "lane": None, "model": "frontier"},
        candidates=[{"seat": "attacker", "backend": "codex", "model": "frontier",
                     "permission": "read-only", "priority": -1,
                     "gate_allowed": True, "data_boundary_satisfied": True}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False},
    )
    assert attacker["resolution"]["seat"] is None
    assert attacker["candidates"][0]["losing_rules"] == ["agent_mismatch"]
    yolo = _decision.decide(
        request={"agent": "wanted", "lane": None, "model": "frontier"},
        candidates=[{"seat": "wanted", "backend": "codex", "model": "frontier",
                     "permission": "yolo", "priority": 0,
                     "gate_allowed": True, "data_boundary_satisfied": True}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False},
    )
    assert yolo["candidates"][0]["losing_rules"] == ["permission_ceiling_exceeded"]
    gated = _decision.decide(
        request={"agent": "wanted", "lane": None, "model": "frontier"},
        candidates=[{"seat": "wanted", "backend": "codex", "model": "frontier",
                     "permission": "read-only", "priority": 1,
                     "reasons": ["gate_denied"], "gate_allowed": True,
                     "data_boundary_satisfied": True}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False},
    )
    assert gated["candidates"][0]["losing_rules"] == ["gate_denied"]


def test_decision_rejects_mixed_priority_types():
    with pytest.raises(_evidence.EvidenceError, match="priority"):
        _decision.decide(
            request={"agent": "wanted", "lane": None, "model": None},
            candidates=[{"seat": "wanted", "backend": "codex", "model": None,
                         "permission": "read-only", "priority": "first",
                         "gate_allowed": True, "data_boundary_satisfied": True}],
            constraints={"permission_ceiling": "read-only",
                         "spend_authorized": True, "enforcement": "enforced",
                         "corrective_allowed": False, "retry_allowed": False,
                         "fallback_allowed": False},
        )


def test_decision_rejects_typos_invalid_permissions_and_fail_open_retry():
    base_candidate = {"seat": "wanted", "backend": "codex", "provider": "codex",
                      "model": "frontier", "permission": "read-only", "priority": 0,
                      "gate_allowed": True, "data_boundary_satisfied": True,
                      "requires_retry": True}
    base_constraints = {"permission_ceiling": None, "spend_authorized": True,
                        "enforcement": "enforced", "corrective_allowed": False,
                        "retry_allowed": False, "fallback_allowed": False}
    with pytest.raises(_evidence.EvidenceError, match="unknown fields"):
        _decision.decide(request={"agent": "wanted", "lane": None,
                                  "gate_allowd": False},
                         candidates=[base_candidate], constraints=base_constraints)
    with pytest.raises(_evidence.EvidenceError, match="permission"):
        _decision.decide(request={"agent": "wanted", "lane": None},
                         candidates=[dict(base_candidate, permission="root")],
                         constraints=base_constraints)
    result = _decision.decide(request={"agent": "wanted", "lane": None},
                              candidates=[base_candidate], constraints=base_constraints)
    assert result["resolution"]["seat"] is None
    assert "retry_not_allowed" in result["candidates"][0]["losing_rules"]


def test_schema_validation_rejects_unknown_and_malformed_nested_fields():
    malformed = {"provider_contacted": False, "request": {}, "resolution": {},
                 "authority": {}, "candidates": ["bad"], "unknowns": [],
                 "digests": {}, "raw_prompt": "private"}
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.decision/v1", malformed)
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.liveness/v1",
                         {"phase": [], "elapsed_ms": "0", "expired": {},
                          "counts": {}})


@pytest.mark.parametrize(("field", "requested_value"), [
    ("agent", []), ("provider", 7), ("model", "/etc/passwd"),
])
def test_decision_schema_rejects_individual_malformed_request_values(field, requested_value):
    body = _decision.decide(
        request={"agent": "wanted", "lane": None},
        candidates=[{"seat": "wanted", "backend": "codex", "provider": "codex",
                     "model": "frontier", "permission": "read-only", "priority": 0,
                     "gate_allowed": True, "data_boundary_satisfied": True}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False})
    payload = {key: item for key, item in body.items()
               if key not in {"schema", "decision_sha256"}}
    payload["request"][field] = requested_value
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.decision/v1", payload)


@pytest.mark.parametrize("mutation", [
    {"phase": "bogus"}, {"expired": "anything"},
    {"first_trusted_event_ms": []},
])
def test_liveness_schema_rejects_individual_malformed_values(mutation):
    payload = {"phase": "startup", "elapsed_ms": 0, "expired": None,
               "counts": dict(EMPTY_LIVENESS_COUNTS), "first_trusted_event_ms": None,
               "last_meaningful_event_ms": None}
    payload.update(mutation)
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.liveness/v1", payload)


def test_decision_kernel_has_no_dispatch_capability_imports():
    source = Path(_decision.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {"__future__", "re", "typing", "_evidence"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= allowed
    forbidden_calls = {"open", "exec", "eval", "compile", "__import__", "input"}
    assert not {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in forbidden_calls
    }


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_liveness_only_trusted_meaningful_events_reset_idle_clock():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    emit = tracker.emitter()
    assert emit.emit("stream_event", source_event_id="1") is True
    clock.advance(1.5)
    assert emit.emit("reconnect", source_event_id="2") is False
    clock.advance(0.6)
    assert tracker.expired() == "generation_idle_timeout"
    assert tracker.snapshot()["counts"]["reconnects"] == 1


def test_liveness_rejects_forged_duplicate_and_zero_token_progress():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    emit = tracker.emitter()
    assert tracker.observe({"trusted": True, "kind": "output_text",
                            "attempt_id": "attempt", "output_tokens": 4}) is False
    assert emit.emit("output_text", source_event_id="2", output_tokens=0) is True
    assert emit.emit("output_text", source_event_id="2", output_tokens=8) is False
    snapshot = tracker.snapshot()
    assert snapshot["counts"]["untrusted"] == 1
    assert snapshot["counts"]["duplicates"] == 1
    assert snapshot["counts"]["meaningful"] == 0


def test_liveness_tool_activity_counts_and_resets_idle_clock():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    emit = tracker.emitter()
    assert emit.emit("tool_activity", source_event_id="1",
                     tool_id="shell", progress=1) is True
    clock.advance(1.5)
    assert tracker.expired() is None
    snapshot = tracker.snapshot()
    assert snapshot["counts"]["tools"] == 1
    assert snapshot["counts"]["meaningful"] == 1


def test_liveness_virtual_time_startup_terminal_and_clock_regression():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    clock.advance(1.0)
    assert tracker.expired() == "startup_timeout"
    emit = tracker.emitter()
    assert emit.emit("terminal", source_event_id="1") is False
    assert tracker.expired() == "startup_timeout"
    clock.value = -1
    with pytest.raises(LivenessError):
        tracker.snapshot()


def test_liveness_terminal_is_absorbing_and_bool_tokens_are_not_progress():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    emit = tracker.emitter()
    assert emit.emit("output_text", source_event_id="1", output_tokens=True) is True
    assert tracker.snapshot()["counts"]["meaningful"] == 0
    assert emit.emit("terminal", source_event_id="2") is False
    assert tracker.snapshot()["phase"] == "terminal"
    assert emit.emit("output_text", source_event_id="3", output_chars=10) is False
    assert tracker.snapshot()["phase"] == "terminal"


def test_liveness_session_binding_and_authenticated_rotation():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", session_id="s1",
                              overall_ms=10_000, first_event_ms=1_000,
                              idle_ms=2_000, clock=clock)
    emit = tracker.emitter()
    assert emit.emit("stream_event", source_event_id="1") is False
    assert emit.emit("stream_event", session_id="s1", source_event_id="2") is True
    assert emit.reconnect(old_session_id="s1", new_session_id="s2",
                          source_event_id="3") is True
    assert emit.emit("output_text", session_id="s1", source_event_id="4",
                     output_chars=1) is False
    assert emit.emit("output_text", session_id="s2", source_event_id="5",
                     output_chars=1) is True


def test_liveness_finalization_and_next_deadline_are_deterministic():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="attempt", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=4_000,
                              finalization_ms=500, clock=clock)
    emit = tracker.emitter()
    assert emit.emit("stream_event", source_event_id="1") is True
    assert emit.emit("finalizing", source_event_id="2") is False
    assert tracker.next_deadline_ms() == 500
    clock.advance(0.5)
    assert tracker.expired() == "finalization_timeout"
    assert emit.emit("terminal", source_event_id="3") is False


def test_executor_stream_integration_projects_meaningful_liveness():
    events = [
        {"type": "thread.started", "thread_id": "session"},
        {"type": "item.completed",
         "item": {"type": "agent_message", "text": "STATUS: DONE"}},
        {"type": "turn.completed", "usage": {"output_tokens": 3}},
    ]
    program = "import json\n" + "\n".join(
        f"print(json.dumps({event!r}), flush=True)" for event in events)
    process = subprocess.Popen(
        [sys.executable, "-c", program], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8")
    response = _executor._drive_process(
        process, "codex", 2_000, parse_stream=True,
        attempt_id="a" * 32, first_event_ms=500, idle_ms=500)
    assert response["liveness"]["phase"] == "terminal"
    assert response["liveness"]["counts"]["meaningful"] == 1
    assert response["liveness"]["counts"]["trusted"] >= 3


def test_executor_startup_liveness_timeout_uses_existing_teardown():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8")
    response = _executor._drive_process(
        process, "codex", 2_000, parse_stream=True,
        attempt_id="b" * 32, first_event_ms=50, idle_ms=500)
    assert response["status"] in {"partial", "error"}
    assert response["timeout"]["stage"] == "startup_timeout"
    assert response["liveness"]["phase"] == "timed_out"
    assert process.poll() is not None

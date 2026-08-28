"""Provider-inert M1/M2 evidence, decision, and liveness contracts."""

from __future__ import annotations

import ast
import copy
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

import _builder
import _decision
import _evidence
import _executor
from _liveness import LivenessError, LivenessTracker
from _stream import StreamProcessor


EMPTY_LIVENESS_COUNTS = {
    "trusted": 0, "meaningful": 0, "ignored": 0, "untrusted": 0,
    "duplicates": 0, "reordered": 0, "reconnects": 0, "tools": 0,
}


def test_canonical_digest_binds_schema_and_mapping_order():
    a = _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": {"b": 2, "a": 1}})
    b = _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": {"a": 1, "b": 2}})
    assert a == b
    liveness = {"phase": "startup", "elapsed_ms": 0,
                "expired": None, "counts": dict(EMPTY_LIVENESS_COUNTS)}
    assert (_evidence.digest("summon.evidence/v1",
                             {"kind": "test", "value": liveness})
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
        _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": 1 << 64})
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": math.inf})
    with pytest.raises(_evidence.EvidenceError, match="binary floats"):
        _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": 1.0})
    with pytest.raises(_evidence.EvidenceError, match="Unicode"):
        _evidence.digest("summon.evidence/v1",
                         {"kind": "test", "value": "\ud800"})


def test_evidence_seal_verify_and_forgery_rejection():
    sealed = _evidence.seal("summon.evidence/v1",
                            {"kind": "test", "value": 1})
    assert _evidence.verify(sealed) == {"kind": "test", "value": 1}
    with pytest.raises(_evidence.EvidenceError, match="seal fields"):
        _evidence.seal("summon.evidence/v1",
                       {"schema": "summon.decision/v2",
                        "kind": "test", "value": 1})
    forged = dict(sealed, value=2)
    with pytest.raises(_evidence.EvidenceError, match="mismatch"):
        _evidence.verify(forged)


def test_unimplemented_evidence_schemas_cannot_be_digested():
    for schema in ("summon.context/v1", "summon.projection/v0"):
        with pytest.raises(_evidence.EvidenceError, match="unknown"):
            _evidence.digest(schema, {})


def test_decision_exact_agent_and_spend_constraints_win():
    result = _decision.decide(
        request={"agent": "sol-review", "lane": None, "model": "gpt-5.6-sol"},
        candidates=[
            {"seat": "cheap", "backend": "codex", "model": "gpt-5.6-luna",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False},
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
    assert len(result["sha256"]) == 64
    assert _evidence.verify(result)["resolution"]["seat"] is None
    assert result["digests"] == {"roster": None, "policy": None,
                                 "project": None, "approval": None}


def test_decision_exact_agent_permission_and_gate_fail_closed():
    attacker = _decision.decide(
        request={"agent": "wanted", "lane": None, "model": "frontier"},
        candidates=[{"seat": "attacker", "backend": "codex", "model": "frontier",
                     "permission": "read-only", "priority": -1,
                     "gate_allowed": True, "data_boundary_satisfied": True,
                     "requires_spend": False}],
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
                     "gate_allowed": True, "data_boundary_satisfied": True,
                     "requires_spend": False}],
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
                     "data_boundary_satisfied": True, "requires_spend": False}],
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
                         "gate_allowed": True, "data_boundary_satisfied": True,
                         "requires_spend": False}],
            constraints={"permission_ceiling": "read-only",
                         "spend_authorized": True, "enforcement": "enforced",
                         "corrective_allowed": False, "retry_allowed": False,
                         "fallback_allowed": False},
        )


def test_decision_requires_explicit_spend_classification():
    with pytest.raises(_evidence.EvidenceError, match="explicit boolean"):
        _decision.decide(
            request={"agent": "wanted", "lane": None},
            candidates=[{"seat": "wanted", "backend": "codex", "model": None,
                         "permission": "read-only", "priority": 0,
                         "gate_allowed": True, "data_boundary_satisfied": True}],
            constraints={"permission_ceiling": None, "spend_authorized": True,
                         "enforcement": "enforced", "corrective_allowed": False,
                         "retry_allowed": False, "fallback_allowed": False})


def test_decision_rejects_typos_invalid_permissions_and_fail_open_retry():
    base_candidate = {"seat": "wanted", "backend": "codex", "provider": "codex",
                      "model": "frontier", "permission": "read-only", "priority": 0,
                      "gate_allowed": True, "data_boundary_satisfied": True,
                      "requires_retry": True, "requires_spend": False}
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
        _evidence.digest("summon.decision/v2", malformed)
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
                     "gate_allowed": True, "data_boundary_satisfied": True,
                     "requires_spend": False}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False})
    payload = {key: item for key, item in body.items()
               if key not in {"schema", "sha256"}}
    payload["request"][field] = requested_value
    with pytest.raises(_evidence.EvidenceError):
        _evidence.digest("summon.decision/v2", payload)


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


def test_v2_generic_evidence_schema_is_exact_and_private_text_is_rejected():
    with pytest.raises(_evidence.EvidenceError, match="exactly"):
        _evidence.digest("summon.evidence/v2", {"value": 1})
    with pytest.raises(_evidence.EvidenceError, match="path-like"):
        _evidence.digest("summon.evidence/v2", {
            "kind": "test", "value": {"nested": ["C:\\private\\note.txt"]}})
    for private_text in (
            "see /home/private/data for details",
            "url file:///home/private",
            "cwd=C:\\private\\note.txt",
            "a,C:/private/note.txt",
            "model:C:\\private\\model",
            "[C:/private/note.txt]",
            "read \\\\server\\private\\data",
            "read //server/private/data"):
        with pytest.raises(_evidence.EvidenceError, match="path-like"):
            _evidence.digest("summon.evidence/v2", {
                "kind": "test", "value": {"nested": [private_text]}})
    assert _evidence.digest("summon.evidence/v2", {
        "kind": "test", "value": {"url": "https://example.com/public"}})


def test_evidence_v1_keeps_its_original_generic_canonical_contract():
    assert _evidence.digest("summon.evidence/v1", {
        "legacy": ["shape", 1], "no_wrapper_required": True})


def test_permission_enforcement_fails_closed_without_a_ceiling():
    result = _decision.decide(
        request={"agent": "wanted", "lane": None},
        candidates=[{"seat": "wanted", "backend": "agy", "model": "frontier",
                     "permission": "read-only", "priority": 0,
                     "gate_allowed": True, "data_boundary_satisfied": True,
                     "requires_spend": False}],
        constraints={"permission_ceiling": None, "spend_authorized": True,
                     "enforcement": "unenforceable", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False})
    assert result["resolution"]["seat"] is None
    assert result["candidates"][0]["losing_rules"] == [
        "permission_unenforceable"]


def test_argv_permission_mapping_alone_cannot_claim_enforcement(monkeypatch):
    monkeypatch.setitem(_builder._PERMISSION_MAPPING, "future-backend", {
        "read-only": ["--plan"], "safe-edit": [], "yolo": []})
    assert _builder.permission_enforcement(
        "future-backend", "read-only") == "unknown"


@pytest.mark.parametrize("backend", ["openai-compat", "arkcli"])
def test_text_only_transport_permission_is_enforced_without_local_tools(backend):
    result = _decision.decide(
        request={"agent": "text-seat", "lane": None},
        candidates=[{"seat": "text-seat", "backend": backend,
                     "model": "frontier", "permission": "read-only",
                     "priority": 0, "gate_allowed": True,
                     "data_boundary_satisfied": True,
                     "requires_spend": False}],
        constraints={"permission_ceiling": None, "spend_authorized": True,
                     "enforcement": _builder.permission_enforcement(
                         backend, "read-only"),
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    assert result["resolution"]["seat"] == "text-seat"
    assert result["candidates"][0]["losing_rules"] == []


def test_decision_verifier_rejects_forged_nonminimal_lane_winner():
    decision = _decision.decide(
        request={"agent": None, "lane": "review"},
        candidates=[
            {"seat": "a", "backend": "codex", "model": "frontier",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False},
            {"seat": "b", "backend": "claude", "model": "frontier",
             "permission": "read-only", "priority": 1,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only", "spend_authorized": True,
                     "enforcement": "enforced", "corrective_allowed": False,
                     "retry_allowed": False, "fallback_allowed": False})
    body = _evidence.verify(decision)
    body["resolution"] = {
        "seat": "b", "backend": "claude", "provider": None,
        "model_targeted": "frontier", "winning_rule": "approved_lane_priority",
        "source": "approved_lane"}
    with pytest.raises(_evidence.EvidenceError, match="deterministic winner"):
        _evidence.verify(_evidence.seal("summon.decision/v2", body))


def test_decision_verifier_binds_winning_rule_to_resolution_state():
    winner = _decision.decide(
        request={"agent": "a", "lane": None, "source": "explicit_agent"},
        candidates=[
            {"seat": "a", "backend": "codex", "model": "frontier",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    forged_winner = _evidence.verify(winner)
    forged_winner["resolution"]["winning_rule"] = "no_eligible_candidate"
    with pytest.raises(_evidence.EvidenceError, match="winning rule"):
        _evidence.verify(_evidence.seal("summon.decision/v2", forged_winner))

    no_winner = _decision.decide(
        request={"agent": "a", "lane": None, "source": "explicit_agent"},
        candidates=[
            {"seat": "a", "backend": "agy", "model": "frontier",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "unenforceable",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    forged_empty = _evidence.verify(no_winner)
    forged_empty["resolution"]["winning_rule"] = "exact_agent_preserved"
    with pytest.raises(_evidence.EvidenceError, match="winning rule"):
        _evidence.verify(_evidence.seal("summon.decision/v2", forged_empty))


def test_decision_source_matches_request_and_rejects_missing_source():
    role = _decision.decide(
        request={"agent": "review", "lane": None,
                 "resolved_agent": "reviewer", "source": "approved_role"},
        candidates=[
            {"seat": "reviewer", "backend": "codex", "model": "frontier",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    legacy = _evidence.verify(role)
    legacy["resolution"].pop("source")
    with pytest.raises(_evidence.EvidenceError, match="source"):
        _evidence.verify(_evidence.seal("summon.decision/v2", legacy))

    explicit = _decision.decide(
        request={"agent": "reviewer", "lane": None,
                 "source": "explicit_agent"},
        candidates=[
            {"seat": "reviewer", "backend": "codex", "model": "frontier",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    for source, rule in (
            ("approved_lane", "approved_lane_priority"),
            ("approved_role", "approved_role_resolved")):
        forged = copy.deepcopy(_evidence.verify(explicit))
        forged["resolution"].update({"source": source,
                                      "winning_rule": rule})
        with pytest.raises(_evidence.EvidenceError, match="source|role"):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))

    with pytest.raises(_evidence.EvidenceError, match="approved_lane"):
        _decision.decide(
            request={"agent": "reviewer", "lane": None,
                     "source": "approved_lane"},
            candidates=[
                {"seat": "reviewer", "backend": "codex",
                 "model": "frontier", "permission": "read-only",
                 "priority": 0, "gate_allowed": True,
                 "data_boundary_satisfied": True,
                 "requires_spend": False}],
            constraints={"permission_ceiling": "read-only",
                         "spend_authorized": True,
                         "enforcement": "enforced",
                         "corrective_allowed": False,
                         "retry_allowed": False,
                         "fallback_allowed": False})


def test_decision_verifier_binds_exact_request_fields_to_candidates():
    explicit = _decision.decide(
        request={"agent": "a", "lane": None, "model": "m",
                 "provider": "codex", "source": "explicit_agent"},
        candidates=[
            {"seat": "a", "backend": "codex", "provider": "codex",
             "model": "m", "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    mutations = (
        {"agent": "b"},
        {"resolved_agent": "b"},
        {"model": "other"},
        {"provider": "claude"},
    )
    for mutation in mutations:
        forged = copy.deepcopy(_evidence.verify(explicit))
        forged["request"].update(mutation)
        with pytest.raises(_evidence.EvidenceError, match="request binding"):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))


def test_decision_verifier_mirrors_visible_authority_and_canonical_sets():
    decision = _decision.decide(
        request={"agent": "a", "lane": None, "source": "explicit_agent"},
        candidates=[
            {"seat": "a", "backend": "codex", "model": "m",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False,
                     "unknowns": ["roster_digest", "policy_digest"]})

    permission = copy.deepcopy(_evidence.verify(decision))
    permission["candidates"][0]["permission"] = "yolo"
    with pytest.raises(_evidence.EvidenceError, match="permission ceiling"):
        _evidence.verify(_evidence.seal("summon.decision/v2", permission))

    for enforcement in ("unknown", "unenforceable"):
        forged = copy.deepcopy(_evidence.verify(decision))
        forged["authority"]["enforcement"] = enforcement
        with pytest.raises(_evidence.EvidenceError,
                           match="permission enforcement"):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))

    duplicate_rules = copy.deepcopy(_evidence.verify(decision))
    duplicate_rules["candidates"][0].update({
        "eligible": False,
        "losing_rules": ["candidate_ineligible", "candidate_ineligible"],
    })
    with pytest.raises(_evidence.EvidenceError, match="not canonical"):
        _evidence.verify(
            _evidence.seal("summon.decision/v2", duplicate_rules))

    duplicate_unknowns = copy.deepcopy(_evidence.verify(decision))
    duplicate_unknowns["unknowns"] = ["roster_digest", "roster_digest"]
    with pytest.raises(_evidence.EvidenceError, match="not canonical"):
        _evidence.verify(
            _evidence.seal("summon.decision/v2", duplicate_unknowns))

    effective = copy.deepcopy(_evidence.verify(decision))
    effective["authority"]["effective_permission"] = "yolo"
    with pytest.raises(_evidence.EvidenceError, match="effective permission"):
        _evidence.verify(_evidence.seal("summon.decision/v2", effective))

    precedence = copy.deepcopy(_evidence.verify(decision))
    precedence["resolution"]["precedence"] = ["approved_role", "exact_agent"]
    with pytest.raises(_evidence.EvidenceError, match="precedence"):
        _evidence.verify(_evidence.seal("summon.decision/v2", precedence))


def test_decision_verifier_recomputes_every_visible_constraint_rule():
    decision = _decision.decide(
        request={"agent": "a", "lane": None, "source": "explicit_agent"},
        candidates=[{
            "seat": "a", "backend": "codex", "model": "m",
            "permission": "read-only", "priority": 0,
            "gate_allowed": True, "data_boundary_satisfied": True,
            "requires_spend": True, "requires_corrective": True,
            "requires_retry": True, "requires_fallback": True,
            "freshness": "fresh",
        }],
        constraints={
            "permission_ceiling": "read-only", "spend_authorized": True,
            "enforcement": "enforced", "corrective_allowed": True,
            "retry_allowed": True, "fallback_allowed": True,
            "require_fresh": True,
        })
    base = _evidence.verify(decision)
    mutations = (
        ("spend_authorized", False),
        ("corrective_allowed", False),
        ("retry_allowed", False),
        ("fallback_allowed", False),
    )
    for field, value in mutations:
        forged = copy.deepcopy(base)
        forged["authority"][field] = value
        with pytest.raises(_evidence.EvidenceError,
                           match="losing rules contradict visible inputs"):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))

    stale = copy.deepcopy(base)
    stale["candidates"][0]["freshness"] = "stale"
    with pytest.raises(_evidence.EvidenceError,
                       match="losing rules contradict visible inputs"):
        _evidence.verify(_evidence.seal("summon.decision/v2", stale))

    gate = copy.deepcopy(base)
    gate["candidates"][0]["gate_allowed"] = False
    with pytest.raises(_evidence.EvidenceError,
                       match="losing rules contradict visible inputs"):
        _evidence.verify(_evidence.seal("summon.decision/v2", gate))

    boundary = copy.deepcopy(base)
    boundary["candidates"][0]["data_boundary_satisfied"] = False
    with pytest.raises(_evidence.EvidenceError,
                       match="losing rules contradict visible inputs"):
        _evidence.verify(_evidence.seal("summon.decision/v2", boundary))


def test_lane_request_rejects_pre_resolved_agent():
    with pytest.raises(_evidence.EvidenceError, match="cannot pre-resolve"):
        _decision.decide(
            request={"agent": None, "lane": "review",
                     "resolved_agent": "reviewer", "source": "approved_lane"},
            candidates=[{
                "seat": "reviewer", "backend": "codex", "model": "m",
                "permission": "read-only", "priority": 0,
                "gate_allowed": True, "data_boundary_satisfied": True,
                "requires_spend": False,
            }],
            constraints={
                "permission_ceiling": "read-only", "spend_authorized": False,
                "enforcement": "enforced", "corrective_allowed": False,
                "retry_allowed": False, "fallback_allowed": False,
            })

    lane = _decision.decide(
        request={"agent": None, "lane": "review", "source": "approved_lane"},
        candidates=[{
            "seat": "reviewer", "backend": "codex", "model": "m",
            "permission": "read-only", "priority": 0,
            "gate_allowed": True, "data_boundary_satisfied": True,
            "requires_spend": False,
        }],
        constraints={
            "permission_ceiling": "read-only", "spend_authorized": False,
            "enforcement": "enforced", "corrective_allowed": False,
            "retry_allowed": False, "fallback_allowed": False,
        })
    forged = copy.deepcopy(_evidence.verify(lane))
    forged["request"]["resolved_agent"] = "reviewer"
    with pytest.raises(_evidence.EvidenceError, match="cannot pre-resolve"):
        _evidence.verify(_evidence.seal("summon.decision/v2", forged))


def test_decision_verifier_binds_spend_and_usage_projections():
    decision = _decision.decide(
        request={"agent": "a", "lane": None, "source": "explicit_agent"},
        candidates=[
            {"seat": "a", "backend": "codex", "model": "m",
             "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": False, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    base = _evidence.verify(decision)
    base["authority"].update({
        "effective_permission": "read-only",
        "credit": {"authorized": False, "source": "none"},
        "payg": {"authorized": False, "source": "none"},
    })
    base["usage"] = {
        "state": "advisory_only", "reason": "exact_pin_preserved",
        "observations_considered": 1,
        "freshness": {"fresh": 1, "stale": 0},
        "dimensions": ["subscription_allowance"],
        "comparability": "unverified_semantics",
    }
    sealed = _evidence.seal("summon.decision/v2", base)
    assert _evidence.verify(sealed)["usage"]["observations_considered"] == 1

    mutations = []
    credit = copy.deepcopy(base)
    credit["authority"]["credit"] = {
        "authorized": True, "source": "dispatch_flag"}
    mutations.append((credit, "spend projection"))
    source = copy.deepcopy(base)
    source["authority"]["credit"]["source"] = "arbitrary"
    mutations.append((source, "credit"))
    totals = copy.deepcopy(base)
    totals["usage"]["freshness"] = {"fresh": 7, "stale": 9}
    mutations.append((totals, "freshness"))
    dimensions = copy.deepcopy(base)
    dimensions["usage"]["dimensions"] = [
        "subscription_allowance", "subscription_allowance"]
    mutations.append((dimensions, "dimensions"))
    comparability = copy.deepcopy(base)
    comparability["usage"]["comparability"] = "arbitrary"
    mutations.append((comparability, "comparability"))
    for forged, match in mutations:
        with pytest.raises(_evidence.EvidenceError, match=match):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))

    role = _decision.decide(
        request={"agent": "review", "lane": None,
                 "resolved_agent": "a", "source": "approved_role"},
        candidates=[
            {"seat": "a", "backend": "codex", "provider": "codex",
             "model": "m", "permission": "read-only", "priority": 0,
             "gate_allowed": True, "data_boundary_satisfied": True,
             "requires_spend": False}],
        constraints={"permission_ceiling": "read-only",
                     "spend_authorized": True, "enforcement": "enforced",
                     "corrective_allowed": False, "retry_allowed": False,
                     "fallback_allowed": False})
    for legacy in (False, True):
        forged = copy.deepcopy(_evidence.verify(role))
        forged["request"]["resolved_agent"] = "b"
        if legacy:
            forged["resolution"].pop("source")
        with pytest.raises(
                _evidence.EvidenceError,
                match="source" if legacy else "request binding"):
            _evidence.verify(_evidence.seal("summon.decision/v2", forged))


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


def test_stream_semantic_replay_dedupe_without_provider_event_ids():
    """No-id transport replays cannot renew idle, but changed work can."""
    clock = Clock()
    tracker = LivenessTracker(attempt_id="semantic-replay", overall_ms=10_000,
                              first_event_ms=1_000, idle_ms=2_000, clock=clock)
    processor = StreamProcessor(event_observer=tracker.emitter())
    processor.process_line(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "session"}))

    text = {"type": "assistant", "session_id": "session", "message": {
        "content": [{"type": "text", "text": "private first answer"}]}}
    processor.process_line(json.dumps(text))
    clock.advance(1.5)
    processor.process_line(json.dumps(text))
    progressed_text = {"type": "assistant", "session_id": "session", "message": {
        "content": [{"type": "text", "text": "private second answer"}]}}
    processor.process_line(json.dumps(progressed_text))
    # A stable outer event id with changed semantic content is incremental
    # progress; replaying the same id+content is not.
    stable = {"type": "assistant", "id": "stable-message",
              "session_id": "session", "message": {"content": [
                  {"type": "text", "text": "stable first"}]}}
    processor.process_line(json.dumps(stable))
    processor.process_line(json.dumps(stable))
    stable["message"]["content"][0]["text"] = "stable second"
    processor.process_line(json.dumps(stable))

    tool = {"type": "assistant", "session_id": "session", "message": {
        "content": [{"type": "tool_use", "name": "Read",
                     "input": {"path": "private-a.txt"}}]}}
    processor.process_line(json.dumps(tool))
    processor.process_line(json.dumps(tool))
    progressed_tool = {"type": "assistant", "session_id": "session", "message": {
        "content": [{"type": "tool_use", "name": "Read",
                     "input": {"path": "private-b.txt"}}]}}
    processor.process_line(json.dumps(progressed_tool))

    snapshot = tracker.snapshot()
    assert snapshot["counts"]["meaningful"] == 6
    assert snapshot["counts"]["tools"] == 2
    assert all(len(digest) == 64 for digest in processor._liveness_semantic_ids)
    cached = repr(processor._liveness_semantic_ids)
    assert "private first answer" not in cached
    assert "private-a.txt" not in cached

    clock.advance(2.1)
    assert tracker.expired() == "generation_idle_timeout"
    assert processor.process_line(json.dumps(
        {"type": "result", "result": "unchanged parsing", "status": "success"}))
    assert processor.get_result()["result"] == "unchanged parsing"


def test_stream_semantic_dedupe_saturates_instead_of_reaccepting_old_replay():
    """Churning the bounded cache cannot make event zero meaningful again."""
    clock = Clock()
    tracker = LivenessTracker(attempt_id="semantic-saturation", overall_ms=60_000,
                              first_event_ms=60_000, idle_ms=60_000, clock=clock)
    processor = StreamProcessor(event_observer=tracker.emitter())
    processor.process_line(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "session"}))
    for index in range(4097):
        processor.process_line(json.dumps({
            "type": "assistant", "session_id": "session",
            "message": {"content": [{"type": "text", "text": f"item-{index}"}]},
        }))
    before = tracker.snapshot()["counts"]["meaningful"]
    assert before == 4096
    processor.process_line(json.dumps({
        "type": "assistant", "session_id": "session",
        "message": {"content": [{"type": "text", "text": "item-0"}]},
    }))
    assert tracker.snapshot()["counts"]["meaningful"] == before
    assert len(processor._liveness_semantic_ids) == 4096


def test_replayed_finalizing_event_cannot_slide_finalization_deadline():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="finalizing-replay", overall_ms=60_000,
                              first_event_ms=60_000, idle_ms=60_000,
                              finalization_ms=2_000, clock=clock)
    emitter = tracker.emitter()
    emitter.emit("transport_started", session_id="session")
    emitter.emit("finalizing", session_id="session")
    for _ in range(5):
        clock.advance(0.4)
        emitter.emit("finalizing", session_id="session")
    assert tracker.expired() == "finalization_timeout"


def test_meaningful_progress_can_leave_then_reenter_finalization_once():
    clock = Clock()
    tracker = LivenessTracker(attempt_id="finalizing-progress", overall_ms=60_000,
                              first_event_ms=60_000, idle_ms=60_000,
                              finalization_ms=2_000, clock=clock)
    emitter = tracker.emitter()
    emitter.emit("transport_started", session_id="session")
    emitter.emit("finalizing", session_id="session")
    clock.advance(1.5)
    emitter.emit("output_text", session_id="session", source_event_id="new-text",
                 output_chars=5)
    clock.advance(1.0)
    assert tracker.expired() is None
    emitter.emit("finalizing", session_id="session", source_event_id="final-2")
    clock.advance(2.0)
    assert tracker.expired() == "finalization_timeout"


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
        process, "codex", 5_000, parse_stream=True,
        attempt_id="a" * 32, first_event_ms=3_000, idle_ms=3_000)
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

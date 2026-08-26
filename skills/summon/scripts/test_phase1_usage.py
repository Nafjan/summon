"""Provider-inert Phase 1 usage and effective-route contract tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import _apibackend
import _builder
import _cli
import _usage
import run_subagent
from _builder import AgentInvocation


def _observation(**updates):
    value = {
        "provider": "codex",
        "dimension": "subscription_allowance",
        "support": "supported",
        "source": "operator_export",
        "observed_at": "2026-08-26T09:00:00Z",
        "retrieved_at": "2026-08-26T09:00:01Z",
        "ttl_seconds": 3600,
        "latency_ms": 1000,
        "remaining": {"value": 0.75, "unit": "fraction"},
        "reset_at": "2026-08-27T09:00:00Z",
    }
    value.update(updates)
    return value


def test_usage_subcommands_are_provider_inert_modes():
    assert _cli.rewrite_subcommand(["usage", "status", "--json"]) == (
        ["--usage-action", "status", "--json"], None)
    assert _cli.rewrite_subcommand(
        ["usage", "import", "--from", "snapshot.json", "--cache", "cache.json"]
    ) == (["--usage-action", "import", "--usage-from", "snapshot.json",
           "--usage-cache", "cache.json"], None)


def test_usage_import_status_round_trip_is_redacted_and_cache_only(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation()],
    }), encoding="utf-8")

    imported = _usage.import_snapshot(str(source), cache_path=str(cache),
                                      now="2026-08-26T09:10:00Z")
    status = _usage.status(cache_path=str(cache), now="2026-08-26T09:10:00Z")

    assert imported["status"] == "success"
    assert imported["provider_contacted"] is False
    assert imported["imported"] == 1
    assert "source" not in imported
    assert status["provider_contacted"] is False
    assert status["cache_state"] == "current"
    assert status["observations"][0]["freshness"] == "fresh"
    assert status["observations"][0]["remaining"] == {
        "value": 0.75, "unit": "fraction"}
    assert str(tmp_path) not in json.dumps(status)


def test_usage_import_rejects_unknown_or_secret_bearing_fields(tmp_path):
    source = tmp_path / "source.json"
    for extra in (
        {"account_id": "private-account"},
        {"api_key": "fixture-secret-value"},
        {"raw_output": "provider dump"},
    ):
        source.write_text(json.dumps({
            "schema": "summon.usage/v1",
            "observations": [_observation(**extra)],
        }), encoding="utf-8")
        with pytest.raises(ValueError, match="unsupported observation field"):
            _usage.import_snapshot(str(source), cache_path=str(tmp_path / "cache.json"))


def test_usage_staleness_and_incomparability_are_explicit(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [
            _observation(ttl_seconds=60),
            _observation(provider="openrouter", dimension="api_balance",
                         remaining={"value": 10.0, "unit": "usd"}),
        ],
    }), encoding="utf-8")
    _usage.import_snapshot(str(source), cache_path=str(cache),
                           now="2026-08-26T09:00:02Z")
    report = _usage.status(cache_path=str(cache), now="2026-08-26T09:10:00Z")

    assert report["observations"][0]["freshness"] == "stale"
    comparison = _usage.compare_observations(*report["observations"])
    assert comparison == {
        "comparable": False,
        "reason": "category_incomparable",
    }


def test_missing_cache_is_honest_unknown_not_an_error(tmp_path):
    report = _usage.status(cache_path=str(tmp_path / "missing.json"))
    assert report["status"] == "success"
    assert report["cache_state"] == "missing"
    assert report["observations"] == []
    assert report["provider_contacted"] is False


def test_effective_decision_preserves_exact_agent_and_authority(monkeypatch):
    monkeypatch.delenv("SUMMON_ALLOW_CREDIT", raising=False)
    monkeypatch.delenv("SUMMON_ALLOW_FABLE", raising=False)
    monkeypatch.delenv("SUMMON_ALLOW_BYTEPLUS_PAYG", raising=False)
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda flag=False: bool(flag))
    invocation = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/packet", model="gpt-5.6-sol",
        permission="read-only", allow_payg=False,
    )
    args = SimpleNamespace(
        agent="sol-strategist", _resolved_agent="sol-strategist",
        _role_provenance={"source": "explicit"}, max_permission="read-only",
        allow_credit=False, allow_payg=False, strict_agents_dir=True,
    )
    decision = run_subagent._effective_decision_view(invocation, args)

    assert decision["schema"] == "summon.decision/v2"
    assert decision["provider_contacted"] is False
    assert decision["request"] == {
        "agent": "sol-strategist", "lane": None, "model": "gpt-5.6-sol",
        "provider": "codex"}
    assert decision["resolution"]["seat"] == "sol-strategist"
    assert decision["resolution"]["winning_rule"] == "exact_agent_preserved"
    assert decision["authority"]["permission_ceiling"] == "read-only"
    assert decision["authority"]["effective_permission"] == "read-only"
    assert decision["authority"]["enforcement"] == "enforced"
    assert decision["authority"]["unenforceable_authorized"] is False
    assert decision["authority"]["payg"] == {"authorized": False, "source": "none"}
    assert decision["usage"] == {
        "state": "not_consulted",
        "reason": "exact_pin_preserved",
    }


def test_credit_guard_and_decision_receipt_describe_same_effective_route(
        monkeypatch):
    monkeypatch.setattr(_builder, "_CREDIT_ONLY_MODELS", {"credit-model"})
    monkeypatch.delenv("SUMMON_ALLOW_CREDIT", raising=False)
    monkeypatch.delenv("SUMMON_ALLOW_FABLE", raising=False)
    monkeypatch.setattr(_apibackend, "payg_consent_allowed",
                        lambda flag=False: bool(flag))
    args = SimpleNamespace(
        agent="reviewer", _resolved_agent="reviewer", _role_provenance={},
        max_permission="read-only", allow_credit=False, allow_payg=False,
        strict_agents_dir=True, usage_cache=None, no_contract_repair=False,
        json_schema=None)
    requested = AgentInvocation(
        cli="claude", prompt="review", cwd="C:/packet",
        model="credit-model", permission="read-only")
    guarded, _, _ = _builder.apply_credit_guard(requested)
    decision = run_subagent._effective_decision_view(guarded, args)
    assert guarded.model == _builder._OPUS_FALLBACK
    assert decision["resolution"]["model_targeted"] == _builder._OPUS_FALLBACK
    assert decision["candidates"][0]["requires_spend"] is False
    assert decision["resolution"]["seat"] == "reviewer"

    monkeypatch.setenv("SUMMON_ALLOW_CREDIT", "1")
    authorized, _, _ = _builder.apply_credit_guard(requested)
    paid = run_subagent._effective_decision_view(authorized, args)
    assert paid["resolution"]["model_targeted"] == "credit-model"
    assert paid["candidates"][0]["requires_spend"] is True
    assert paid["authority"]["spend_authorized"] is True


def test_credit_only_name_on_api_transport_does_not_inherit_claude_policy(
        monkeypatch):
    monkeypatch.setattr(_builder, "_CREDIT_ONLY_MODELS", {"credit-model"})
    monkeypatch.setattr(_apibackend, "payg_consent_allowed",
                        lambda flag=False: bool(flag))
    invocation = AgentInvocation(
        cli="openai-compat", prompt="review", cwd="C:/packet",
        model="credit-model", permission="read-only", allow_payg=False)
    args = SimpleNamespace(
        agent="api-reviewer", _resolved_agent="api-reviewer",
        _role_provenance={}, max_permission="read-only", allow_credit=False,
        allow_payg=False, strict_agents_dir=True, usage_cache=None,
        no_contract_repair=False, json_schema=None)
    decision = run_subagent._effective_decision_view(invocation, args)
    assert decision["candidates"][0]["requires_spend"] is False
    assert decision["resolution"]["seat"] == "api-reviewer"


def test_dry_run_keeps_invalid_decision_evidence_private_without_false_refusal():
    invocation = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/private/packet",
        model="C:/private/model", permission="read-only",
        profile="token=abcdefghijklmnop",
        extra_args=("token=abcdefghijklmnop",))
    args = SimpleNamespace(
        agent="C:/private/seat", _resolved_agent="C:/private/seat",
        _role_provenance={"source": "explicit"}, max_permission="read-only",
        allow_credit=False, allow_payg=False, strict_agents_dir=True,
        timeout=1000, worktree=None, isolated_lane=False,
        allow_tool_credentials=False, read_root=None, usage_cache=None)
    view = run_subagent._dry_run_view(invocation, args, agents_dir=None)
    rendered = json.dumps(view)
    assert "would_refuse" not in view
    assert view["preview_incomplete"] is True
    assert view["decision_projection_available"] is False
    assert view["projection_error_kind"] == "decision_evidence_invalid"
    assert view["provider_contacted"] is False
    for private in ("C:/private/model", "C:/private/seat",
                    "C:/private/packet", "abcdefghijklmnop"):
        assert private not in rendered
    for normal_key in ("agent", "cli", "cwd", "permission", "timeout_ms",
                       "read_allowlist", "effective_decision"):
        assert normal_key in view


def test_invalid_projection_cannot_hide_independent_read_root_refusal():
    invocation = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/private/packet",
        model="C:/private/model", permission="read-only",
        read_roots=("C:/other-root",))
    args = SimpleNamespace(
        agent="C:/private/seat", _resolved_agent="C:/private/seat",
        _role_provenance={"source": "explicit"}, max_permission="read-only",
        allow_credit=False, allow_payg=False, strict_agents_dir=True,
        timeout=1000, worktree=None, isolated_lane=False,
        allow_tool_credentials=False, read_root=None, usage_cache=None)
    view = run_subagent._dry_run_view(invocation, args, agents_dir=None)
    rendered = json.dumps(view)
    assert view["preview_incomplete"] is True
    assert view["would_refuse"] is True
    assert view["error_kind"] == "read_allowlist_unsupported"
    assert view["read_allowlist"]["would_refuse"] is True
    for private in ("C:/private/model", "C:/private/seat",
                    "C:/private/packet", "C:/other-root"):
        assert private not in rendered


def test_invalid_projection_preserves_safe_routing_refusals():
    common = dict(
        _role_provenance={"source": "explicit"}, max_permission="read-only",
        allow_credit=False, allow_payg=False, strict_agents_dir=True,
        timeout=1000, worktree=None, isolated_lane=False,
        allow_tool_credentials=False, read_root=None, usage_cache=None)
    incompatible = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/packet",
        model="claude-opus-5/..", permission="read-only")
    incompatible_args = SimpleNamespace(
        agent="reviewer", _resolved_agent="reviewer", **common)
    view = run_subagent._dry_run_view(incompatible, incompatible_args, None)
    assert view["would_refuse"] is True
    assert view["error_kind"] == "backend_model_incompatible"
    assert "claude-opus-5/.." not in json.dumps(view)

    conflict = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/packet",
        model="gpt-5.6-sol", permission="read-only",
        extra_args=("--model", "gpt-5.6-luna"))
    conflict_args = SimpleNamespace(
        agent="C:/private/seat", _resolved_agent="C:/private/seat", **common)
    view = run_subagent._dry_run_view(conflict, conflict_args, None)
    assert view["would_refuse"] is True
    assert view["error_kind"] == "model_selection_conflict"
    assert "C:/private/seat" not in json.dumps(view)
    assert "gpt-5.6-luna" not in json.dumps(view)


def test_decision_receipt_matches_retry_and_fallback_authority(monkeypatch):
    base = dict(
        agent="reviewer", _resolved_agent="reviewer", _role_provenance={},
        max_permission="yolo", allow_credit=False, allow_payg=False,
        strict_agents_dir=True, usage_cache=None, no_contract_repair=False,
        json_schema=None, retries=0, transient_retries=False,
        no_acp_fallback=False, allow_kimi_acp_fallback=False)
    invocation = AgentInvocation(
        cli="gemini", prompt="review", cwd="C:/packet",
        model="gemini-3.7-flash-high", permission="yolo")
    args = SimpleNamespace(**base)
    decision = run_subagent._effective_decision_view(invocation, args)
    assert decision["authority"]["retry_allowed"] is False
    assert decision["authority"]["fallback_allowed"] is True
    readonly = AgentInvocation(
        cli="gemini", prompt="review", cwd="C:/packet",
        model="gemini-3.7-flash-high", permission="read-only")
    assert run_subagent._effective_decision_view(
        readonly, args)["authority"]["fallback_allowed"] is False

    args.retries = 1
    assert run_subagent._effective_decision_view(
        invocation, args)["authority"]["retry_allowed"] is True
    args.retries = 0
    args.transient_retries = True
    assert run_subagent._effective_decision_view(
        invocation, args)["authority"]["retry_allowed"] is True
    agy = AgentInvocation(
        cli="agy", prompt="review", cwd="C:/packet",
        model="gemini-3.7-flash-high", permission="yolo")
    args.transient_retries = False
    assert run_subagent._effective_decision_view(
        agy, args)["authority"]["retry_allowed"] is True

    monkeypatch.setenv("SUMMON_ACP_FALLBACK", "0")
    assert run_subagent._effective_decision_view(
        invocation, args)["authority"]["fallback_allowed"] is False
    monkeypatch.delenv("SUMMON_ACP_FALLBACK")
    kimi = AgentInvocation(
        cli="kimi", prompt="review", cwd="C:/packet",
        model="kimi-code/k3", permission="yolo")
    assert run_subagent._effective_decision_view(
        kimi, args)["authority"]["fallback_allowed"] is False
    args.allow_kimi_acp_fallback = True
    assert run_subagent._effective_decision_view(
        kimi, args)["authority"]["fallback_allowed"] is True


def test_agy_advisory_opt_in_is_explicit_not_mislabeled(monkeypatch):
    monkeypatch.setenv("SUMMON_ALLOW_UNENFORCED_READONLY", "1")
    invocation = AgentInvocation(
        cli="agy", prompt="review", cwd="C:/packet", model="gemini-3.7-flash-high",
        permission="read-only", permission_forced=False)
    args = SimpleNamespace(
        agent="researcher", _resolved_agent="researcher", _role_provenance={},
        max_permission=None, allow_credit=False, allow_payg=False,
        strict_agents_dir=True, usage_cache=None)
    decision = run_subagent._effective_decision_view(invocation, args)
    assert decision["resolution"]["seat"] == "researcher"
    assert decision["authority"]["enforcement"] == "unenforceable"
    assert decision["authority"]["unenforceable_authorized"] is True


def test_agy_explicit_safe_edit_is_typed_full_authority_but_forced_is_refused():
    explicit = AgentInvocation(
        cli="agy", prompt="work", cwd="C:/packet", model="gemini-3.7-flash-high",
        permission="safe-edit", permission_forced=False)
    args = SimpleNamespace(
        agent="researcher", _resolved_agent="researcher", _role_provenance={},
        max_permission=None, allow_credit=False, allow_payg=False,
        strict_agents_dir=True, usage_cache=None)
    decision = run_subagent._effective_decision_view(explicit, args)
    assert decision["resolution"]["seat"] == "researcher"
    assert decision["authority"]["enforcement"] == "unenforceable"
    assert decision["authority"]["unenforceable_authorized"] is True
    assert _builder.readonly_unenforceable_error(
        "agy", "safe-edit", forced=False) is None
    assert "FORCED" in _builder.readonly_unenforceable_error(
        "agy", "safe-edit", forced=True)


def test_usage_import_accepts_utf8_bom_and_crlf(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    payload = json.dumps({"schema": "summon.usage/v1",
                          "observations": [_observation()]}, indent=2)
    source.write_bytes(("\ufeff" + payload.replace("\n", "\r\n")).encode("utf-8"))
    assert _usage.import_snapshot(
        str(source), cache_path=str(cache), now="2026-08-26T09:10:00Z")["imported"] == 1


def test_usage_import_rejects_false_provider_source_and_preserves_old_cache(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    cache.write_text('{"sentinel":true}', encoding="utf-8")
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation(source="provider_endpoint")],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="operator_export"):
        _usage.import_snapshot(str(source), cache_path=str(cache))
    assert cache.read_text(encoding="utf-8") == '{"sentinel":true}'


def test_usage_status_rejects_forged_cache_attestation(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "cached_at": "2026-08-26T09:00:02Z",
        "observations": [_observation(source="provider_endpoint")],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="operator_export"):
        _usage.status(cache_path=str(cache), now="2026-08-26T09:10:00Z")


def test_atomic_replace_failure_is_fail_closed(monkeypatch, tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    old = b'{"existing":"private cache"}'
    cache.write_bytes(old)
    source.write_text(json.dumps({
        "schema": "summon.usage/v1", "observations": [_observation()]
    }), encoding="utf-8")

    def sharing_violation(src, dst):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(_usage.os, "replace", sharing_violation)
    with pytest.raises(ValueError, match="atomically"):
        _usage.import_snapshot(
            str(source), cache_path=str(cache), now="2026-08-26T09:10:00Z")
    assert cache.read_bytes() == old


def test_usage_status_explains_advisory_only_behavior(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation()],
    }), encoding="utf-8")
    _usage.import_snapshot(str(source), cache_path=str(cache),
                           now="2026-08-26T09:00:02Z")
    report = _usage.status(cache_path=str(cache), now="2026-08-26T09:10:00Z")
    assert report["selection_advice"] == {
        "routing_changed": False,
        "provider_contacted": False,
        "reason": "advisory_only_exact_requests_preserved",
        "fresh_observations": 1,
        "stale_observations": 0,
    }


def test_usage_rejects_nonfinite_numbers_and_duplicate_json_keys(tmp_path):
    cache = tmp_path / "cache.json"
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text(
        '{"schema":"summon.usage/v1","observations":['
        + json.dumps(_observation()).replace('0.75', 'NaN') + ']}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        _usage.import_snapshot(str(nonfinite), cache_path=str(cache))

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"schema":"summon.usage/v1","schema":"summon.usage/v1",'
        '"observations":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON field"):
        _usage.import_snapshot(str(duplicate), cache_path=str(cache))


def test_usage_rejects_huge_numbers_without_replacing_cache(tmp_path):
    source = tmp_path / "huge.json"
    cache = tmp_path / "cache.json"
    original = b'{"existing":"normalized cache"}'
    cache.write_bytes(original)
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation(
            dimension="api_balance",
            remaining={"value": 10 ** 400, "unit": "usd"})],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="supported range"):
        _usage.import_snapshot(str(source), cache_path=str(cache))
    assert cache.read_bytes() == original


def test_integer_count_boundaries_do_not_round_through_float():
    assert _usage._bounded_number(
        _usage.MAX_COUNT_VALUE - 1, "remaining.value",
        maximum=_usage.MAX_COUNT_VALUE, integer_only=True) == _usage.MAX_COUNT_VALUE - 1
    assert _usage._bounded_number(
        _usage.MAX_COUNT_VALUE, "remaining.value",
        maximum=_usage.MAX_COUNT_VALUE, integer_only=True) == _usage.MAX_COUNT_VALUE


def test_usage_count_units_require_exact_bounded_integers(tmp_path):
    source = tmp_path / "counts.json"
    for value in (1.5, _usage.MAX_COUNT_VALUE + 1):
        source.write_text(json.dumps({
            "schema": "summon.usage/v1",
            "observations": [_observation(
                remaining={"value": value, "unit": "tokens"})],
        }), encoding="utf-8")
        with pytest.raises(ValueError):
            _usage.import_snapshot(
                str(source), cache_path=str(tmp_path / "cache.json"))


def test_unknown_or_missing_values_are_never_comparable():
    assert _usage.compare_observations(
        {"provider": "codex", "dimension": "unknown", "freshness": "fresh"},
        {"provider": "codex", "dimension": "unknown", "freshness": "fresh"},
    ) == {"comparable": False, "reason": "value_unavailable"}


@pytest.mark.parametrize("updates", [
    {"dimension": "unknown", "remaining": {"value": 1, "unit": "unknown"}},
    {"support": "unsupported"},
    {"support": "unknown"},
])
def test_unknown_or_unsupported_observations_cannot_carry_remaining(tmp_path, updates):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1", "observations": [_observation(**updates)]
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="remaining requires support=supported"):
        _usage.import_snapshot(str(source), cache_path=str(tmp_path / "cache.json"))


def test_operator_exports_never_claim_semantic_comparability():
    left = dict(_observation(), freshness="fresh")
    right = dict(_observation(), freshness="fresh")
    assert _usage.compare_observations(left, right) == {
        "comparable": False, "reason": "unverified_semantics"}


@pytest.mark.parametrize("updates", [
    {"dimension": ["subscription_allowance"]},
    {"support": ["supported"]},
    {"source": ["operator_export"]},
    {"ttl_seconds": 1.5},
    {"latency_ms": 1.5},
    {"retrieved_at": "9999-12-31T23:59:59Z"},
])
def test_schema_mutations_are_bounded_value_errors(tmp_path, updates):
    source = tmp_path / "source.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1", "observations": [_observation(**updates)]
    }), encoding="utf-8")
    with pytest.raises(ValueError):
        _usage.import_snapshot(str(source), cache_path=str(tmp_path / "cache.json"))


def test_usage_import_accepts_small_clock_skew_but_rejects_far_future(tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation(
            observed_at="2026-08-26T09:04:00Z",
            retrieved_at="2026-08-26T09:04:30Z")],
    }), encoding="utf-8")
    assert _usage.import_snapshot(
        str(source), cache_path=str(cache), now="2026-08-26T09:00:00Z")["imported"] == 1

    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation(
            observed_at="2099-01-01T00:00:00Z",
            retrieved_at="2099-01-01T00:00:01Z",
            reset_at="2099-01-02T00:00:00Z")],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="future"):
        _usage.import_snapshot(
            str(source), cache_path=str(cache), now="2026-08-26T09:00:00Z")


def test_usage_status_revalidates_editable_cache_clock(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "cached_at": "2099-01-01T00:00:02Z",
        "observations": [_observation(
            observed_at="2099-01-01T00:00:00Z",
            retrieved_at="2099-01-01T00:00:01Z",
            reset_at="2099-01-02T00:00:00Z")],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="future"):
        _usage.status(cache_path=str(cache), now="2026-08-26T09:00:00Z")


def test_usage_clock_skew_boundary_does_not_compose(tmp_path):
    cache = tmp_path / "cache.json"

    def write_cache(cached_at, observed_at, retrieved_at):
        cache.write_text(json.dumps({
            "schema": "summon.usage/v1",
            "cached_at": cached_at,
            "observations": [_observation(
                observed_at=observed_at, retrieved_at=retrieved_at)],
        }), encoding="utf-8")

    write_cache("2026-08-26T09:05:00Z", "2026-08-26T09:04:59Z",
                "2026-08-26T09:05:00Z")
    assert _usage.status(
        cache_path=str(cache), now="2026-08-26T09:00:00Z")["cache_state"] == "current"

    write_cache("2026-08-26T09:05:00Z", "2026-08-26T09:05:00Z",
                "2026-08-26T09:05:01Z")
    with pytest.raises(ValueError, match="future"):
        _usage.status(cache_path=str(cache), now="2026-08-26T09:00:00Z")

    write_cache("2026-08-26T09:05:00Z", "2026-08-26T09:09:59Z",
                "2026-08-26T09:10:00Z")
    with pytest.raises(ValueError, match="future"):
        _usage.status(cache_path=str(cache), now="2026-08-26T09:00:00Z")


def test_effective_decision_explains_explicit_usage_cache_without_rerouting(
        monkeypatch, tmp_path):
    source = tmp_path / "source.json"
    cache = tmp_path / "cache.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1", "observations": [_observation()]
    }), encoding="utf-8")
    _usage.import_snapshot(str(source), cache_path=str(cache),
                           now="2026-08-26T09:00:02Z")
    monkeypatch.setattr(_usage, "_utc_now",
                        lambda: _usage._parse_time("2026-08-26T09:10:00Z", "now"))
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda flag=False: False)
    invocation = AgentInvocation(
        cli="codex", prompt="review", cwd="C:/packet", model="gpt-5.6-sol",
        permission="read-only", allow_payg=False,
    )
    args = SimpleNamespace(
        agent="sol-strategist", _resolved_agent="sol-strategist",
        _role_provenance={}, max_permission="read-only", allow_credit=False,
        strict_agents_dir=True, usage_cache=str(cache),
    )
    decision = run_subagent._effective_decision_view(invocation, args)
    assert decision["resolution"]["winning_rule"] == "exact_agent_preserved"
    assert decision["usage"] == {
        "state": "advisory_only",
        "reason": "exact_pin_preserved",
        "observations_considered": 1,
        "freshness": {"fresh": 1, "stale": 0},
        "dimensions": ["subscription_allowance"],
        "comparability": "unverified_semantics",
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_cmd_usage_status_preserves_provider_inert_json(tmp_path):
    wrapper = Path(__file__).with_name("summon.cmd")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper), "usage", "status",
         "--cache", str(tmp_path / "missing.json"), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["cache_state"] == "missing"
    assert report["provider_contacted"] is False


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_cmd_malformed_usage_import_is_bounded(tmp_path):
    wrapper = Path(__file__).with_name("summon.cmd")
    source = tmp_path / "malformed.json"
    source.write_text(json.dumps({
        "schema": "summon.usage/v1",
        "observations": [_observation(dimension=["not-a-string"])],
    }), encoding="utf-8")
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper), "usage", "import",
         "--from", str(source), "--cache", str(tmp_path / "cache.json"), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert completed.returncode == 1
    envelope = json.loads(completed.stdout)
    assert "unsupported usage dimension" in envelope["error"]
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert "Traceback" not in completed.stderr + completed.stdout

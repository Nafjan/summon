"""Provider-inert contract tests for durable deliberation context freshness."""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest
import _deliberation_context as context_module


SCRIPTS = pathlib.Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _deliberation_context import (  # noqa: E402
    ContextFreshnessError,
    acceptance_identity,
    bind_context,
    parse_context_packet,
    parse_private_projection,
    parse_private_projection_readonly,
    private_projection,
    prompt_projection,
    public_projection,
    revalidate_source,
)


NOW = 2_000_000
REVISION = "a" * 40
NEXT_REVISION = "b" * 40
SOURCE_DIGEST = "1" * 64
NEXT_DIGEST = "2" * 64
RUNS_ROOT_SHA = "3" * 64


def packet(*, captured_at: int = NOW - 100, revision: str = REVISION,
           digest: str = SOURCE_DIGEST, body: str = "Keep the public API stable.") -> dict:
    return {
        "schema": "summon.deliberation-context/v1",
        "source": {
            "kind": "git_commit",
            "revision": revision,
            "source_digest": digest,
            "captured_at_unix_ms": captured_at,
        },
        "freshness_policy": {
            "fresh_max_age_ms": 1_000,
            "hard_max_age_ms": 20_000,
            "hard_max_revision_delta": 3,
        },
        "entries": [
            {
                "id": "constraint-1",
                "kind": "constraint",
                "body": body,
                "provenance": {"kind": "authored", "source_sha256": "3" * 64},
            },
            {
                "id": "artifact-1",
                "kind": "artifact_reference",
                "artifact_ref": "sha256:" + "4" * 64,
                "provenance": {"kind": "authored", "source_sha256": "5" * 64},
            },
        ],
    }


def observation(*, relation: str = "same", revision: str = REVISION,
                digest: str = SOURCE_DIGEST, delta: int = 0,
                observed_at: int = NOW) -> dict:
    return {
        "schema": "summon.context-source-observation/v1",
        "kind": "git_commit",
        "captured_revision": REVISION,
        "current_revision": revision,
        "current_source_digest": digest,
        "relation": relation,
        "revision_delta": delta,
        "verification_method": "git-readback",
        "observed_at_unix_ms": observed_at,
    }


def intent(parsed, *, now: int = NOW, max_age: int = 10_000,
           max_delta: int = 2, entry_ids=("constraint-1", "artifact-1")) -> dict:
    return {
        "schema": "summon.accept-stale-intent/v2",
        "packet_sha256": parsed.packet_sha256,
        "source_revision_sha256": parsed.source_revision_sha256,
        "runs_root_sha256": RUNS_ROOT_SHA,
        "run_id": "run-1",
        "decision_id": "decision-1",
        "actor": {"kind": "human", "id": "operator"},
        "reason": "Reviewed the bounded source delta.",
        "scope": {"entry_ids": list(entry_ids), "use": "deliberation_prompt"},
        "expires_at_unix_ms": now + 5_000,
        "max_age_ms": max_age,
        "max_revision_delta": max_delta,
    }


def bind(p=None, o=None, stale=None, *, now=NOW):
    return bind_context(
        p or packet(), o or observation(), run_id="run-1",
        decision_id="decision-1", unix_now_ms=now,
        runs_root_sha256=RUNS_ROOT_SHA, accept_stale=stale)


def test_exact_source_is_fresh_and_has_no_routing_authority():
    value = bind()
    assert value.state == "fresh"
    assert value.actual_age_ms == 100
    assert value.actual_revision_delta == 0
    assert value.routing_authority is False
    assert value.mismatch_reasons == ()


def test_acceptance_run_and_decision_ids_match_run_directory_bound():
    parsed = parse_context_packet(packet())
    value = intent(parsed)
    value["run_id"] = "r" * 65
    with pytest.raises(ContextFreshnessError, match="acceptance run id"):
        acceptance_identity(value)
    value = intent(parsed)
    value["decision_id"] = "d" * 65
    with pytest.raises(ContextFreshnessError, match="acceptance decision id"):
        acceptance_identity(value)


def test_age_stale_requires_explicit_acceptance_and_remains_visibly_stale():
    p = packet(captured_at=NOW - 5_000)
    parsed = parse_context_packet(p)
    refused = bind(p, observation())
    assert refused.state == "stale_refused"
    accepted = bind(p, observation(), intent(parsed))
    assert accepted.state == "accepted_stale"
    assert accepted.actual_age_ms == 5_000
    assert "age_exceeds_fresh_limit" in accepted.mismatch_reasons
    assert prompt_projection(accepted)["freshness"]["state"] == "accepted_stale"
    assert public_projection(accepted)["state"] == "accepted_stale"


def test_stale_acceptance_cannot_be_replayed_in_a_different_runs_root():
    p = packet(captured_at=NOW - 5_000)
    auth = intent(parse_context_packet(p))
    replay = bind_context(
        p, observation(), run_id="run-1", decision_id="decision-1",
        unix_now_ms=NOW, runs_root_sha256="4" * 64, accept_stale=auth)
    assert replay.state == "stale_refused"


def test_forward_revision_delta_can_be_accepted_with_bounded_authority():
    parsed = parse_context_packet(packet())
    accepted = bind(
        o=observation(relation="descendant", revision=NEXT_REVISION,
                      digest=NEXT_DIGEST, delta=2), stale=intent(parsed))
    assert accepted.state == "accepted_stale"
    assert accepted.actual_revision_delta == 2
    assert "source_revision_advanced" in accepted.mismatch_reasons


@pytest.mark.parametrize("relation", ["diverged", "rewound", "unknown"])
def test_unbounded_revision_relations_refuse_even_with_intent(relation):
    parsed = parse_context_packet(packet())
    value = bind(o=observation(relation=relation, revision=NEXT_REVISION,
                               digest=NEXT_DIGEST, delta=0), stale=intent(parsed))
    assert value.state == "stale_refused"
    assert "revision_relation_unbounded" in value.mismatch_reasons


def test_same_revision_content_drift_cannot_be_accepted():
    parsed = parse_context_packet(packet())
    value = bind(o=observation(digest=NEXT_DIGEST), stale=intent(parsed))
    assert value.state == "stale_refused"
    assert "same_revision_digest_mismatch" in value.mismatch_reasons


@pytest.mark.parametrize("field,value", [
    ("packet_sha256", "f" * 64),
    ("source_revision_sha256", "e" * 64),
    ("run_id", "run-2"),
    ("decision_id", "decision-2"),
    ("expires_at_unix_ms", NOW),
    ("max_age_ms", 20_001),
    ("max_revision_delta", 4),
])
def test_forged_or_overbroad_acceptance_is_refused(field, value):
    p = packet(captured_at=NOW - 5_000)
    parsed = parse_context_packet(p)
    auth = intent(parsed)
    auth[field] = value
    result = bind(p, observation(), auth)
    assert result.state == "stale_refused"


def test_partial_or_unknown_acceptance_scope_is_refused():
    p = packet(captured_at=NOW - 5_000)
    parsed = parse_context_packet(p)
    assert bind(p, observation(), intent(parsed, entry_ids=("constraint-1",))).state == "stale_refused"
    assert bind(p, observation(), intent(parsed, entry_ids=("constraint-1", "missing"))).state == "stale_refused"


def test_intent_cannot_supply_actual_values_or_routing_fields():
    p = packet(captured_at=NOW - 5_000)
    parsed = parse_context_packet(p)
    for key in ("actual_age_ms", "actual_revision_delta", "model", "provider"):
        auth = intent(parsed)
        auth[key] = 0
        with pytest.raises(ContextFreshnessError, match="fields"):
            bind(p, observation(), auth)


def test_public_projection_omits_private_context_actor_reason_and_revision():
    p = packet(captured_at=NOW - 5_000, body="PRIVATE CONTEXT BODY")
    parsed = parse_context_packet(p)
    value = bind(p, observation(), intent(parsed))
    rendered = json.dumps(public_projection(value), sort_keys=True)
    for private in ("PRIVATE CONTEXT BODY", "operator", "Reviewed the bounded",
                    REVISION, NEXT_REVISION):
        assert private not in rendered
    assert public_projection(value)["affected_entry_count"] == 2


def test_prompt_projection_preserves_historical_ox_identity_without_relabeling():
    old = "Historical receipt served openrouter/stealth/ox-alpha."
    value = bind(packet(body=old), observation())
    rendered = json.dumps(prompt_projection(value), sort_keys=True)
    assert "openrouter/stealth/ox-alpha" in rendered
    assert "z-ai/glm-5.3-flash" not in rendered


def test_prelaunch_source_revalidation_detects_any_identity_drift():
    value = bind()
    assert revalidate_source(value, observation(), unix_now_ms=NOW) is True
    for changed in (
        observation(revision=NEXT_REVISION, relation="descendant", delta=1,
                    digest=NEXT_DIGEST),
        observation(digest=NEXT_DIGEST),
        observation(observed_at=NOW + 1, relation="unknown"),
    ):
        with pytest.raises(ContextFreshnessError, match="drift"):
            revalidate_source(value, changed, unix_now_ms=NOW)


def test_prelaunch_source_revalidation_requires_an_explicit_clock():
    with pytest.raises(TypeError, match="unix_now_ms"):
        revalidate_source(bind(), observation())


def test_private_projection_round_trips_and_detects_forgery():
    p = packet(captured_at=NOW - 5_000)
    value = bind(p, observation(), intent(parse_context_packet(p)))
    projected = private_projection(value)
    assert parse_private_projection(projected) == value
    for field, forged in (("state", "fresh"), ("actual_age_ms", 1),
                          ("binding_sha256", "f" * 64)):
        changed = copy.deepcopy(projected)
        changed[field] = forged
        with pytest.raises(ContextFreshnessError, match="binding"):
            parse_private_projection(changed)


def test_authenticated_v1_binding_is_readable_but_cannot_be_prompt_authority():
    current = private_projection(bind())
    legacy = copy.deepcopy(current)
    legacy["schema"] = context_module.LEGACY_BINDING_SCHEMA
    legacy.pop("runs_root_sha256")
    identity = {
        "schema": context_module.LEGACY_BINDING_SCHEMA,
        "packet_sha256": legacy["packet_sha256"],
        "source_revision_sha256": legacy["source_revision_sha256"],
        "run_id": legacy["run_id"], "decision_id": legacy["decision_id"],
        "bound_at_unix_ms": legacy["bound_at_unix_ms"],
        "state": legacy["state"], "actual_age_ms": legacy["actual_age_ms"],
        "actual_revision_delta": legacy["actual_revision_delta"],
        "mismatch_reasons": legacy["mismatch_reasons"],
        "observation_identity": {key: legacy["observation"][key] for key in (
            "kind", "captured_revision", "current_revision", "current_source_digest",
            "relation", "revision_delta", "verification_method")},
        "acceptance_sha256": None, "routing_authority": False,
    }
    legacy["binding_sha256"] = context_module._sha(identity)
    restored = parse_private_projection_readonly(legacy)
    assert public_projection(restored)["legacy_read_only"] is True
    with pytest.raises(ContextFreshnessError):
        parse_private_projection(legacy)
    with pytest.raises(ContextFreshnessError):
        prompt_projection(restored)
    forged = copy.deepcopy(legacy)
    forged["actual_age_ms"] += 1
    with pytest.raises(ContextFreshnessError, match="binding"):
        parse_private_projection_readonly(forged)


def test_prelaunch_revalidation_rechecks_expiry_and_age_authority():
    p = packet(captured_at=NOW - 5_000)
    value = bind(p, observation(), intent(parse_context_packet(p)))
    assert revalidate_source(value, observation(observed_at=NOW + 1),
                             unix_now_ms=NOW + 1) is True
    with pytest.raises(ContextFreshnessError, match="authority"):
        revalidate_source(value, observation(observed_at=NOW + 5_001),
                          unix_now_ms=NOW + 5_001)


def test_duplicate_json_keys_floats_and_nonfinite_values_are_typed_refusals():
    raw = json.dumps(packet(), separators=(",", ":"))
    with pytest.raises(ContextFreshnessError, match="duplicate"):
        parse_context_packet(raw.replace('"schema":', '"schema":"x","schema":', 1))
    with pytest.raises(ContextFreshnessError, match="floating"):
        parse_context_packet(raw.replace('"fresh_max_age_ms":1000', '"fresh_max_age_ms":1.0'))
    with pytest.raises(ContextFreshnessError):
        parse_context_packet(raw.replace('"fresh_max_age_ms":1000', '"fresh_max_age_ms":NaN'))


def test_packet_rejects_unknown_fields_duplicate_ids_and_invalid_artifact_refs():
    for mutation in ("unknown", "duplicate", "path", "url"):
        value = packet()
        if mutation == "unknown":
            value["provider"] = "openrouter"
        elif mutation == "duplicate":
            value["entries"][1]["id"] = "constraint-1"
        elif mutation == "path":
            value["entries"][1]["artifact_ref"] = "C:\\private\\file"
        else:
            value["entries"][1]["artifact_ref"] = "https://example.invalid/file"
        with pytest.raises(ContextFreshnessError):
            parse_context_packet(value)


def test_parser_deep_snapshots_mutable_input_and_digest_binds_body():
    original = packet()
    parsed = parse_context_packet(original)
    original["entries"][0]["body"] = "mutated"
    assert parsed.entries[0]["body"] == "Keep the public API stable."
    other = parse_context_packet(packet(body="Different constraint."))
    assert other.packet_sha256 != parsed.packet_sha256


def test_parser_rejects_lone_surrogates_and_excessive_depth():
    value = packet()
    value["entries"][0]["body"] = "\ud800"
    with pytest.raises(ContextFreshnessError):
        parse_context_packet(value)
    with pytest.raises(ContextFreshnessError):
        parse_context_packet("\ud800")
    nested = packet()
    body = {}
    cursor = body
    for _ in range(70):
        cursor["x"] = {}
        cursor = cursor["x"]
    nested["extra"] = body
    with pytest.raises(ContextFreshnessError):
        parse_context_packet(nested)


def test_observation_and_intent_are_deep_snapshotted():
    p = packet(captured_at=NOW - 5_000)
    parsed = parse_context_packet(p)
    obs = observation()
    auth = intent(parsed)
    value = bind(p, obs, auth)
    obs["current_source_digest"] = "f" * 64
    auth["reason"] = "changed"
    assert value.observation["current_source_digest"] == SOURCE_DIGEST
    assert value.acceptance["reason"] == "Reviewed the bounded source delta."


def test_boolean_integer_and_clock_rollback_are_refused():
    value = packet()
    value["freshness_policy"]["fresh_max_age_ms"] = True
    with pytest.raises(ContextFreshnessError):
        parse_context_packet(value)
    with pytest.raises(ContextFreshnessError, match="clock"):
        bind(packet(captured_at=NOW + 301_000), observation(), now=NOW)


def test_source_kind_and_verification_method_must_match_packet_contract():
    bad = observation()
    bad["kind"] = "revision_manifest"
    with pytest.raises(ContextFreshnessError, match="kind"):
        bind(o=bad)
    bad = observation()
    bad["verification_method"] = "self-asserted"
    with pytest.raises(ContextFreshnessError, match="verification"):
        bind(o=bad)


def test_copying_acceptance_between_packets_fails_closed():
    stale = packet(captured_at=NOW - 5_000)
    auth = intent(parse_context_packet(stale))
    changed = copy.deepcopy(stale)
    changed["entries"][0]["body"] = "Changed durable constraint."
    assert bind(changed, observation(), auth).state == "stale_refused"

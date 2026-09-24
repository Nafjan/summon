import pytest

import _context_policy as policy


def test_operator_policy_is_strict_and_has_stable_public_projection():
    value = policy.make("safe", policy_id="workspace-safe")
    assert value["implementation"] == "typed-context-compiler-v1"
    assert policy.public(value) == {
        "schema": policy.SCHEMA, "mode": "safe", "revision": 1,
        "origin": "operator",
    }
    assert len(policy.digest(value)) == 64


def test_policy_change_requires_a_newer_revision():
    first = policy.make("off", policy_id="context")
    assert policy.validate(first, previous=first) == first
    with pytest.raises(policy.ContextPolicyError, match="monotonic"):
        policy.validate({**first, "mode": "safe",
                         "implementation": "typed-context-compiler-v1"},
                        previous=first)
    changed = {**first, "mode": "safe", "revision": 2,
               "implementation": "typed-context-compiler-v1"}
    assert policy.validate(changed, previous=first) == changed


@pytest.mark.parametrize("mutator,kind", [
    (lambda row: row.update(extra=True), "fields"),
    (lambda row: row.update(mode="compact"), "unsupported"),
    (lambda row: row.update(mode="safe", implementation="whole-message-v1"), "mismatch"),
    (lambda row: row.update(revision=0), "revision"),
    (lambda row: row.update(origin="derived-compatibility"), "compatibility"),
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_malformed_or_unsupported_policy_is_rejected(mutator, kind):
    row = policy.make("off")
    mutator(row)
    with pytest.raises(policy.ContextPolicyError, match=kind):
        policy.validate(row)


def test_legacy_v1_is_explicitly_derived_and_cannot_be_used_as_operator_policy():
    legacy = policy.derived_legacy()
    assert policy.public(legacy) == {
        "schema": policy.SCHEMA, "mode": "off", "revision": 0,
        "origin": "derived-compatibility",
    }
    with pytest.raises(policy.ContextPolicyError, match="compatibility"):
        policy.validate(legacy)
    forged = dict(legacy, mode="safe", implementation="typed-context-compiler-v1")
    with pytest.raises(policy.ContextPolicyError):
        policy.public(forged)

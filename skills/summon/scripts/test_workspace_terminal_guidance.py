"""Synthetic projection/source checks; no browser or producer qualification."""
import copy
import json

import pytest

from test_workspace_view import lifecycle, render, scope, view
from _workspace_page import page_bytes


@pytest.mark.parametrize("state", ["rejected", "expired", "cancelled", "dead_lettered"])
@pytest.mark.parametrize("operator_text", [False, True])
def test_terminal_private_reason_is_unavailable_and_effects_are_unchanged(state, operator_text):
    fixture = lifecycle(state)
    delivery = next(iter(fixture.state["deliveries"].values()))
    secret = "private-account-token-abc123"
    delivery["reason"] = secret
    before = copy.deepcopy(fixture.state)
    result = render(fixture, scope=scope(audience="operator" if operator_text else "public", text=operator_text))
    row = result["deliveries"][0]
    assert row["label"] == view.DELIVERY_LABELS[state]
    assert row["reason"] == state + "_reason_unavailable"
    assert secret not in json.dumps(result)
    assert secret not in page_bytes(nonce="n" * 24).decode()
    assert row["certainty"] == delivery["certainty"]
    assert row["inherited_uncertainty"] == delivery["inherited_uncertainty"]
    assert row["acknowledgement"] == delivery["ack_level"]
    assert row["action_guidance"] == {"actions": [], "unavailable_reason":
        "no_delivery_action_in_current_scope" if operator_text else "read_only_export"}
    assert fixture.state == before


@pytest.mark.parametrize("state,reason,category", [
    ("cancelled", "cancel_queued_context", "queued_context_cancellation_recorded"),
    ("dead_lettered", "dispose_held_context", "held_context_disposition_recorded"),
    ("rejected", "cancel_queued_context", "rejected_reason_unavailable"),
    ("expired", "dispose_held_context", "expired_reason_unavailable"),
])
def test_reason_categories_are_state_bound(state, reason, category):
    fixture = lifecycle(state)
    next(iter(fixture.state["deliveries"].values()))["reason"] = reason
    assert render(fixture)["deliveries"][0]["reason"] == category


@pytest.mark.parametrize("state", list(view.DELIVERY_LABELS))
def test_every_boundary_has_explicit_unavailable_guidance_without_capabilities(state):
    fixture = lifecycle(state)
    row = render(fixture)["deliveries"][0]
    assert row["action_guidance"]["actions"] == []
    assert row["action_guidance"]["unavailable_reason"] == "read_only_export"
    if state == "held_for_recovery":
        assert row["reason"] == next(iter(fixture.state["deliveries"].values()))["reason"]
    elif state not in {"rejected", "expired", "cancelled", "dead_lettered"}:
        assert row["reason"] is None


@pytest.mark.parametrize("state,action", [("queued", "cancel_queued_context"),
                                           ("held_for_recovery", "dispose_held_context")])
def test_guidance_tracks_capability_removal_and_snapshot(state, action):
    fixture = lifecycle(state)
    operator = scope(audience="operator")
    target = view._opaque(operator, "delivery", next(iter(fixture.state["deliveries"])))
    cap = {"available": True, "actions_by_delivery": {target: [action]}, "reason": None}
    result = render(fixture, scope=operator, operator_commands=cap)
    assert result["deliveries"][0]["action_guidance"] == {"actions": [action], "unavailable_reason": None}
    cap["actions_by_delivery"].clear()
    after = render(fixture, scope=operator, operator_commands=cap)
    assert after["deliveries"][0]["action_guidance"]["actions"] == []
    assert result["snapshot"] != after["snapshot"]
    assert result["deliveries"][0]["action_guidance"]["actions"] == [action]


def test_guidance_requires_exact_projected_target_for_retain_and_link():
    commands = {"actions_by_delivery": {}, "reason": None}
    dispositions = {"available": True, "targets": [{"id": "one"}]}
    linked = {"available": True, "targets": [{"parent_target": "one"}]}
    assert view._delivery_guidance("one", commands, dispositions, linked)["actions"] == [
        "retain_held_context", "propose_linked_replacement"]
    assert view._delivery_guidance("other", commands, dispositions, linked)["actions"] == []


def test_page_separates_hold_reason_and_capability_guidance_without_new_dispatch():
    source = page_bytes(nonce="n" * 24).decode()
    assert "d.state==='held_for_recovery'?'Hold: ':'Reason: '" in source
    assert "deliveryGuidance(d)" in source
    assert "Each action is checked by the host; none implies retry or resolves contact/spend uncertainty." in source
    helper = source.split("function deliveryGuidance(delivery)", 1)[1].split("function journalTime", 1)[0]
    assert "Object.hasOwn(labels,action)" in helper
    assert "request(" not in helper and "beginCommand(" not in helper

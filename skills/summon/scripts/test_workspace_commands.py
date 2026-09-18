"""Pure operator-command contract tests; no active HTTP or durable lookup claim."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_commands as commands
from _workspace_view import ViewScope, _opaque
from test_workspace_protocol import delivery, message


def fixture(state="queued", action="cancel_queued_context"):
    workspace = {"workspace_id": "workspace", "run_id": "run", "messages": {"message": message()},
                 "deliveries": {"delivery": delivery(state)}, "lanes": {"main-2": {}}}
    view = ViewScope("workspace", "run", b"v" * 32, "operator")
    scope = commands.CommandScope("workspace", "run", (("delivery", (action,)),))
    body = {"operation_key": "a" * 32, "action": action, "target": _opaque(view, "delivery", "delivery")}
    return workspace, {"view_scope": view, "command_scope": scope, "body": body}


@pytest.mark.parametrize("state,action,target,role,category", [
    ("queued", "cancel_queued_context", "cancelled", "authorized_cancellation", "grant"),
    ("held_for_recovery", "dispose_held_context", "dead_lettered", "authenticated_disposition", "event"),
], ids=['p001_case_001', 'p001_case_002'])
def test_binding_is_immutable_bounded_and_reconstructable_after_disposition(state, action, target, role, category):
    workspace, options = fixture(state, action)
    before = copy.deepcopy(workspace)
    bound = commands.bind_command(workspace, **options)
    assert (bound.target_state, bound.proof_role, bound.proof_category) == (target, role, category)
    assert len(bound.source_bytes) <= 4096
    assert workspace == before
    workspace["deliveries"]["delivery"].update(state=target, reason="operator_requested")
    assert commands.bind_command(workspace, **options, for_lookup=True) == bound
    assert workspace["deliveries"]["delivery"]["certainty"] == before["deliveries"]["delivery"]["certainty"]
    with pytest.raises(commands.WorkspaceCommandError, match="command_state_refused"):
        commands.bind_command(workspace, **options)
    assert "delivery" not in repr(bound) and bound.request_sha256 not in repr(bound)


@pytest.mark.parametrize("change", ["missing_scope", "public", "foreign_scope", "mutable_scope", "duplicate_scope", "action", "raw_target", "unknown_field", "bad_key", "identity", "state"], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007', 'p002_case_008', 'p002_case_009', 'p002_case_010', 'p002_case_011'])
def test_read_scope_and_client_claims_cannot_become_command_authority(change):
    workspace, options = fixture()
    if change == "missing_scope": options["command_scope"] = None
    elif change == "public": options["view_scope"] = ViewScope("workspace", "run", b"v" * 32)
    elif change == "foreign_scope": options["command_scope"] = commands.CommandScope("other", "run", (("delivery", ("cancel_queued_context",)),))
    elif change == "mutable_scope": options["command_scope"] = commands.CommandScope("workspace", "run", [("delivery", ("cancel_queued_context",))])
    elif change == "duplicate_scope": options["command_scope"] = commands.CommandScope("workspace", "run", (("delivery", ("cancel_queued_context",)),) * 2)
    elif change == "action": options["body"]["action"] = "dispose_held_context"
    elif change == "raw_target": options["body"]["target"] = "delivery"
    elif change == "unknown_field": options["body"]["certainty"] = {"spend": "not_incurred"}
    elif change == "bad_key": options["body"]["operation_key"] = "private-command-path"
    elif change == "identity": workspace["deliveries"]["delivery"]["recipient"]["instance_id"] = "another-worker"
    else: workspace["deliveries"]["delivery"] = delivery("submission_started")
    before = copy.deepcopy(workspace)
    with pytest.raises(commands.WorkspaceCommandError):
        commands.bind_command(workspace, **options)
    assert workspace == before


@pytest.mark.parametrize("status,durable,expected", [
    ("not_observed", False, "uncertain"), ("not_observed", True, "uncertain"),
    ("evidence_only", False, "uncertain"), ("evidence_only", True, "pending"),
    ("recorded", False, "uncertain"), ("recorded", True, "recorded"),
    ("uncertain", True, "uncertain"),
], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006', 'p003_case_007'])
def test_only_matched_durable_runtime_observation_can_report_recorded(status, durable, expected):
    workspace, options = fixture()
    bound = commands.bind_command(workspace, **options)
    result = commands.present_lookup(bound, {"status": status, "request_sha256": bound.request_sha256,
                                           "durable_prefix_verified": durable, "revision": 7})
    assert result["status"] == expected and result["retry_with_new_key"] is False
    assert result["target_state"] == ("cancelled" if expected == "recorded" else None)
    assert bound.request_sha256 not in json.dumps(result) and "source_bytes" not in result


def test_unknown_or_conflicting_operation_observation_cannot_be_success():
    workspace, options = fixture()
    bound = commands.bind_command(workspace, **options)
    base = {"status": "recorded", "request_sha256": None, "durable_prefix_verified": True, "revision": 7}
    assert commands.present_lookup(bound, base)["status"] == "uncertain"
    with pytest.raises(commands.WorkspaceCommandError, match="command_request_conflict"):
        commands.present_lookup(bound, dict(base, request_sha256="b" * 64))
    with pytest.raises(commands.WorkspaceCommandError, match="invalid_command_observation"):
        commands.present_lookup(bound, dict(base, raw_receipt="SYNTHETIC-PRIVATE"))
    with pytest.raises(commands.WorkspaceCommandError, match="invalid_command_observation"):
        commands.present_lookup(bound, dict(base, status={}))

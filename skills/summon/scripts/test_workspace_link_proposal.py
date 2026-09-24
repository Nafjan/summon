"""Provider-free linked-replacement proposal and authority refusal tests."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _workspace_admission as admission
from test_workspace_protocol import delivery, message, ref


def _proposal() -> dict:
    return {"schema": admission.LINKED_REPLACEMENT_PROPOSAL_SCHEMA,
            "action": admission.LINKED_REPLACEMENT_ACTION, "operation_key": "a" * 32,
            "parent_target": "opaque-parent", "recipient_target": "opaque-recipient"}


def _authority(parent: dict, logical_message: dict) -> dict:
    recipient = copy.deepcopy(parent["recipient"])

    def source(name: str, *, parent_id: str = "delivery", generation: int = 3,
              expires_at_ms: int = 100, revoked: bool = False) -> dict:
        return {"reference": ref(name), "parent_delivery_id": parent_id,
                "task_id": logical_message["destination_task_id"],
                "recipient": copy.deepcopy(recipient), "generation": generation,
                "expires_at_ms": expires_at_ms, "revoked": revoked}

    return {"parent_delivery_id": parent["delivery_id"], "parent_target": "opaque-parent",
            "recipient_target": "opaque-recipient", "task_id": logical_message["destination_task_id"],
            "recipient": recipient, "generation": 3, "now_ms": 10,
            "sources": {"authenticated_disposition": source("disposition"),
                        "remaining_authority": source("remaining"),
                        "fresh_attempt_grant": source("fresh"),
                        "physical_fence": source("fence")}}


def test_linked_replacement_proposal_is_typed_opaque_and_detached():
    proposal = _proposal()
    checked = admission.linked_replacement_proposal(proposal)
    assert checked == proposal
    proposal["parent_target"] = "changed"
    assert checked["parent_target"] == "opaque-parent"
    for forbidden in ("delivery", "evidence", "grant", "decision", "journal_payload"):
        with pytest.raises(admission.WorkspaceAdmissionError):
            admission.linked_replacement_proposal(_proposal() | {forbidden: {"id": "not-authority"}})


def test_valid_linked_replacement_sources_are_detached_and_role_complete():
    parent, logical_message = delivery("held_for_recovery"), message()
    authority = _authority(parent, logical_message)
    result = admission.validate_linked_replacement_authority(
        _proposal(), parent=parent, message=logical_message, authority=authority)
    assert result["parent_delivery_id"] == "delivery"
    assert result["task_id"] == "main-2"
    assert set(result["evidence"]) == {"authenticated_disposition", "remaining_authority",
                                         "fresh_attempt_grant", "physical_fence"}
    authority["sources"]["physical_fence"]["parent_delivery_id"] = "changed"
    assert result["evidence"]["physical_fence"] == ref("fence")


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "missing or unknown"), ("stale", "authority is stale"),
    ("wrong_parent", "authority parent differs"),
], ids=["missing-authority", "stale-authority", "wrong-parent-authority"])
def test_linked_replacement_refuses_missing_stale_or_wrong_parent_authority(mutation, expected):
    parent, logical_message = delivery("held_for_recovery"), message()
    authority = _authority(parent, logical_message)
    if mutation == "missing":
        del authority["sources"]["physical_fence"]
    elif mutation == "stale":
        authority["sources"]["fresh_attempt_grant"]["generation"] = 2
    else:
        authority["sources"]["authenticated_disposition"]["parent_delivery_id"] = "other"
    before = copy.deepcopy(authority)
    with pytest.raises(admission.WorkspaceAdmissionError, match=expected):
        admission.validate_linked_replacement_authority(
            _proposal(), parent=parent, message=logical_message, authority=authority)
    assert authority == before


def test_linked_replacement_refuses_copied_grant_or_wrong_recipient_scope():
    parent, logical_message = delivery("held_for_recovery"), message()
    authority = _authority(parent, logical_message)
    authority["sources"]["fresh_attempt_grant"]["reference"] = copy.deepcopy(parent["grant_ref"])
    with pytest.raises(admission.WorkspaceAdmissionError, match="fresh grant"):
        admission.validate_linked_replacement_authority(
            _proposal(), parent=parent, message=logical_message, authority=authority)
    authority = _authority(parent, logical_message)
    authority["sources"]["remaining_authority"]["task_id"] = "side-1"
    with pytest.raises(admission.WorkspaceAdmissionError, match="scope differs"):
        admission.validate_linked_replacement_authority(
            _proposal(), parent=parent, message=logical_message, authority=authority)

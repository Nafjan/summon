"""Provider-free ConversationRuntime source-family acceptance.

The fixture uses the synthetic dispatcher from ``test_conversation_runtime``.
It proves that a fresh chat turn does not self-authorize its continuation: an
independent revalidation packet must be accepted and linked before the real
runtime can consume it at the child guard boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_launch_qualification as chat_qualification
import _chat_source_family as source_family
import _launch_qualification as policy
from _conversation import ConversationJournal
from _conversation_runtime import ConversationRuntime, ConversationRuntimeError
from _launch_binding import binding_projection


def _fixture():
    # Keep this focused acceptance file from collecting the large compatibility
    # unittest class a second time when it is selected in a release partition.
    from test_conversation_runtime import ConversationRuntimeTests
    owner = ConversationRuntimeTests("test_durable_turn_and_same_provider_session_resume")
    owner.setUp()
    runtime = ConversationRuntime(
        owner.root, cwd=owner.project, agents_dir=owner.roster,
        dispatcher=owner.dispatcher, timeout_ms=10_000, strict_agents_dir=True,
    )
    return owner, runtime


def _last_observation(runtime, owner):
    journal = ConversationJournal.open(owner.root, "room-1")
    starts, finish = runtime._last_turn(journal.events(native=True), "worker")
    return (
        starts["payload"]["turn_id"],
        finish["payload"]["launch_observation"],
    )


def _qualification(runtime, owner, *, turn_id, observation, expires_at=None):
    family_dir = runtime._runtime_owner_dir("room-1", "worker", create=False)
    family_path = source_family.family_path(family_dir)
    family = source_family.read(family_path)
    fields = {
        key: observation[key] for key in (
            "backend", "transport", "registry_generation", "registry_digest",
            "adapter", "adapter_version", "external_cli_version",
            "executable_sha256", "launch_material_sha256")}
    fields["material_contract"] = policy.material_contract_for(
        backend=observation["backend"], transport=observation["transport"],
        adapter=observation["adapter"], adapter_version=observation["adapter_version"])
    fields["revocation_id"] = policy.revocation_id_for(
        dict(fields, operation="chat_resume"))
    qualification = chat_qualification.issue(
        source_family_id=family["source_family_id"], turn_id=turn_id,
        observation=observation,
        expires_at=time.time() + 600 if expires_at is None else expires_at,
        token=family["nonce"], **fields)
    return family, family_path, qualification


def _first(owner, runtime):
    with patch.dict("os.environ", {"FAKE_MARKER": str(owner.marker)}):
        first = runtime.start_turn("room-1", "worker", "first", wait=True)
    assert first["status"] == "success"
    turn_id, observation = _last_observation(runtime, owner)
    return turn_id, observation


def test_revalidated_source_is_consumed_by_real_runtime(tmp_path):
    del tmp_path
    owner, runtime = _fixture()
    try:
        turn_id, observation = _first(owner, runtime)
        family, family_path, qualification = _qualification(
            runtime, owner, turn_id=turn_id, observation=observation)
        journal_before = (owner.root / "room-1.jsonl").read_bytes()
        receipt = runtime.revalidate_chat_source(
            "room-1", "worker", turn_id=turn_id,
            observation=observation, qualification=qualification)
        assert receipt["provider_contacted"] is False
        assert receipt["launch_started"] is False
        assert (owner.marker.read_text(encoding="utf-8").count("\n")) == 1
        assert (owner.root / ".chat-runtime" / "room-1" / "worker" /
                f"chat-qualification-{turn_id}.json").exists()
        assert (owner.root / "room-1.jsonl").read_bytes() == journal_before
        assert source_family.read(family_path)["qualifications"]
        with patch.dict("os.environ", {"FAKE_MARKER": str(owner.marker)}):
            continuation = runtime.start_turn("room-1", "worker", "continue", wait=True)
        assert continuation["status"] == "success"
        assert owner.marker.read_text(encoding="utf-8").count("\n") == 2
        # No automatic revalidation is allowed: a new source turn needs a new
        # independently authenticated packet.
        with patch.dict("os.environ", {"FAKE_MARKER": str(owner.marker)}):
            refused = runtime.start_turn("room-1", "worker", "third", wait=True)
        assert refused["status"] == "blocked"
        assert refused["refusal"]["reason_code"] == "resume_launch_qualification_missing"
        assert owner.marker.read_text(encoding="utf-8").count("\n") == 2
    finally:
        runtime.close(timeout=2)
        owner.tearDown()


@pytest.mark.parametrize("kind", ["forged", "expired", "revoked", "wrong_turn", "stale_observation"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_invalid_source_qualification_refuses_without_provider(kind):
    owner, runtime = _fixture()
    try:
        turn_id, observation = _first(owner, runtime)
        family, family_path, qualification = _qualification(
            runtime, owner, turn_id=turn_id,
            observation=observation,
            expires_at=None)
        presented_observation = dict(observation)
        presented_qualification = dict(qualification)
        if kind == "forged":
            presented_qualification["auth"] = "0" * 64
        elif kind == "revoked":
            source_family.revoke(family_path, family, qualification["revocation_id"])
        elif kind == "expired":
            presented_qualification["expires_at"] = time.time() - 1
            body = {key: value for key, value in presented_qualification.items()
                    if key != "auth"}
            presented_qualification["auth"] = chat_qualification._auth(
                family["nonce"], body)
        elif kind == "wrong_turn":
            presented_qualification = chat_qualification.issue(
                source_family_id=family["source_family_id"], turn_id="wrong-turn",
                observation=observation, expires_at=time.time() + 600,
                token=family["nonce"], **{
                    key: qualification[key] for key in (
                        "backend", "transport", "registry_generation", "registry_digest",
                        "adapter", "adapter_version", "external_cli_version",
                        "material_contract", "executable_sha256", "launch_material_sha256",
                        "revocation_id")})
        elif kind == "stale_observation":
            presented_observation["executable_mtime_ns"] += 1
        with pytest.raises(ConversationRuntimeError):
            runtime.revalidate_chat_source(
                "room-1", "worker", turn_id=turn_id,
                observation=presented_observation,
                qualification=presented_qualification)
        with patch.dict("os.environ", {"FAKE_MARKER": str(owner.marker)}):
            refused = runtime.start_turn("room-1", "worker", "continue", wait=True)
        assert refused["status"] == "blocked"
        assert refused["refusal"]["reason_code"] in {
            "resume_launch_qualification_missing", "resume_launch_qualification_invalid",
            "resume_launch_qualification_revoked"}
        assert owner.marker.read_text(encoding="utf-8").count("\n") == 1
    finally:
        runtime.close(timeout=2)
        owner.tearDown()

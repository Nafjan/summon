"""Provider-free public chat revalidation and legacy migration acceptance."""

from __future__ import annotations

import json
from io import StringIO
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
import run_subagent as dispatcher
from _conversation import ConversationJournal
from _conversation_runtime import ConversationRuntime


MIGRATION_KEY = "synthetic-chat-migration-key-0123456789abcdef"


def _fixture():
    # Import lazily so pytest does not collect the large unittest compatibility
    # class a second time when this focused acceptance file is selected.
    from test_conversation_runtime import ConversationRuntimeTests
    owner = ConversationRuntimeTests("test_durable_turn_and_same_provider_session_resume")
    owner.setUp()
    runtime = ConversationRuntime(
        owner.root, cwd=owner.project, agents_dir=owner.roster,
        dispatcher=owner.dispatcher, timeout_ms=10_000, strict_agents_dir=True,
    )
    return owner, runtime


def _packet(owner, runtime):
    with patch.dict("os.environ", {"FAKE_MARKER": str(owner.marker)}):
        first = runtime.start_turn("room-1", "worker", "first", wait=True)
    assert first["status"] == "success"
    journal = ConversationJournal.open(owner.root, "room-1")
    start, finish = runtime._last_turn(journal.events(native=True), "worker")
    turn_id = start["payload"]["turn_id"]
    observation = finish["payload"]["launch_observation"]
    owner_dir = runtime._runtime_owner_dir("room-1", "worker", create=False)
    family_path = source_family.family_path(owner_dir)
    family = source_family.read(family_path)
    fields = {key: observation[key] for key in (
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
        observation=observation, expires_at=time.time() + 600,
        token=family["nonce"], **fields)
    packet = {
        "schema": "summon.chat-revalidation-packet/v1",
        "session_id": "room-1", "participant": "worker", "turn_id": turn_id,
        "observation": observation, "qualification": qualification,
        "source_family": family,
    }
    packet["authority"] = source_family.issue_migration_authority(
        packet, key=MIGRATION_KEY)
    return packet, family_path, owner, runtime


def _remove_family(family_path):
    Path(family_path).unlink()
    assert not Path(family_path).exists()


def test_public_chat_revalidate_migrates_legacy_room_without_provider(tmp_path, monkeypatch, capsys):
    del tmp_path
    packet, family_path, owner, runtime = _packet(*_fixture())
    # _packet returns the owner/runtime created by the same fixture; retain the
    # explicit teardown even when the public command refuses.
    try:
        _remove_family(family_path)
        packet_path = owner.base / "chat-revalidation.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        monkeypatch.setenv("SUMMON_CHAT_MIGRATION_KEY", MIGRATION_KEY)
        monkeypatch.setenv("SUMMON_TELEMETRY", "0")
        with patch.object(sys, "argv", [
            "summon", "chat", "revalidate", "room-1", "worker",
            "--evidence-file", str(packet_path),
            "--conversation-dir", str(owner.root), "--cwd", str(owner.project),
            "--agents-dir", str(owner.roster), "--strict-agents-dir", "--json",
        ]), patch("subprocess.Popen", side_effect=AssertionError("provider launch")):
            with pytest.raises(SystemExit) as completed:
                dispatcher.main()
        assert completed.value.code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "revalidated"
        assert report["migration"] == "legacy_source_family"
        assert report["provider_contacted"] is False
        assert Path(family_path).is_file()
        assert source_family.read(family_path)["qualifications"]
    finally:
        runtime.close(timeout=2)
        owner.tearDown()


def test_legacy_migration_requires_separate_authority_before_sidecars(monkeypatch):
    packet, family_path, owner, runtime = _packet(*_fixture())
    try:
        _remove_family(family_path)
        packet.pop("authority")
        monkeypatch.delenv("SUMMON_CHAT_MIGRATION_KEY", raising=False)
        before = {p.relative_to(owner.root): p.read_bytes()
                  for p in owner.root.rglob("*") if p.is_file()}
        with pytest.raises(Exception) as refused:
            runtime.revalidate_chat_packet("room-1", "worker", packet)
        assert "authority" in str(refused.value)
        after = {p.relative_to(owner.root): p.read_bytes()
                 for p in owner.root.rglob("*") if p.is_file()}
        assert before == after
        assert not Path(family_path).exists()
    finally:
        runtime.close(timeout=2)
        owner.tearDown()


def test_public_chat_revalidate_consumes_existing_family_without_migration_key(monkeypatch, capsys):
    packet, family_path, owner, runtime = _packet(*_fixture())
    try:
        packet = {key: packet[key] for key in (
            "schema", "session_id", "participant", "turn_id",
            "observation", "qualification")}
        packet_path = owner.base / "sealed-room-revalidation.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        monkeypatch.delenv("SUMMON_CHAT_MIGRATION_KEY", raising=False)
        monkeypatch.setenv("SUMMON_TELEMETRY", "0")
        monkeypatch.setattr(sys, "argv", [
            "summon", "chat", "revalidate", "room-1", "worker",
            "--evidence-file", str(packet_path), "--conversation-dir", str(owner.root),
            "--cwd", str(owner.project), "--agents-dir", str(owner.roster),
            "--strict-agents-dir", "--json",
        ])
        with patch("subprocess.Popen", side_effect=AssertionError("provider launch")):
            with pytest.raises(SystemExit) as completed:
                dispatcher.main()
        assert completed.value.code == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "revalidated"
        assert report["provider_contacted"] is False
        assert Path(family_path).is_file()
    finally:
        runtime.close(timeout=2)
        owner.tearDown()

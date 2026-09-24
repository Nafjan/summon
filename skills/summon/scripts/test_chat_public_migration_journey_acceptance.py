"""Independent public migration, immutable history and later continuation."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_source_family as family
import _chat_launch_qualification as qualification
import run_subagent as dispatcher
import test_chat_legacy_migration_acceptance as fixture


@pytest.mark.parametrize("case", ["supported", "forged_migration", "wrong_key", "forged_qualification", "expired"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_public_migration_preserves_history_and_qualifies_only_supported_continuation(monkeypatch, capsys, case):
    packet, family_path, owner, runtime = fixture._packet(*fixture._fixture())
    try:
        fixture._remove_family(family_path)
        if case == "forged_migration":
            packet["authority"]["auth"] = "0" * 64
        elif case in {"forged_qualification", "expired"}:
            if case == "forged_qualification":
                packet["qualification"]["auth"] = "0" * 64
            else:
                packet["qualification"]["expires_at"] = time.time() - 1
                body = {k: v for k, v in packet["qualification"].items() if k != "auth"}
                packet["qualification"]["auth"] = qualification._auth(packet["source_family"]["nonce"], body)
            packet["authority"] = family.issue_migration_authority(packet, key=fixture.MIGRATION_KEY)
        packet_path = owner.base / "synthetic-migration.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        history = (owner.root / "room-1.jsonl").read_bytes()

        def snapshot():
            return {str(p.relative_to(owner.root)): p.read_bytes() if p.is_file() else None
                    for p in owner.root.rglob("*")}

        before = snapshot()
        monkeypatch.setenv("SUMMON_CHAT_MIGRATION_KEY", "wrong-synthetic-key-" * 3 if case == "wrong_key" else fixture.MIGRATION_KEY)
        monkeypatch.setenv("SUMMON_TELEMETRY", "0")
        monkeypatch.setattr(sys, "argv", [
            "summon", "chat", "revalidate", "room-1", "worker", "--evidence-file", str(packet_path),
            "--conversation-dir", str(owner.root), "--cwd", str(owner.project),
            "--agents-dir", str(owner.roster), "--strict-agents-dir", "--json",
        ])
        with monkeypatch.context() as nonlaunching:
            def forbidden(*args, **kwargs):
                pytest.fail("public migration launched a child", pytrace=False)
            nonlaunching.setattr(subprocess, "Popen", forbidden)
            with pytest.raises(SystemExit) as completed:
                dispatcher.main()
        captured = capsys.readouterr()
        public = captured.out + captured.err
        safe = all(value not in public for value in (
            str(owner.base), fixture.MIGRATION_KEY, packet["source_family"]["nonce"], packet["qualification"]["auth"]))
        assert safe, "migration output exposed private source or authority material"
        unchanged_history = (owner.root / "room-1.jsonl").read_bytes() == history
        assert unchanged_history, "migration rewrote historical journal bytes"
        assert completed.value.code == (0 if case == "supported" else 1)
        if case == "supported":
            report = json.loads(captured.out)
            assert report["launch_started"] is False and report["provider_contacted"] is False
            monkeypatch.setenv("FAKE_MARKER", str(owner.marker))
            result = runtime.start_turn("room-1", "worker", "continue synthetic task", wait=True)
            assert result["status"] == "success"
            calls = [json.loads(line) for line in owner.marker.read_text(encoding="utf-8").splitlines()]
            assert len(calls) == 2 and "--resume" in calls[1]
        else:
            unchanged = snapshot() == before
            assert unchanged, "refused migration changed the stored file set or bytes"
            assert not Path(family_path).exists()
    finally:
        runtime.close(timeout=2)
        owner.tearDown()

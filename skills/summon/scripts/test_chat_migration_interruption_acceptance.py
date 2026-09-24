"""Public migration recovery across interrupted qualification/family publication."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_source_family as family
import run_subagent as dispatcher
import test_chat_legacy_migration_acceptance as fixture


@pytest.mark.parametrize("boundary", [
    "before_qualification", "before_family", "after_family", "before_family_with_refused_resume",
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_interrupted_public_migration_reconciles_same_authenticated_packet(monkeypatch, capsys, boundary):
    packet, family_path, owner, runtime = fixture._packet(*fixture._fixture())
    try:
        fixture._remove_family(family_path)
        packet_path = owner.base / "synthetic-interruption-packet.json"
        packet_path.write_text(json.dumps(packet), encoding="utf-8")
        journal = owner.root / "room-1.jsonl"
        original_history = journal.read_bytes()
        monkeypatch.setenv("SUMMON_CHAT_MIGRATION_KEY", fixture.MIGRATION_KEY)
        monkeypatch.setenv("SUMMON_TELEMETRY", "0")
        monkeypatch.setattr(sys, "argv", [
            "summon", "chat", "revalidate", "room-1", "worker", "--evidence-file", str(packet_path),
            "--conversation-dir", str(owner.root), "--cwd", str(owner.project),
            "--agents-dir", str(owner.roster), "--strict-agents-dir", "--json",
        ])

        def forbidden(*args, **kwargs):
            pytest.fail("interrupted migration or refused resume launched a child", pytrace=False)

        monkeypatch.setattr(subprocess, "Popen", forbidden)

        def invoke():
            with pytest.raises(SystemExit) as result:
                dispatcher.main()
            captured = capsys.readouterr()
            assert str(owner.base) not in captured.out + captured.err
            return result.value.code, json.loads(captured.out)

        original_write = family._atomic_write
        interrupted = False

        def interrupt_write(path, value):
            nonlocal interrupted
            is_family = Path(path) == Path(family_path)
            target = not is_family if boundary == "before_qualification" else is_family
            if target and not interrupted:
                interrupted = True
                if boundary == "after_family":
                    original_write(path, value)
                raise family.SourceFamilyError("synthetic_interruption", "synthetic publication interruption")
            return original_write(path, value)

        with monkeypatch.context() as interruption:
            interruption.setattr(family, "_atomic_write", interrupt_write)
            code, _ = invoke()
        assert interrupted and code == 1
        preserved = journal.read_bytes() == original_history
        assert preserved, "interrupted migration rewrote historical journal bytes"

        if boundary == "before_family_with_refused_resume":
            result = runtime.start_turn("room-1", "worker", "synthetic continuation probe", wait=True)
            assert result["status"] == "blocked"

        before_reconcile = journal.read_bytes()
        code, report = invoke()
        assert code == 0, "same authenticated migration packet could not reconcile interrupted publication"
        assert report["launch_started"] is False and report["provider_contacted"] is False
        preserved = journal.read_bytes() == before_reconcile
        assert preserved, "migration reconciliation rewrote history"
        current = family.read(family_path)
        qualified, _ = family.read_qualification(family_path, current, packet["turn_id"])
        assert qualified == packet["qualification"]
    finally:
        runtime.close(timeout=2)
        owner.tearDown()

"""Public jobs revalidation through the real dispatcher, with synthetic authority."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _job_continuation as continuation
import _launch_qualification as qualification
import run_subagent as dispatcher
from test_resume_revalidation_acceptance import _legacy_packet, _files


@pytest.mark.parametrize("case", ["supported", "forged_auth", "revoked"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_public_jobs_revalidation_is_nonlaunching_and_preserves_history(tmp_path, monkeypatch, capsys, case):
    root, job_id, qualified, evidence = _legacy_packet(tmp_path)
    if case == "forged_auth":
        qualified = dict(qualified, auth="0" * 64)
    if case == "revoked":
        qualification.revoke(root, job_id, qualified["revocation_id"])
    packet = tmp_path / "synthetic-revalidation.json"
    packet.write_text(json.dumps({
        "observation": evidence["launch_observation"], "qualification": qualified,
    }), encoding="utf-8")
    source = Path(continuation.continuation_path(root, job_id))
    original = source.read_bytes()
    before = _files(root)

    def forbidden(*args, **kwargs):
        pytest.fail("public revalidation attempted to spawn a child")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setenv("SUMMON_TELEMETRY", "0")
    monkeypatch.setattr(sys, "argv", [
        "summon", "jobs", "revalidate", job_id,
        "--evidence-file", str(packet), "--job-dir", root, "--json",
    ])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    output = capsys.readouterr()
    assert completed.value.code == (0 if case == "supported" else 1)
    assert source.read_bytes() == original
    public = output.out + output.err
    assert str(tmp_path) not in public
    assert qualified["auth"] not in public
    if case == "supported":
        report = json.loads(output.out)
        assert report["status"] == "revalidated"
        assert report["launch_started"] is False and report["provider_contacted"] is False
        assert continuation.read_launch_binding(root, job_id) is not None
    else:
        after = _files(root)
        assert set(before) == set(after)
        assert all(before[p] == after[p] for p in before)

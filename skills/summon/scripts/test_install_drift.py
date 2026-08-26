"""Provider-inert install-drift scope tests."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _installs import drift_report
from _doctor import render


def _record(label: str, sha: str | None, managed: bool) -> dict:
    return {
        "label": label,
        "present": True,
        "managed": managed,
        "running": label == "running",
        "sha256": sha,
        "duplicates": [],
        "duplicates_truncated": False,
        "version": "fixture",
        "scripts_dir": f"C:/synthetic/{label}/scripts",
    }


def test_managed_convergence_is_separate_from_unmanaged_drift():
    records = [
        _record("host-a", "a" * 64, True),
        _record("plugin-local", "b" * 64, False),
        _record("running", "a" * 64, False),
    ]

    report = drift_report(records)

    assert report["converged"] is False
    assert report["managed_converged"] is True
    assert [r["label"] for r in report["managed_drifted"]] == []
    assert [r["label"] for r in report["unmanaged_drifted"]] == ["plugin-local"]


def test_managed_convergence_requires_a_present_managed_copy():
    report = drift_report([_record("running", "a" * 64, False)])

    assert report["converged"] is True
    assert report["managed_converged"] is False


def test_unmanaged_running_copy_cannot_make_identical_managed_set_drift():
    records = [
        _record("host-a", "b" * 64, True),
        _record("host-b", "b" * 64, True),
        _record("running", "a" * 64, False),
    ]
    report = drift_report(records)

    assert report["converged"] is False
    assert report["managed_converged"] is True
    assert report["managed_reference_sha"] == "b" * 64
    assert report["running_matches_managed"] is False
    assert report["managed_drifted"] == []
    assert [item["label"] for item in report["drifted"]] == ["host-a", "host-b"]

    rendered = render({
        "platform": "synthetic", "python": "3.13", "git": {"found": True},
        "backends": {},
        "agents_dir": {"found": True, "path": "synthetic", "agent_count": 0},
        "billing_guard": {"openai_api_key_present": False, "guard_active": True},
        "installs": {"records": records, "drift": report},
        "usable_backends": [],
        "ok": False,
    })
    assert "all installer-managed copies agree; the running unmanaged copy differs" in rendered
    assert "run  python install.py" not in rendered
    running_line = next(line for line in rendered.splitlines() if "running" in line)
    assert "re-run install.py" not in running_line
    assert "installer will not modify it" not in running_line

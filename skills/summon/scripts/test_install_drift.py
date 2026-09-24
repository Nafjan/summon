"""Provider-inert install-drift scope tests."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _installs import _probe, drift_report
from _doctor import render


ROOT = Path(__file__).resolve().parents[3]
PUBLIC_DOCS = (
    *sorted((ROOT / "docs" / "planning").glob("*.md")),
    ROOT / "CHANGELOG.md",
    ROOT / "docs" / "ENGINEERING_CHANGELOG.md",
    ROOT / "skills" / "summon" / "references" / "backends.md",
)
PRIVATE_PATTERNS = (
    ("machine-specific absolute path", re.compile(
        r"(?i)\b[A-Z]:[\\/](?:Users|home|Antigravity|AppData|ProgramData|Windows|private|workspace|evidence|secrets)[^\s`)]*")),
    ("private evidence path", re.compile(
        r"(?i)\bsummon-evidence[-\\/][^\s`)]*")),
    ("raw error identifier", re.compile(r"\berr_[0-9a-f]{6,}\b")),
    ("account-specific subscription claim", re.compile(
        r"(?i)\b(?:subscription refresh activates|on the account, on the ModelArk|billing is disabled; the calls draw)\b")),
    ("unqualified ModelArk file-capability claim", re.compile(
        r"(?i)file-capable inputs via the `modelark` OpenCode provider")),
)


def _privacy_violations(text: str) -> list[str]:
    return [rule for rule, pattern in PRIVATE_PATTERNS if pattern.search(text)]


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


def _skill_tree(root: Path) -> Path:
    """Build a minimal complete skill tree under a synthetic host layout."""
    skill = root / "skills" / "summon"
    (skill / "scripts").mkdir(parents=True)
    for directory in ("references", "agents", "examples"):
        (skill / directory).mkdir()
    (skill / "SKILL.md").write_text("---\nname: summon\n---\n", encoding="utf-8")
    (skill / "scripts" / "run_subagent.py").write_text(
        "__version__ = '3.5.0'\n", encoding="utf-8")
    (skill / "scripts" / "summon.cmd").write_text("@echo off\n", encoding="utf-8")
    (skill / "references" / "backends.md").write_text(
        "initial public routing note\n", encoding="utf-8")
    (skill / "agents" / "worker.md").write_text("worker\n", encoding="utf-8")
    (skill / "examples" / "sample.txt").write_text("sample\n", encoding="utf-8")
    return skill


def test_docs_only_payload_drift_blocks_generic_convergence_and_refresh_restores_it():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = _skill_tree(root / "source")
        managed = _skill_tree(root / "managed")
        source_rec = _probe("running", str(source / "scripts"), managed=False)
        source_rec["running"] = True
        managed_rec = _probe("host-a", str(managed / "scripts"), managed=True)
        before = drift_report([managed_rec, source_rec])
        assert before["payload_tracking"] is True
        assert before["converged"] is True
        assert before["managed_converged"] is True
        scripts_sha = source_rec["sha256"]

        (source / "references" / "backends.md").write_text(
            "docs-only correction\n", encoding="utf-8")
        source_after = _probe("running", str(source / "scripts"), managed=False)
        source_after["running"] = True
        after = drift_report([managed_rec, source_after])
        assert source_after["sha256"] == scripts_sha
        assert after["drifted"] == []
        assert [item["label"] for item in after["payload_drifted"]] == ["host-a"]
        assert after["converged"] is False
        assert after["managed_converged"] is True
        assert after["managed_internal_converged"] is True
        assert after["managed_source_converged"] is False

        shutil.copyfile(source / "references" / "backends.md",
                        managed / "references" / "backends.md")
        refreshed = _probe("host-a", str(managed / "scripts"), managed=True)
        final = drift_report([refreshed, source_after])
        assert final["converged"] is True
        assert final["managed_converged"] is True


def test_mixed_legacy_payload_records_are_unknown_and_fail_closed():
    modern = _record("host-a", "a" * 64, True)
    modern.update(payload_sha256="b" * 64, payload_file_count=1, payload_error=None)
    running = _record("running", "a" * 64, False)
    running["running"] = True
    report = drift_report([modern, running])
    assert report["payload_tracking"] is True
    assert [item["label"] for item in report["payload_unknown"]] == ["running"]
    assert report["converged"] is False
    assert report["managed_converged"] is True
    assert report["managed_source_converged"] is False


def test_payload_links_are_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        skill = _skill_tree(Path(tmp) / "source")
        link = skill / "references" / "outside.md"
        try:
            link.symlink_to(Path(tmp) / "outside.md")
        except OSError:
            # Some Windows runners do not permit unprivileged symlink creation.
            pytest.skip("symlink creation is unavailable on this Windows runner")
        record = _probe("running", str(skill / "scripts"), managed=False)
        assert record["payload_sha256"] is None
        assert "linked payload entry" in (record["payload_error"] or "")


def test_payload_oversize_entries_are_unknown(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        skill = _skill_tree(Path(tmp) / "source")
        monkeypatch.setattr("_installs._ENUM_MAX_BYTES", 8)
        record = _probe("running", str(skill / "scripts"), managed=False)
        assert record["payload_sha256"] is None
        assert "unreadable or oversized" in (record["payload_error"] or "")


def test_doctor_renders_payload_drift_as_nonconverged():
    records = [_record("host-a", "a" * 64, True),
               _record("running", "a" * 64, False)]
    records[0].update(payload_sha256="b" * 64, payload_file_count=1, payload_error=None)
    records[1].update(payload_sha256="c" * 64, payload_file_count=1, payload_error=None)
    records[1]["running"] = True
    report = drift_report(records)
    rendered = render({
        "platform": "synthetic", "python": "3.13", "git": {"found": True},
        "backends": {},
        "agents_dir": {"found": True, "path": "synthetic", "agent_count": 0},
        "billing_guard": {"openai_api_key_present": False, "guard_active": True},
        "installs": {"records": records, "drift": report},
        "usable_backends": [],
        "ok": False,
    })
    assert "installed payload differs from running source" in rendered
    assert "run  python install.py  to converge" in rendered


def test_public_docs_have_no_private_paths_or_account_specific_receipts():
    violations = []
    for path in PUBLIC_DOCS:
        violations.extend(
            f"{path.relative_to(ROOT)}: {rule}"
            for rule in _privacy_violations(path.read_text(encoding="utf-8"))
        )
    assert violations == []


def test_privacy_guard_catches_synthetic_private_values():
    sample = r"D:\workspace\private\receipt.json err_abc123"
    assert set(_privacy_violations(sample)) == {
        "machine-specific absolute path", "raw error identifier"
    }


def test_privacy_guard_allows_repository_relative_synthetic_docs():
    sample = "docs/planning/README.md uses a synthetic receipt and a catalog ID."
    assert _privacy_violations(sample) == []

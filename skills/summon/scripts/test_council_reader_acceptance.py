"""Provider-inert compatibility checks for durable council readers."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path
from unittest import mock

import pytest

import _council
import _loader
import _rundir
from test_discovery import _council_stub_and_runner


def _tree(path):
    return {item.relative_to(path).as_posix(): item.read_bytes()
            for item in Path(path).rglob("*") if item.is_file()}


@pytest.fixture
def council_run(tmp_path):
    for agent in ("m1", "m2", "chair"):
        (tmp_path / f"{agent}.md").write_text(
            "---\nrun-agent: claude\npermission: safe-edit\n---\n# Fixture\n",
            encoding="utf-8")
    _module, _argparse, calls, fake, run = _council_stub_and_runner()
    fresh = argparse.Namespace(
        question="X or Y?", question_file=None, members="m1,m2",
        chairman="chair", rounds=1, cwd=str(tmp_path), agents_dir=str(tmp_path),
        timeout=60000, out=None, run_dir=str(tmp_path))
    with mock.patch.object(_council, "_dispatch", fake):
        rc, result = run(fresh)
    assert rc == 0 and calls["n"] == 3
    resume = argparse.Namespace(**vars(fresh))
    resume.question = resume.members = resume.chairman = resume.rounds = None
    resume.resume_run = result["run_id"]
    return Path(result["run_dir"]), resume, calls, fake, run


def _status(resume):
    args = argparse.Namespace(council_status=resume.resume_run,
                              run_dir=resume.run_dir, cwd=resume.cwd, json=True)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        rc = _council.run_council_status(args)
    return rc, json.loads(output.getvalue())


def test_unversioned_receipt_status_and_unchanged_carry_remain_supported(council_run):
    path, resume, calls, fake, run = council_run
    receipt = json.loads((path / "receipt.json").read_text(encoding="utf-8"))
    # The first durable producer predates these optional governance additions.
    for optional in ("strict_agents_dir", "enable_roles", "roles"):
        receipt.pop(optional, None)
    assert "schema_version" not in receipt
    _rundir.atomic_write_json(str(path / "receipt.json"), receipt)
    before = _tree(path)
    rc, status = _status(resume)
    assert rc == 0 and status["consistent"] is True
    assert status["members"] == ["m1", "m2"]
    assert _tree(path) == before
    calls["n"] = 0
    with mock.patch.object(_council, "_dispatch", fake):
        rc, result = run(resume)
    assert rc == 0 and calls["n"] == 0
    assert result["run_id"] == resume.resume_run
    assert (path / "receipt.json").read_bytes() == before["receipt.json"]


def test_status_detects_owner_absent_generation_race_and_preserves_uncertainty(council_run):
    path, resume, _calls, _fake, _run = council_run
    owner = _rundir.acquire_owner(str(path), 600)
    _rundir.journal_append(str(path), {
        "event": "attempt_started", "generation": owner.generation,
        "attempt_id": "unsettled-attempt", "stage": "r1-m1",
    }, owner=owner)
    _rundir.release_owner(owner)
    before = _tree(path)
    with mock.patch.object(_rundir, "_last_generation", side_effect=[2, 3, 4, 5]) as counter:
        rc, status = _status(resume)
    assert rc == 0 and status["consistent"] is False
    assert counter.call_count == 4
    assert status["owner"] is None
    assert status["abandoned_ids"] == ["unsettled-attempt"]
    assert status["attempts"] == {"started": 4, "finished": 3}
    assert _tree(path) == before


def test_status_retries_once_and_accepts_a_stable_generation(council_run):
    path, resume, _calls, _fake, _run = council_run
    before = _tree(path)
    with mock.patch.object(_rundir, "_last_generation", side_effect=[1, 2, 2, 2]) as counter:
        rc, status = _status(resume)
    assert rc == 0 and status["consistent"] is True
    assert status["current_generation"] == 2 and counter.call_count == 4
    assert _tree(path) == before


@pytest.mark.parametrize("change", ["mode", "run_id", "missing_run_id", "missing"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
@pytest.mark.parametrize("operation", ["status", "resume"], ids=['p002_case_001', 'p002_case_002'])
def test_invalid_initial_receipt_refuses_before_ownership_or_dispatch(council_run, change, operation):
    path, resume, _calls, _fake, run = council_run
    receipt_path = path / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if change == "missing":
        receipt_path.unlink()
    else:
        if change == "missing_run_id":
            receipt.pop("run_id")
        else:
            receipt[change] = "foreign"
        _rundir.atomic_write_json(str(receipt_path), receipt)
    before = _tree(path)
    with mock.patch.object(_rundir, "acquire_owner") as acquire, \
            mock.patch.object(_council, "_dispatch") as dispatch:
        rc, result = _status(resume) if operation == "status" else run(resume)
    assert rc == 1 and result["status"] == "error"
    assert "members" not in result
    acquire.assert_not_called()
    dispatch.assert_not_called()
    assert _tree(path) == before


def test_resume_receipt_replacement_before_ownership_refuses(council_run):
    path, resume, _calls, _fake, run = council_run
    original_load = _loader.load_agent
    replacement = json.loads((path / "receipt.json").read_text(encoding="utf-8"))
    replacement["question"] = "Changed input"
    after_replacement = {}

    def replace_during_preflight(*args, **kwargs):
        loaded = original_load(*args, **kwargs)
        _rundir.atomic_write_json(str(path / "receipt.json"), replacement)
        after_replacement.update(_tree(path))
        return loaded

    with mock.patch.object(_loader, "load_agent", side_effect=replace_during_preflight), \
            mock.patch.object(_rundir, "acquire_owner") as acquire, \
            mock.patch.object(_council, "_dispatch") as dispatch:
        rc, result = run(resume)
    assert rc == 1 and result["status"] == "error"
    acquire.assert_not_called()
    dispatch.assert_not_called()
    assert _tree(path) == after_replacement


@pytest.mark.parametrize("field", ["mode", "run_id", "question"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003'])
def test_resume_receipt_replacement_under_owner_refuses_before_journal_or_work(council_run, field):
    path, resume, _calls, _fake, run = council_run
    generation = _rundir._last_generation(str(path))
    before = _tree(path)
    original_acquire = _rundir.acquire_owner

    def replace_after_acquire(*args, **kwargs):
        owner = original_acquire(*args, **kwargs)
        receipt = json.loads((path / "receipt.json").read_text(encoding="utf-8"))
        receipt[field] = "replacement"
        _rundir.atomic_write_json(str(path / "receipt.json"), receipt)
        return owner

    with mock.patch.object(_rundir, "acquire_owner", side_effect=replace_after_acquire), \
            mock.patch.object(_rundir, "journal_read") as journal, \
            mock.patch.object(_rundir, "journal_repair") as repair, \
            mock.patch.object(_council, "_dispatch") as dispatch:
        rc, result = run(resume)
    assert rc == 1 and result["status"] == "error"
    journal.assert_not_called()
    repair.assert_not_called()
    dispatch.assert_not_called()
    assert _rundir.read_owner(str(path)) is None
    assert _rundir._last_generation(str(path)) == generation + 1
    after = _tree(path)
    # Acquisition keeps its own generation evidence; the replacement belongs
    # to the simulated concurrent writer. Existing work evidence is untouched.
    for name, raw in before.items():
        if name != "receipt.json" and name != _rundir.GENERATION_FILE:
            assert after[name] == raw


def test_status_receipt_identity_replaced_during_scan_refuses(council_run):
    path, resume, _calls, _fake, _run = council_run
    original_read = _rundir.journal_read
    after_replacement = {}

    def replace_during_scan(*args, **kwargs):
        records = original_read(*args, **kwargs)
        receipt = json.loads((path / "receipt.json").read_text(encoding="utf-8"))
        receipt["run_id"] = "foreign"
        _rundir.atomic_write_json(str(path / "receipt.json"), receipt)
        after_replacement.update(_tree(path))
        return records

    with mock.patch.object(_rundir, "journal_read", side_effect=replace_during_scan):
        rc, result = _status(resume)
    assert rc == 1 and result["error_kind"] == "invalid_receipt"
    assert "members" not in result
    assert _tree(path) == after_replacement

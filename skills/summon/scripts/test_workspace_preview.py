"""Public workspace preview labels with provider-free, synthetic state."""
import json
import os
import subprocess
import sys
from pathlib import Path

import _workspace_entry
from _spawn import run_flags
from test_workspace_entry import _args, _free_port, _versioned_plan


def _cli(*args):
    script = Path(__file__).with_name("run_subagent.py")
    return subprocess.run(
        [sys.executable, str(script), "workspace", *args],
        cwd=str(script.parent), capture_output=True, text=True, timeout=20,
        env={**os.environ, "SUMMON_TELEMETRY": "0", "PYTHONDONTWRITEBYTECODE": "1"},
        **run_flags(),
    )


def _plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_versioned_plan()), encoding="utf-8")
    return path


def _snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
            for path in root.rglob("*")}


def test_workspace_cli_help_explicitly_labels_preview():
    result = _cli("--help")
    assert result.returncode == 0
    assert "Workspace preview." in result.stdout
    assert "workspace create RUN_ID" in result.stdout
    assert "workspace open RUN_ID" in result.stdout
    assert "workspace inspect RUN_ID" in result.stdout
    assert "workspace refresh RUN_ID" in result.stdout
    assert "control-token-file" in result.stdout


def test_workspace_page_does_not_throw_on_unrenderable_journal_time():
    from _workspace_page import page_bytes
    page = page_bytes(nonce="test-nonce-123456").decode("utf-8")
    assert "8640000000000" in page
    assert "event time unavailable" in page
    assert "Journal recorded at" in page


def test_workspace_cli_create_and_inspect_label_preview_without_inspection_mutation(tmp_path):
    runs = tmp_path / "runs"
    result = _cli("create", "preview-run", "--plan", str(_plan(tmp_path)),
                  "--runs-root", str(runs))
    assert result.returncode == 0
    created = json.loads(result.stdout)
    assert created["preview"] is True
    assert created["status"] == "created"
    assert created["mode"] == "create"
    assert created["run_id"] == "preview-run"
    assert created["provider_calls"] == created["workers_started"] == 0
    assert created["task_count"] == created["operator_target_count"] == 1
    before = _snapshot(runs)
    result = _cli("inspect", "preview-run", "--runs-root", str(runs))
    assert result.returncode == 0
    inspected = json.loads(result.stdout)
    assert inspected["preview"] is True
    assert inspected["status"] == "success"
    assert inspected["mode"] == "inspect"
    assert inspected["run_id"] == created["run_id"]
    assert inspected["workspace_id"] == created["workspace_id"]
    assert inspected["task_count"] == 1
    assert inspected["provider_calls"] == inspected["workers_resumed"] == 0
    assert inspected["mutation"] == "none"
    assert _snapshot(runs) == before
    assert not any(runs.rglob("workspace-host.sock"))


def test_workspace_open_public_ready_and_stopped_outputs_label_preview(tmp_path, monkeypatch, capsys):
    runs = tmp_path / "runs"
    _workspace_entry.create_workspace_from_plan(str(runs), "preview-open", str(_plan(tmp_path)))

    def interrupt(_seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(_workspace_entry.time, "sleep", interrupt)
    code = _workspace_entry.run_command(_args(
        workspace_action="open", workspace_run_id="preview-open",
        workspace_runs_root=str(runs), workspace_port=_free_port(),
        workspace_interactive=True,
    ))
    assert code == 0
    captured = capsys.readouterr()
    ready, stopped = [json.loads(line) for line in captured.out.splitlines()]
    assert ready["preview"] is stopped["preview"] is True
    assert ready["status"] == "ready"
    assert ready["mode"] == "open"
    assert ready["run_id"] == "preview-open"
    assert ready["qualification"] == "provider_inert_reopen"
    assert ready["provider_calls"] == ready["workers_started"] == ready["workers_resumed"] == 0
    assert "bootstrap_code" not in ready
    assert "Summon workspace bootstrap code (one-time, keep private): " in captured.err
    bootstrap = captured.err.strip().split(": ", 1)[1]
    assert bootstrap not in captured.out
    assert stopped["status"] == "stopped"
    assert stopped["cleanup"] == "complete"
    assert stopped["lifecycle"] == "foreground_owned"
    assert stopped["provider_calls"] == stopped["workers_started"] == stopped["workers_resumed"] == 0

"""Provider-inert tests for the approved fleet runtime bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from _builder import AgentInvocation
import _cli
import _evidence
import _executor
import _fleet
import _fleet_activation
import _fleet_approval
import _fleet_dispatch
import _fleet_runtime
import _loader
import run_subagent as dispatcher


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _approved_lane_files(tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "alpha.md").write_text(
        "---\nrun-agent: claude\nprovider: anthropic\n"
        "permission: read-only\nmodel: frontier-alpha\n"
        "lifecycle: active\n---\n# Alpha\nReview the task.\n",
        encoding="utf-8")
    agents = _loader.list_agents(str(agents_dir))
    _report, fleet, plan = _fleet.proposal(
        lane="review", seats=["alpha"], agents=agents, cwd=str(tmp_path),
        permission_ceiling="read-only", data_boundary="local_sanitized",
        corrective={"contract_repair": False, "retry": False,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": False, "payg": False,
               "max_provider_contacts": 1, "max_billable_attempts": 0,
               "max_parallel": 1})
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(name, raising=False)
    approval = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    fleet_file = tmp_path / "fleet.json"
    fleet_file.write_text(json.dumps(fleet), encoding="utf-8")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Review this provider-free fixture.", encoding="utf-8")
    return {
        "agents_dir": agents_dir, "fleet_file": fleet_file,
        "prompt_file": prompt_file,
        "approval_id": approval["approval"]["approval_id"],
    }


def test_lane_flag_exists_only_on_explicit_dispatch_subcommand():
    rewritten, mode = _cli.rewrite_subcommand([
        "dispatch", "--lane", "review", "--prompt-file", "task.txt"])
    assert mode is None
    assert rewritten[:2] == ["--fleet-dispatch-lane-internal", "review"]
    parser = _cli.build_parser("test", 1)
    assert parser.parse_args(rewritten).fleet_dispatch_lane == "review"
    with pytest.raises(SystemExit):
        parser.parse_args(["--lane", "review"])
    run_rewritten, mode = _cli.rewrite_subcommand(["run", "--lane", "review"])
    assert mode is None
    with pytest.raises(SystemExit):
        parser.parse_args(run_rewritten)
    for argv in (
            ["--fleet-dispatch-lane-internal", "review"],
            ["run", "--fleet-dispatch-lane-internal=review"],
            ["dispatch", "--fleet-dispatch-lane-internal", "review"]):
        _rewritten, rejected = _cli.rewrite_subcommand(argv)
        assert rejected and rejected.startswith("error:")


@pytest.mark.parametrize("mode_args", [
    ["--manifest", "jobs.json"],
    ["--council", "--question", "decide"],
    ["--deliberate", "--question", "decide"],
    ["--chat-action", "list"],
    ["--swarm-action", "status"],
    ["--jobs-list"],
    ["--usage-action", "status"],
    ["--telemetry-status"],
    ["--fleet-action", "inspect"],
])
def test_lane_is_rejected_by_fanout_whitelist_before_handler(mode_args):
    argv, mode = _cli.rewrite_subcommand([
        "dispatch", "--lane", "review", *mode_args])
    assert mode is None
    parser = _cli.build_parser("test", 1)
    args = parser.parse_args(argv)
    refusal = _cli.unsupported_mode_flags(argv, args)
    assert refusal is not None
    assert "--lane" in refusal
    assert "fleet-dispatch-lane" not in refusal


def test_main_rejects_lane_manifest_before_manifest_handler(
        monkeypatch, capsys):
    import _manifest

    entered = []
    monkeypatch.setattr(
        _manifest, "run_manifest",
        lambda *_a, **_k: entered.append(True) or 99)
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--manifest", "must-not-open.json"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 1
    assert entered == []
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "--lane" in envelope["error"]
    assert "fleet-dispatch-lane" not in envelope["error"]
    assert envelope["provider_contacted"] is False


def test_main_rejects_lane_swarm_before_swarm_handler(monkeypatch, capsys):
    import _swarm_coordinator

    entered = []
    monkeypatch.setattr(
        _swarm_coordinator, "run_command",
        lambda *_a, **_k: entered.append(True) or 99)
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--swarm-action", "status", "--swarm-run-id", "never-read"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 1
    assert entered == []
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "--lane" in envelope["error"]
    assert "fleet-dispatch-lane" not in envelope["error"]
    assert envelope["provider_contacted"] is False


def test_main_rejects_lane_doctor_before_doctor_handler(monkeypatch, capsys):
    import _doctor

    entered = []
    monkeypatch.setattr(
        _doctor, "doctor", lambda *_a, **_k: entered.append(True) or {})
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--doctor"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 1
    assert entered == []
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "dispatch --lane cannot be combined" in envelope["error"]
    assert envelope["provider_contacted"] is False


@pytest.fixture
def approved(tmp_path, monkeypatch):
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(name, raising=False)
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "alpha.md").write_text(
        "---\nrun-agent: claude\nprovider: anthropic\n"
        "permission: read-only\nmodel: frontier-alpha\n"
        "lifecycle: active\n---\n# Alpha\nReview the task.\n",
        encoding="utf-8")
    agents = _loader.list_agents(str(agents_dir))
    _report, fleet, plan = _fleet.proposal(
        lane="review", seats=["alpha"], agents=agents, cwd=str(tmp_path),
        permission_ceiling="read-only", data_boundary="local_sanitized",
        corrective={"contract_repair": False, "retry": False,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": False, "payg": False,
               "max_provider_contacts": 1, "max_billable_attempts": 0,
               "max_parallel": 1})
    catalog, digest, _unavailable = _fleet.catalog_snapshot(agents)
    assert digest == _evidence.verify(plan)["catalog_sha256"]
    receipt = _fleet_approval.approve(
        fleet=fleet, plan=plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    fleet_file = tmp_path / "fleet.json"
    fleet_file.write_text(json.dumps(fleet), encoding="utf-8")
    invocation = AgentInvocation(
        cli="claude", prompt="review this", cwd=str(tmp_path),
        permission="read-only", transport="subprocess",
        model="frontier-alpha", model_source="agent",
        model_exact_required=True, model_exact_source="agent",
        output_contract="report")
    runtime = _fleet_runtime.prepare(
        approval_id=receipt["approval"]["approval_id"],
        fleet_path=str(fleet_file), agents_dir=str(agents_dir),
        strict_agents_dir=False, lane_name="review",
        cwd=str(tmp_path), data_proof="operator_attested",
        invocation=invocation, agent_definition_sha256=_sha("definition"),
        request_identity_sha256=_sha("request"))
    return runtime


def _evidence_for(runtime):
    return _executor._subprocess_launch_evidence(
        "claude", ["--print", runtime.invocation.prompt], runtime.cwd,
        {"PATH": "private", "TOKEN": "never projected"},
        backend="claude")


def _result(*, status="success", contacted=True):
    return {
        "status": status, "execution_status": status,
        "result": "STATUS: COMPLETE\nVERDICT: PASS\nHANDOFF: done",
        "raw_backend_exit_code": 0, "normalized_exit_code": 0,
        "provider_contacted": contacted, "report_ok": True,
        "model": {"requested": "frontier-alpha",
                  "targeted": "frontier-alpha", "served": None},
        "served_model_evidence": "absent",
    }


def test_runtime_claim_spawn_reap_terminalizes_once_without_private_evidence(approved):
    control = approved.control()
    control.before_provider_launch(_evidence_for(approved))
    control.spawned(object())
    control.reaped(object())
    result = approved.finalize(_result())
    claim = _fleet_dispatch.get_claim(approved.reservation)
    assert claim["phase"] == "terminal"
    assert claim["provider_contacted"] is True
    assert claim["contact_slot_consumed"] is True
    public = result["fleet_dispatch"]
    encoded = json.dumps(public, sort_keys=True)
    assert public["claim"]["phase"] == "terminal"
    assert "command_sha256" not in encoded
    assert "argv_sha256" not in encoded
    assert "env_names_sha256" not in encoded
    assert "env_sha256" not in encoded


def test_runtime_pre_spawn_failure_terminalizes_without_provider_contact(approved):
    control = approved.control()
    control.before_provider_launch(_evidence_for(approved))
    control.pre_spawn_failed(FileNotFoundError("private executable"))
    result = approved.finalize(_result(status="error", contacted=False))
    assert result["fleet_dispatch"]["claim"]["phase"] == "terminal"
    assert result["fleet_dispatch"]["claim"]["provider_contacted"] is False


def test_runtime_provider_free_refusal_releases_reserved_capacity(approved):
    result = approved.finalize(_result(status="error", contacted=False))
    claim = _fleet_dispatch.get_claim(approved.reservation)
    assert claim["phase"] == "cancelled_pre_spawn"
    assert claim["contact_slot_reserved"] is False
    assert claim["contact_slot_consumed"] is False
    assert result["fleet_dispatch"]["claim"]["provider_contacted"] is False


def test_runtime_cleanup_failure_is_explicit_and_never_loses_reservation(
        approved, monkeypatch):
    monkeypatch.setattr(
        _fleet_dispatch, "cancel_pre_spawn",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("ledger unavailable")))
    result = approved.failure(ValueError("provider-free build failure"))
    assert result["status"] == "error"
    assert result["error_kind"] == "fleet_dispatch_reconciliation_failed"
    assert result["attempts"] == 0
    assert result["attempt_status"] == "not_run"
    assert result["provider_contacted"] is False
    assert result["fleet_dispatch"]["claim"]["phase"] == "reserved"


def test_runtime_terminalization_failure_preserves_known_provider_contact(
        approved, monkeypatch):
    control = approved.control()
    control.before_provider_launch(_evidence_for(approved))
    handle = object()
    control.spawned(handle)
    control.reaped(handle)
    monkeypatch.setattr(
        _fleet_dispatch, "mark_terminal",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("terminal write failed")))
    monkeypatch.setattr(
        _fleet_dispatch, "mark_indeterminate",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("reconcile write failed")))
    result = approved.failure(OSError("dispatcher failed after reap"))
    assert result["status"] == "error"
    assert result["error_kind"] == "fleet_dispatch_reconciliation_failed"
    assert result["attempts"] == 1
    assert result["attempt_status"] == "indeterminate"
    assert result["provider_contacted"] is True
    assert result["fleet_dispatch"]["claim"]["phase"] == "reaped"


def test_runtime_ambiguous_post_claim_failure_is_not_usable(approved):
    control = approved.control()
    control.before_provider_launch(_evidence_for(approved))
    control.launch_indeterminate(RuntimeError("private detail"))
    result = approved.finalize(_result(status="error", contacted=None))
    assert result["status"] == "error"
    assert result["error_kind"] == "fleet_dispatch_indeterminate"
    assert result["result_usable"] is False
    assert result["fleet_dispatch"]["claim"]["phase"] == "indeterminate"
    assert "private detail" not in json.dumps(result)


def test_launch_evidence_binds_exact_plan_without_exposing_values(tmp_path):
    one = _executor._subprocess_launch_evidence(
        r"C:\private\claude.cmd", ["--prompt", "TOP SECRET"],
        str(tmp_path), {"API_TOKEN": "first"}, backend="claude")
    two = _executor._subprocess_launch_evidence(
        r"C:\private\claude.cmd", ["--prompt", "TOP SECRET"],
        str(tmp_path), {"API_TOKEN": "second"}, backend="claude")
    three = _executor._subprocess_launch_evidence(
        r"C:\private\claude.cmd", ["--prompt", "different"],
        str(tmp_path), {"API_TOKEN": "second", "NEW_NAME": "value"},
        backend="claude")
    encoded = json.dumps(one, sort_keys=True)
    assert one["env_names_sha256"] == two["env_names_sha256"]
    assert one["env_sha256"] != two["env_sha256"]
    assert one["argv_sha256"] != three["argv_sha256"]
    assert one["env_names_sha256"] != three["env_names_sha256"]
    assert "TOP SECRET" not in encoded
    assert "private" not in encoded.lower()
    assert "first" not in encoded


def test_approved_lane_dry_run_resolves_one_seat_without_reservation(
        tmp_path, monkeypatch):
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "alpha.md").write_text(
        "---\nrun-agent: claude\nprovider: anthropic\n"
        "permission: read-only\nmodel: frontier-alpha\n"
        "lifecycle: active\n---\n# Alpha\nReview the task.\n",
        encoding="utf-8")
    agents = _loader.list_agents(str(agents_dir))
    assert any(item["name"] == "alpha" for item in agents)
    _report, fleet, _plan = _fleet.proposal(
        lane="review", seats=["alpha"], agents=agents, cwd=str(tmp_path),
        permission_ceiling="read-only", data_boundary="local_sanitized",
        corrective={"contract_repair": False, "retry": False,
                    "fallback": False, "continuation": False},
        spend={"subscription": True, "credit": False, "payg": False,
               "max_provider_contacts": 1, "max_billable_attempts": 0,
               "max_parallel": 1})
    root = tmp_path / "private"
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_STORE", str(root / "store.json"))
    monkeypatch.setenv("SUMMON_FLEET_APPROVAL_KEY", str(root / "store.key"))
    approval = _fleet_approval.approve(
        fleet=fleet, plan=_plan, lane_name="review",
        expires_in_seconds=3600, expected_generation=0)
    fleet_file = tmp_path / "fleet.json"
    fleet_file.write_text(json.dumps(fleet), encoding="utf-8")
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Review this provider-free fixture.", encoding="utf-8")
    script = Path(__file__).with_name("run_subagent.py")
    completed = subprocess.run(
        [sys.executable, str(script), "dispatch", "--lane", "review",
         "--fleet-file", str(fleet_file), "--fleet-approval-id",
         approval["approval"]["approval_id"], "--fleet-data-proof",
         "operator_attested", "--prompt-file", str(prompt_file),
         "--cwd", str(tmp_path), "--agents-dir", str(agents_dir),
         "--dry-run", "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=45,
        env=os.environ.copy())
    assert completed.returncode == 0, completed.stdout + completed.stderr
    envelope = json.loads(completed.stdout)
    assert envelope["provider_contacted"] is False
    assert envelope["agent"] == "alpha"
    assert envelope["fleet_dispatch"]["authorization"] == "approved_lane"
    assert envelope["fleet_dispatch"]["seat"] == "alpha"
    assert envelope["fleet_dispatch"]["attempt_policy"]["max_physical_attempts"] == 1
    assert not os.path.exists(_fleet_dispatch.ledger_path(
        approval["approval"]["approval_id"]))


def test_main_lane_path_calls_one_controlled_executor_and_no_corrective_path(
        tmp_path, monkeypatch, capsys):
    material = _approved_lane_files(tmp_path, monkeypatch)
    calls = []
    continuation_contexts = []
    original_emit = dispatcher._emit

    def recording_emit(*args, **kwargs):
        continuation_contexts.append(kwargs.get("continuation_context"))
        return original_emit(*args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden orchestration path entered")

    def fake_execute(invocation, **kwargs):
        calls.append(invocation)
        control = kwargs.get("launch_control")
        assert control is not None
        control.before_provider_launch(_executor._subprocess_launch_evidence(
            "claude", ["--print", invocation.prompt], invocation.cwd,
            {"PATH": "fixture"}, backend="claude"))
        handle = object()
        control.spawned(handle)
        control.reaped(handle)
        return {
            "status": "success", "execution_status": "success",
            "result": "STATUS: COMPLETE\nVERDICT: PASS\nHANDOFF: done",
            "exit_code": 0, "raw_backend_exit_code": 0,
            "normalized_exit_code": 0, "provider_contacted": True,
            "report_ok": True, "result_usable": True,
            "served_model_evidence": "reported",
            "model": {"requested": "frontier-alpha",
                      "targeted": "frontier-alpha",
                      "served": "frontier-alpha"},
        }

    monkeypatch.setattr(dispatcher, "execute_agent", fake_execute)
    monkeypatch.setattr(dispatcher, "_emit", recording_emit)
    monkeypatch.setattr(dispatcher, "_preflight_backend", lambda *_a, **_k: None)
    for name in ("_dispatch_with_retries", "_apply_schema",
                 "_apply_contract_repair", "_run_gate", "_spawn_background"):
        monkeypatch.setattr(dispatcher, name, forbidden)
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--fleet-file", str(material["fleet_file"]),
        "--fleet-approval-id", material["approval_id"],
        "--fleet-data-proof", "operator_attested",
        "--prompt-file", str(material["prompt_file"]),
        "--cwd", str(tmp_path), "--agents-dir", str(material["agents_dir"]),
        "--json"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 0
    assert len(calls) == 1
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["status"] == "success"
    assert envelope["fleet_dispatch"]["claim"]["phase"] == "terminal"
    assert envelope["fleet_dispatch"]["claim"]["provider_contacted"] is True
    assert envelope["fleet_dispatch"]["claim"]["contact_slot_consumed"] is True
    assert continuation_contexts[-1] is None


def test_main_lane_rejects_hidden_job_file_without_provider_contact(
        tmp_path, monkeypatch, capsys):
    material = _approved_lane_files(tmp_path, monkeypatch)
    job_file = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--fleet-file", str(material["fleet_file"]),
        "--fleet-approval-id", material["approval_id"],
        "--fleet-data-proof", "operator_attested",
        "--prompt-file", str(material["prompt_file"]),
        "--cwd", str(tmp_path), "--agents-dir", str(material["agents_dir"]),
        "--job-file", str(job_file), "--json"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 1
    assert capsys.readouterr().out == ""
    envelope = json.loads(job_file.read_text(encoding="utf-8"))
    assert envelope["error_kind"] == "fleet_activation_usage_invalid"
    assert envelope["provider_contacted"] is False
    assert envelope["attempts"] == 0
    assert "continuation" not in envelope


def test_prepare_does_not_run_redundant_post_reservation_preflight(
        tmp_path, monkeypatch):
    material = _approved_lane_files(tmp_path, monkeypatch)
    invocation = AgentInvocation(
        cli="claude", prompt="Review this provider-free fixture.", cwd=str(tmp_path),
        permission="read-only", transport="subprocess",
        model="frontier-alpha", model_source="agent",
        model_exact_required=True, model_exact_source="agent",
        output_contract="report")
    monkeypatch.setattr(
        _fleet_dispatch, "preflight_activation_dispatch",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("redundant post-reservation preflight called")))
    runtime = _fleet_runtime.prepare(
        approval_id=material["approval_id"],
        fleet_path=str(material["fleet_file"]),
        agents_dir=str(material["agents_dir"]), strict_agents_dir=False,
        lane_name="review", cwd=str(tmp_path),
        data_proof="operator_attested", invocation=invocation,
        agent_definition_sha256=_sha("definition"),
        request_identity_sha256=_sha("request"))
    claim = _fleet_dispatch.get_claim(runtime.reservation)
    assert claim["phase"] == "reserved"
    runtime.finalize(_result(status="error", contacted=False))


def test_main_lane_rereads_fleet_at_boundary_and_refuses_drift_before_contact(
        tmp_path, monkeypatch, capsys):
    material = _approved_lane_files(tmp_path, monkeypatch)
    provider_contacts = []

    def fake_execute(invocation, **kwargs):
        material["fleet_file"].write_text("{}", encoding="utf-8")
        kwargs["launch_control"].before_provider_launch(
            _executor._subprocess_launch_evidence(
                "claude", ["--print", invocation.prompt], invocation.cwd,
                {"PATH": "fixture"}, backend="claude"))
        provider_contacts.append(True)
        raise AssertionError("provider boundary should have refused")

    monkeypatch.setattr(dispatcher, "execute_agent", fake_execute)
    monkeypatch.setattr(dispatcher, "_preflight_backend", lambda *_a, **_k: None)
    monkeypatch.setattr(sys, "argv", [
        str(Path(dispatcher.__file__)), "dispatch", "--lane", "review",
        "--fleet-file", str(material["fleet_file"]),
        "--fleet-approval-id", material["approval_id"],
        "--fleet-data-proof", "operator_attested",
        "--prompt-file", str(material["prompt_file"]),
        "--cwd", str(tmp_path), "--agents-dir", str(material["agents_dir"]),
        "--json"])
    with pytest.raises(SystemExit) as completed:
        dispatcher.main()
    assert completed.value.code == 1
    assert provider_contacts == []
    envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert envelope["provider_contacted"] is False
    assert envelope["fleet_dispatch"]["claim"]["phase"] == "cancelled_pre_spawn"
    assert envelope["fleet_dispatch"]["claim"]["contact_slot_consumed"] is False

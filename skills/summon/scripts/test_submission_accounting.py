import argparse
import hashlib
import json
import os
import sys
from unittest import mock

import pytest

import _executor
import _apibackend
import _background
import _jobs
import _submission_accounting as accounting
import run_subagent
from _builder import AgentInvocation, submission_payload_parts


def _estimate(inv, boundary="subprocess_adapter_payload"):
    return accounting.estimate_payload(
        submission_payload_parts(inv, boundary), boundary=boundary)


def _record(inv, attempt_id, usage, observation, *, state="submitted"):
    envelope = {"provider_contacted": state == "submitted", "usage": usage,
                "usage_observation": observation}
    return accounting.private_record(
        attempt_id=attempt_id, attempt_kind=inv.attempt_kind,
        attempt_ordinal=inv.attempt_ordinal,
        parent_attempt_id=inv.parent_attempt_id,
        request_sha256=inv.request_sha256, estimate=_estimate(inv),
        envelope=envelope, submission_state=state)


def test_claude_fresh_and_resume_estimates_match_actual_visible_payload_scope():
    fresh = AgentInvocation(cli="claude", prompt="task", cwd="C:/work",
                            system_context="agent rules", request_sha256="a" * 64)
    resumed = AgentInvocation(cli="claude", prompt="task", cwd="C:/work",
                              system_context="agent rules", resume_id="session",
                              request_sha256="a" * 64)
    fresh_estimate = _estimate(fresh)
    resume_estimate = _estimate(resumed)
    assert fresh_estimate["represented_bytes"] > len(fresh.prompt.encode())
    assert fresh_estimate["material_sha256"] != resume_estimate["material_sha256"]
    assert fresh_estimate["represented_bytes"] != len(resumed.system_context.encode())
    assert fresh_estimate["complete_provider_input"] is False
    assert fresh_estimate["scope"] == "summon_visible_adapter_input"


def test_retries_count_repeated_material_and_missing_usage_without_leaking_identity():
    base = AgentInvocation(cli="codex", prompt="same", cwd=".",
                           system_context="rules", request_sha256="b" * 64)
    first = _record(base, "1" * 32,
                    {"input_tokens": 10, "output_tokens": 0,
                     "total_tokens": 10, "cache_read_tokens": 0},
                    {"scope": "attempt_total", "source": "synthetic_terminal"})
    second_inv = AgentInvocation(**{
        **base.__dict__, "attempt_kind": "transient_retry", "attempt_ordinal": 2,
        "parent_attempt_id": "1" * 32})
    second = _record(second_inv, "2" * 32,
                     {"output_tokens": 5},
                     {"scope": "last_step_snapshot", "source": "synthetic_step"})
    summary = accounting.public_summary([first, first, second])
    assert summary["physical_submissions"] == 2
    assert summary["candidate_records"] == 2
    assert summary["estimate"]["covered_submissions"] == 2
    assert summary["reported"]["output_tokens"] == {
        "known_subtotal": 0, "covered_attempts": 1, "missing_attempts": 1,
        "covered_submissions": 1,
        "missing_submissions": 1, "completeness": "partial"}
    assert summary["reported"]["cache_read_tokens"]["known_subtotal"] == 0
    assert summary["reported"]["cache_write_tokens"]["known_subtotal"] is None
    assert summary["unknown_spend"] is True
    public = json.dumps(summary, sort_keys=True)
    for private in ("11111111", "22222222", "material_sha256",
                    "request_sha256", "prompt", "account", "launch_observation"):
        assert private not in public


def test_public_summary_ignores_numeric_metrics_not_marked_reported():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="d" * 64)
    record = _record(inv, "3" * 32, {"total_tokens": 7},
                     {"scope": "attempt_total", "source": "synthetic"})
    record["reported"]["field_state"]["total_tokens"] = "malformed"
    summary = accounting.public_summary([record])
    assert summary["reported"]["total_tokens"] == {
        "known_subtotal": None, "covered_attempts": 0, "missing_attempts": 1,
        "covered_submissions": 0, "missing_submissions": 1,
        "completeness": "partial"}


def test_public_summary_preserves_reported_metric_in_malformed_overall_record():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="e" * 64)
    record = _record(inv, "4" * 32, {"total_tokens": 7},
                     {"scope": "attempt_total", "source": "synthetic"})
    record["reported"]["field_state"]["input_tokens"] = "malformed"
    record["reported"]["completeness"] = "malformed"
    summary = accounting.public_summary([record])
    assert summary["reported"]["total_tokens"]["known_subtotal"] == 7
    assert summary["reported"]["total_tokens"]["covered_attempts"] == 1
    assert summary["reported"]["input_tokens"]["known_subtotal"] is None


def test_malformed_usage_is_unknown_while_explicit_zero_is_measured():
    reported = accounting.normalize_reported_usage(
        {"input_tokens": True, "output_tokens": -1, "total_tokens": float("nan"),
         "cache_read_tokens": 0},
        {"scope": "attempt_total", "source": "synthetic_terminal"})
    assert reported["field_state"]["input_tokens"] == "malformed"
    assert reported["field_state"]["output_tokens"] == "malformed"
    assert reported["field_state"]["total_tokens"] == "malformed"
    assert reported["metrics"]["cache_read_tokens"] == 0
    assert reported["field_state"]["cache_write_tokens"] == "absent"
    assert reported["completeness"] == "malformed"


def test_contact_does_not_infer_submission_or_local_process_and_uncertainty_propagates():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="a" * 64)
    record = accounting.private_record(
        attempt_id="a" * 32, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv),
        envelope={"provider_contacted": True, "uncertain_spend": True})
    assert record["submission_state"] == "indeterminate"
    assert record["contact"]["local_process_created"] is None
    assert record["uncertain_spend"] is True
    assert record["unknown_spend"] is True


def test_metric_completeness_includes_indeterminate_physical_attempts():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="b" * 64)
    known = _record(inv, "b" * 32, {"total_tokens": 4},
                    {"scope": "attempt_total", "source": "synthetic"})
    unknown = accounting.private_record(
        attempt_id="c" * 32, attempt_kind="transient_retry", attempt_ordinal=2,
        parent_attempt_id="b" * 32, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={"provider_contacted": True})
    summary = accounting.public_summary([known, unknown])
    assert summary["physical_submissions"] == 1
    assert summary["physical_attempts"] == 2
    assert summary["reported"]["total_tokens"] == {
        "known_subtotal": 4, "covered_attempts": 1, "missing_attempts": 1,
        "covered_submissions": 1,
        "missing_submissions": 1, "completeness": "partial"}


def test_background_payg_accounting_grant_tracks_existing_consent_surfaces(monkeypatch):
    from argparse import Namespace
    args = Namespace(
        retries=0, cli="openai-compat", no_acp_fallback=False,
        json_schema=None, no_contract_repair=False, allow_payg=False)
    monkeypatch.delenv("SUMMON_FRESH_CONSENT_ONLY", raising=False)
    monkeypatch.setattr(_apibackend, "payg_consent_allowed",
                        lambda flag=False: bool(flag) or True)
    assert _background._submission_accounting_grants(args)["payg_fallback"] == 1
    monkeypatch.setenv("SUMMON_FRESH_CONSENT_ONLY", "1")
    assert _background._submission_accounting_grants(args)["payg_fallback"] == 0
    args.allow_payg = True
    assert _background._submission_accounting_grants(args)["payg_fallback"] == 1


def test_metric_completeness_includes_possible_prelaunch_attempts():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="a" * 64)
    known = _record(inv, "d" * 32, {"total_tokens": 4},
                    {"scope": "attempt_total", "source": "synthetic"})
    possible = accounting.private_record(
        attempt_id="e" * 32, attempt_kind="transient_retry", attempt_ordinal=2,
        parent_attempt_id="d" * 32, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={}, submission_state="possible")
    summary = accounting.public_summary([known, possible])
    assert summary["physical_submissions"] == 1
    assert summary["physical_attempts"] == 2
    assert summary["indeterminate_contact"] == 1
    assert summary["reported"]["total_tokens"] == {
        "known_subtotal": 4, "covered_attempts": 1, "missing_attempts": 1,
        "covered_submissions": 1,
        "missing_submissions": 1, "completeness": "partial"}
    assert summary["unknown_spend"] is True


def test_possible_only_attempt_is_incomplete_without_physical_submission():
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256="b" * 64)
    possible = accounting.private_record(
        attempt_id="f" * 32, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={}, submission_state="possible")
    summary = accounting.public_summary([possible])
    assert summary["physical_submissions"] == 0
    assert summary["physical_attempts"] == 1
    assert summary["indeterminate_contact"] == 1
    assert summary["reported"]["total_tokens"] == {
        "known_subtotal": None, "covered_attempts": 0, "missing_attempts": 1,
        "covered_submissions": 0,
        "missing_submissions": 1, "completeness": "partial"}
    assert summary["estimate"]["missing_attempts"] == 0
    assert summary["estimate"]["completeness"] == "complete"
    assert summary["unknown_spend"] is True


def test_retry_dispatch_retains_each_attempt_accounting(monkeypatch):
    root_request = "c" * 64
    inv = AgentInvocation(cli="codex", prompt="same", cwd=".",
                          system_context="rules", request_sha256=root_request)
    calls = []

    def fake_execute(attempt_inv, **_kwargs):
        calls.append(attempt_inv)
        n = len(calls)
        envelope = {
            "status": "error" if n == 1 else "success",
            "execution_status": "error" if n == 1 else "success",
            "provider_contacted": True, "attempts": 1,
            "submission_state": "submitted",
            "attempt_id": attempt_inv.attempt_id,
            "attempt_kind": attempt_inv.attempt_kind,
            "attempt_ordinal": attempt_inv.attempt_ordinal,
            "usage": {"total_tokens": 10 * n},
            "usage_observation": {"scope": "attempt_total", "source": "synthetic"},
        }
        envelope["submission_accounting"] = accounting.private_record(
            attempt_id=attempt_inv.attempt_id,
            attempt_kind=attempt_inv.attempt_kind,
            attempt_ordinal=attempt_inv.attempt_ordinal,
            parent_attempt_id=attempt_inv.parent_attempt_id,
            request_sha256=attempt_inv.request_sha256,
            estimate=_estimate(attempt_inv), envelope=envelope)
        return envelope

    monkeypatch.setattr(run_subagent, "execute_agent", fake_execute)
    monkeypatch.setattr(run_subagent.time, "sleep", lambda _n: None)
    args = argparse.Namespace(timeout=1000, debug_dir=None,
                              max_tool_output_bytes=None, retries=1,
                              gate_with=None)
    result = run_subagent._dispatch_with_retries(inv, args)
    accounting.attach_public_summary(result)
    assert [call.attempt_ordinal for call in calls] == [1, 2]
    assert calls[0].attempt_id != calls[1].attempt_id
    assert result["submission_summary"]["physical_submissions"] == 2
    assert result["submission_summary"]["reported"]["total_tokens"]["known_subtotal"] == 30


def test_background_owner_fence_interruption_and_no_lost_pid_update(tmp_path):
    job_id = "d" * 32
    nonce = "private-fence"
    inv = AgentInvocation(cli="codex", prompt="same", cwd=str(tmp_path),
                          system_context="rules", attempt_id=job_id,
                          request_sha256="e" * 64)
    _jobs.write_prepared(
        str(tmp_path), job_id, nonce=nonce, agent="synthetic",
        prompt_sha256=hashlib.sha256(b"same").hexdigest(), cwd=str(tmp_path),
        flags={"cli": "codex"}, summon={"version": "test"}, attempt_id=job_id)
    possible = accounting.private_record(
        attempt_id=job_id, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={}, submission_state="possible")
    with pytest.raises(PermissionError):
        _jobs.record_submission_prelaunch(
            str(tmp_path), job_id, nonce="wrong", accounting=possible)
    _jobs.record_submission_prelaunch(
        str(tmp_path), job_id, nonce=nonce, accounting=possible)
    interrupted = _jobs.read_json(_jobs.record_path(str(tmp_path), job_id))
    interrupted_public = _jobs.public_job_status({"job_id": job_id,
        "attempt_id": job_id, "state": "prepared", "trusted": True,
        "record": interrupted, "result": None})
    assert interrupted_public["submission_summary"]["unknown_spend"] is True
    assert "request_sha256" not in json.dumps(interrupted_public["submission_summary"])

    _jobs.update_spawned(str(tmp_path), job_id, 1234)
    settled = _record(inv, job_id,
                      {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                       "reasoning_tokens": 0, "cache_read_tokens": 0,
                       "cache_write_tokens": 0},
                      {"scope": "attempt_total", "source": "synthetic_terminal"})
    _jobs.settle_submission(str(tmp_path), job_id, nonce=nonce, accounting=settled)
    assert _jobs.settle_submission(
        str(tmp_path), job_id, nonce=nonce, accounting=settled) == settled
    final = _jobs.read_json(_jobs.record_path(str(tmp_path), job_id))
    assert final["pid"] == 1234
    summary = accounting.public_summary(final["submission_accounting"])
    assert summary["unknown_spend"] is False
    assert summary["reported"]["total_tokens"]["known_subtotal"] == 0


def test_prelaunch_refusal_releases_unknown_without_creating_submission(tmp_path):
    job_id = "f" * 32
    nonce = "private-fence"
    inv = AgentInvocation(cli="codex", prompt="same", cwd=str(tmp_path),
                          system_context="rules", attempt_id=job_id,
                          request_sha256="a" * 64)
    _jobs.write_prepared(str(tmp_path), job_id, nonce=nonce, agent="synthetic",
                         prompt_sha256="0" * 64, cwd=str(tmp_path), flags={},
                         summon={}, attempt_id=job_id)
    possible = accounting.private_record(
        attempt_id=job_id, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={}, submission_state="possible")
    _jobs.record_submission_prelaunch(str(tmp_path), job_id, nonce=nonce,
                                      accounting=possible)
    refused = accounting.private_record(
        attempt_id=job_id, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={"provider_contacted": False},
        submission_state="not_submitted")
    _jobs.settle_submission(str(tmp_path), job_id, nonce=nonce, accounting=refused)
    summary = accounting.public_summary([refused])
    assert summary["physical_submissions"] == 0
    assert summary["not_submitted"] == 1
    assert summary["unknown_spend"] is False


def test_background_accounting_grant_accepts_one_authorized_retry_only(tmp_path):
    root_id = "4" * 32
    retry_id = "5" * 32
    nonce = "private-fence"
    base = AgentInvocation(cli="codex", prompt="same", cwd=str(tmp_path),
                           system_context="rules", attempt_id=root_id,
                           request_sha256="6" * 64)
    _jobs.write_prepared(
        str(tmp_path), root_id, nonce=nonce, agent="synthetic",
        prompt_sha256="0" * 64, cwd=str(tmp_path), flags={}, summon={},
        attempt_id=root_id,
        accounting_grants={"initial": 1, "transient_retry": 1})
    first_possible = accounting.private_record(
        attempt_id=root_id, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=base.request_sha256,
        estimate=_estimate(base), envelope={}, submission_state="possible")
    _jobs.record_submission_prelaunch(str(tmp_path), root_id, nonce=nonce,
                                      accounting=first_possible)
    first = _record(base, root_id, {"total_tokens": 1},
                    {"scope": "attempt_total", "source": "synthetic"})
    _jobs.settle_submission(str(tmp_path), root_id, nonce=nonce, accounting=first)

    retry = AgentInvocation(**{
        **base.__dict__, "attempt_id": retry_id, "attempt_kind": "transient_retry",
        "attempt_ordinal": 2, "parent_attempt_id": root_id})
    retry_possible = accounting.private_record(
        attempt_id=retry_id, attempt_kind="transient_retry", attempt_ordinal=2,
        parent_attempt_id=root_id, request_sha256=retry.request_sha256,
        estimate=_estimate(retry), envelope={}, submission_state="possible")
    _jobs.record_submission_prelaunch(str(tmp_path), root_id, nonce=nonce,
                                      accounting=retry_possible)
    second = _record(retry, retry_id, {"total_tokens": 2},
                     {"scope": "attempt_total", "source": "synthetic"})
    _jobs.settle_submission(str(tmp_path), root_id, nonce=nonce, accounting=second)
    record = _jobs.read_json(_jobs.record_path(str(tmp_path), root_id))
    assert accounting.public_summary(record["submission_accounting"])[
        "reported"]["total_tokens"]["known_subtotal"] == 3

    excess_id = "7" * 32
    excess = accounting.private_record(
        attempt_id=excess_id, attempt_kind="transient_retry", attempt_ordinal=3,
        parent_attempt_id=retry_id, request_sha256=retry.request_sha256,
        estimate=_estimate(retry), envelope={}, submission_state="possible")
    with pytest.raises(PermissionError):
        _jobs.record_submission_prelaunch(str(tmp_path), root_id, nonce=nonce,
                                          accounting=excess)


def test_aggregate_accounting_grants_refuse_before_first_contact(tmp_path):
    job_id = "8" * 32
    with pytest.raises(ValueError, match="64-record journal budget"):
        _jobs.write_prepared(
            str(tmp_path), job_id, nonce="bounded-grant-fence",
            agent="synthetic", prompt_sha256="0" * 64,
            cwd=str(tmp_path), flags={}, summon={}, attempt_id=job_id,
            accounting_grants={"initial": 1, "transient_retry": 63,
                               "payg_fallback": 1})
    assert not os.path.exists(_jobs.record_path(str(tmp_path), job_id))


def test_executor_synthetic_local_process_is_provider_inert_and_accounted(tmp_path):
    attempt_id = "9" * 32
    inv = AgentInvocation(cli="codex", prompt="task", cwd=str(tmp_path),
                          system_context="rules", attempt_id=attempt_id,
                          request_sha256="8" * 64)
    events = [
        json.dumps({"type": "thread.started", "thread_id": "synthetic-thread"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "synthetic"}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 3, "output_tokens": 1, "total_tokens": 4}}),
    ]
    script = "import sys;sys.stdout.write(" + repr("\n".join(events) + "\n") + ")"
    with mock.patch.object(_executor, "build_invocation_args",
                           return_value=(sys.executable, ["-c", script], None)):
        result = _executor.execute_agent(inv, timeout_ms=5000)
    assert result["provider_contacted"] is True
    assert result["submission_accounting"]["attempt"]["id"] == attempt_id
    assert result["submission_accounting"]["submission_state"] == "indeterminate"
    assert result["submission_accounting"]["contact"]["local_process_created"] is True
    assert result["submission_accounting"]["reported"]["observation_scope"] == "attempt_total"
    assert result["submission_accounting"]["estimate"]["represented_bytes"] > len(b"task")


def test_executor_synthetic_background_settles_existing_launch_record(tmp_path,
                                                                      monkeypatch):
    attempt_id = "3" * 32
    nonce = "private-fence"
    inv = AgentInvocation(cli="codex", prompt="task", cwd=str(tmp_path),
                          system_context="rules", attempt_id=attempt_id,
                          request_sha256="2" * 64)
    _jobs.write_prepared(
        str(tmp_path), attempt_id, nonce=nonce, agent="synthetic",
        prompt_sha256=hashlib.sha256(b"task").hexdigest(), cwd=str(tmp_path),
        flags={"cli": "codex"}, summon={}, attempt_id=attempt_id,
        accounting_grants={"initial": 1})
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", attempt_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    events = [
        json.dumps({"type": "thread.started", "thread_id": "synthetic-thread"}),
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "synthetic"}}),
        json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}}),
    ]
    script = "import sys;sys.stdout.write(" + repr("\n".join(events) + "\n") + ")"
    with mock.patch.object(_executor, "build_invocation_args",
                           return_value=(sys.executable, ["-c", script], None)):
        result = _executor.execute_agent(inv, timeout_ms=5000)
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path), attempt_id))
    assert result["submission_accounting_durable"] == {
        "state": "settled", "owner_fenced": True}
    assert durable["submission_accounting"][0]["submission_state"] == "indeterminate"
    assert accounting.public_summary(durable["submission_accounting"])["unknown_spend"] is True


def test_byteplus_payg_second_request_is_distinct_durable_child(tmp_path, monkeypatch):
    attempt_id = "d" * 32
    nonce = "private-fence"
    inv = AgentInvocation(
        cli="openai-compat", prompt="task", cwd=str(tmp_path),
        system_context="rules", attempt_id=attempt_id,
        request_sha256="e" * 64, model="seed-1-6-250615",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        api_key_env="BYTEPLUS_CODING_API_KEY", allow_payg=True)
    _jobs.write_prepared(
        str(tmp_path), attempt_id, nonce=nonce, agent="synthetic",
        prompt_sha256=hashlib.sha256(b"task").hexdigest(), cwd=str(tmp_path),
        flags={"cli": "openai-compat"}, summon={}, attempt_id=attempt_id,
        accounting_grants={"initial": 1, "payg_fallback": 1})
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", attempt_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    monkeypatch.setattr(_apibackend, "resolve_api_credential",
                        lambda *_args: ("synthetic-key", "env"))
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda _flag: True)
    calls = []

    def fake_request(base_url, *_args, **_kwargs):
        calls.append(base_url)
        if len(calls) == 1:
            return {"status": "error", "exit_code": 1, "cli": "openai-compat",
                    "error": "quota", "provider_contacted": True,
                    "submission_state": "submitted", "_payg_fallback_worthy": True,
                    "_fallback_reason": "quota"}
        return {"status": "success", "exit_code": 0, "cli": "openai-compat",
                "result": "ok", "provider_contacted": True,
                "submission_state": "submitted", "usage": {"total_tokens": 3},
                "usage_observation": {"scope": "attempt_total", "source": "synthetic"}}

    monkeypatch.setattr(_apibackend, "_do_request", fake_request)
    result = _executor.execute_agent(inv, timeout_ms=5000)
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path), attempt_id))
    records = durable["submission_accounting"]
    assert len(calls) == 2
    assert len(records) == 2
    assert records[1]["attempt"]["id"] != attempt_id
    assert records[1]["attempt"]["parent_id"] == attempt_id
    assert records[1]["attempt"]["kind"] == "payg_fallback"
    summary = accounting.public_summary(result["submission_accounting"])
    assert summary["physical_attempts"] == 2
    assert summary["reported"]["total_tokens"]["known_subtotal"] == 3


def test_payg_preparation_failure_refuses_second_and_preserves_first(tmp_path, monkeypatch):
    attempt_id = "f" * 32
    nonce = "private-fence"
    inv = AgentInvocation(
        cli="openai-compat", prompt="task", cwd=str(tmp_path),
        system_context="rules", attempt_id=attempt_id,
        request_sha256="1" * 64, model="seed-1-6-250615",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        api_key_env="BYTEPLUS_CODING_API_KEY", allow_payg=True)
    _jobs.write_prepared(
        str(tmp_path), attempt_id, nonce=nonce, agent="synthetic",
        prompt_sha256="0" * 64, cwd=str(tmp_path), flags={}, summon={},
        attempt_id=attempt_id, accounting_grants={"initial": 1})
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", attempt_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    monkeypatch.setattr(_apibackend, "resolve_api_credential",
                        lambda *_args: ("synthetic-key", "env"))
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda _flag: True)
    calls = []
    def fake_request(*_args, **_kwargs):
        calls.append(1)
        return {"status": "error", "exit_code": 1, "cli": "openai-compat",
                "error": "quota", "provider_contacted": True,
                "submission_state": "submitted", "_payg_fallback_worthy": True}
    monkeypatch.setattr(_apibackend, "_do_request", fake_request)
    result = _executor.execute_agent(inv, timeout_ms=5000)
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path), attempt_id))
    assert len(calls) == 1
    assert result["error_kind"] == "submission_accounting_unavailable"
    assert len(durable["submission_accounting"]) == 1
    assert durable["submission_accounting"][0]["submission_state"] == "submitted"


@pytest.mark.parametrize("unsupported_schema", [
    "summon.background-launch/v999", {"version": 2}, 2,
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_accounting_migration_refuses_future_or_wrong_schema_without_change(
        tmp_path, unsupported_schema):
    job_id = "2" * 32
    nonce = "private-fence"
    inv = AgentInvocation(cli="codex", prompt="task", cwd=str(tmp_path),
                          system_context="rules", attempt_id=job_id,
                          request_sha256="3" * 64)
    _jobs.write_prepared(
        str(tmp_path), job_id, nonce=nonce, agent="synthetic",
        prompt_sha256="0" * 64, cwd=str(tmp_path), flags={}, summon={},
        attempt_id=job_id, accounting_grants={"initial": 1})
    path = _jobs.record_path(str(tmp_path), job_id)
    record = _jobs.read_json(path)
    record["schema"] = unsupported_schema
    _jobs._atomic_write_json(path, record)
    before = open(path, "rb").read()
    possible = accounting.private_record(
        attempt_id=job_id, attempt_kind="initial", attempt_ordinal=1,
        parent_attempt_id=None, request_sha256=inv.request_sha256,
        estimate=_estimate(inv), envelope={}, submission_state="possible")
    with pytest.raises(ValueError):
        _jobs.record_submission_prelaunch(
            str(tmp_path), job_id, nonce=nonce, accounting=possible)
    assert open(path, "rb").read() == before


def test_supported_legacy_background_record_remains_readable(tmp_path):
    job_id = "4" * 32
    nonce = "legacy-nonce"
    _jobs.write_prepared(
        str(tmp_path), job_id, nonce=nonce, agent="synthetic",
        prompt_sha256="0" * 64, cwd=str(tmp_path), flags={}, summon={},
        attempt_id=job_id)
    path = _jobs.record_path(str(tmp_path), job_id)
    record = _jobs.read_json(path)
    record["schema"] = "summon.background-launch/v1"
    record.pop("accounting_handoff", None)
    record.pop("submission_accounting", None)
    _jobs._atomic_write_json(path, record)
    status = _jobs.job_status(str(tmp_path), job_id)
    assert status["state"] == "prepared"
    assert status["attempt_id"] == job_id

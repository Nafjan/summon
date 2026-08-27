from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import threading
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _usage_live as live


NOW = "2026-08-27T00:00:00Z"


@pytest.fixture(autouse=True)
def isolated_checkpoint_root(tmp_path_factory, monkeypatch):
    monkeypatch.setenv(
        "SUMMON_USAGE_LIVE_CHECKPOINT_ROOT",
        str(tmp_path_factory.mktemp("independent-usage-checkpoints")))


def wire(*, account="acct-a", used=25, nested=None, extra_account=None):
    account_value = {"id": account, "email": "private@example.test"}
    if extra_account:
        account_value.update(extra_account)
    limits = {"primary": {"usedPercent": used, "resetsAt": 1787792400}}
    rate_result = {"rateLimits": limits}
    if nested is not None:
        rate_result["rateLimitsByLimitId"] = {
            "codex": {"primary": {"usedPercent": nested, "resetsAt": 1787792400}}}
    values = [
        {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"account": account_value}},
        {"jsonrpc": "2.0", "id": 3, "result": rate_result},
    ]
    return "\n".join(json.dumps(item) for item in values) + "\n"


def runner_for(payload, *, stderr="", exit_code=0, elapsed_ms=1, calls=None,
               provider_contacted=True, cli_version="codex-cli 0.147.0"):
    def run(plan):
        if calls is not None:
            calls.append(plan)
        return {"stdout": payload, "stderr": stderr, "exit_code": exit_code,
                "elapsed_ms": elapsed_ms,
                "provider_contacted": provider_contacted,
                "cli_version": cli_version}
    return run


def test_module_ast_has_no_provider_or_process_execution_surface():
    source = (SCRIPTS / "_usage_live.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden_imports = {
        "asyncio", "ctypes", "ftplib", "http", "httpx", "importlib",
        "multiprocessing", "requests", "runpy", "socket", "subprocess", "urllib",
    }
    forbidden_os_imports = {
        "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp",
        "execvpe", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe",
        "spawnv", "spawnve", "spawnvp", "spawnvpe", "startfile", "system",
    }
    imports = set()
    imported_os_process_apis = set()
    calls = set()

    def qualified_name(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = qualified_name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
            if node.module == "os":
                imported_os_process_apis.update(
                    alias.name for alias in node.names
                    if alias.name in forbidden_os_imports or alias.name.startswith(("exec", "spawn")))
        elif isinstance(node, ast.Call):
            name = qualified_name(node.func)
            if name:
                calls.add(name)
    assert not imports & forbidden_imports
    assert not imported_os_process_apis
    assert not calls & {
        "Popen", "__import__", "call", "check_call", "check_output", "compile",
        "eval", "exec", "getattr", "globals", "locals", "run", "urlopen", "vars",
    }
    forbidden_qualified = {
        "ctypes.CDLL", "ctypes.PyDLL", "ctypes.WinDLL", "importlib.import_module",
        "os.popen", "os.startfile", "os.system", "runpy.run_module", "runpy.run_path",
        "subprocess.call", "subprocess.check_call", "subprocess.check_output",
        "subprocess.Popen", "subprocess.run",
    }
    assert not calls & forbidden_qualified
    assert not any(name.startswith(("os.exec", "os.spawn")) for name in calls)
    assert not any(name.startswith("asyncio.create_subprocess_") for name in calls)


def test_capability_registry_is_truthful_and_ark_is_disabled():
    values = {item["provider"]: item for item in live.capabilities()}
    assert values["codex"]["state"] == "fixture_supported"
    assert values["codex"]["live_enabled"] is True
    assert values["codex"]["tested_cli_version"] == "codex-cli 0.147.0"
    assert values["codex"]["fixture_sha256"] == live.CODEX_SCHEMA_FIXTURE_SHA256
    assert values["arkcli"]["state"] == "schema_unverified"
    assert values["arkcli"]["live_enabled"] is False
    assert {values[name]["state"] for name in ("claude", "kimi")} == {"unsupported"}
    assert values["agy"]["state"] == "schema_unverified"
    assert values["agy"]["live_enabled"] is False
    assert live.preflight("arkcli", allow_account_usage_read=True)["error_kind"] == "schema_unverified"
    assert live.preflight("agy", allow_account_usage_read=True)["error_kind"] == "schema_unverified"


def test_consent_and_dry_run_never_invoke_runner(tmp_path):
    calls = []
    denied = live.refresh_codex(
        runner=lambda plan: calls.append(plan), store_file=str(tmp_path / "usage.json"))
    assert denied["error_kind"] == "consent_required"
    result = live.refresh_codex(
        allow_account_usage_read=True, dry_run=True,
        runner=lambda plan: calls.append(plan), store_file=str(tmp_path / "usage.json"))
    assert result["status"] == "success"
    assert result["provider_contacted"] is False
    assert result["attempts"] == 0
    assert calls == []
    assert not (tmp_path / "usage.json").exists()


def test_exact_codex_plan_is_bounded_one_attempt_and_disables_refresh_token():
    plan = live.codex_request_plan(allow_account_usage_read=True)
    assert plan["argv"] == ["codex", "app-server"]
    assert plan["required_cli_version"] == "codex-cli 0.147.0"
    assert plan["schema_fixture_sha256"] == live.CODEX_SCHEMA_FIXTURE_SHA256
    assert plan["command_timeout_ms"] == 10_000
    assert plan["total_timeout_ms"] == 25_000
    assert plan["max_stdout_bytes"] == 64 * 1024
    assert plan["max_stderr_bytes"] == 8 * 1024
    assert plan["max_attempts"] == 1 and plan["retry_allowed"] is False
    assert [item["method"] for item in plan["requests"]] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read"]
    assert "id" not in plan["requests"][1]
    assert plan["requests"][2]["params"] == {"refreshToken": False}


def test_version_pinned_codex_schema_fixture_matches_runtime_contract():
    fixture_path = (Path(__file__).parent / "fixtures"
                    / "codex_app_server_usage_0_147_0.json")
    raw = fixture_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == live.CODEX_SCHEMA_FIXTURE_SHA256
    fixture = json.loads(raw)
    assert fixture["generator"]["cli_version"] == live.CODEX_SCHEMA_CLI_VERSION
    assert [item["method"] for item in fixture["request_contract"]] == [
        "initialize", "initialized", "account/read", "account/rateLimits/read"]
    account = fixture["request_contract"][2]
    assert account["refreshToken_type"] == "boolean"
    assert account["summon_value"] is False
    assert fixture["response_contract"]["GetAccountResponse"]["properties"] == [
        "account", "requiresOpenaiAuth"]
    assert fixture["response_contract"]["GetAccountRateLimitsResponse"]["required"] == [
        "rateLimits"]


def test_unreviewed_codex_cli_version_fails_closed_without_persisting(tmp_path):
    path = tmp_path / "usage.json"
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for(wire(), cli_version="codex-cli 0.148.0"),
        store_file=str(path), now=NOW)
    assert result["status"] == "error"
    assert result["error_kind"] == "schema_version_mismatch"
    assert result["provider_contacted"] is True
    assert not path.exists()


def test_missing_binary_failure_is_not_mislabeled_as_schema_mismatch(tmp_path):
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for(
            "", exit_code=1, provider_contacted=False, cli_version=None),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "backend_execution_failed"
    assert result["provider_contacted"] is False
    assert result["attempts"] == 1


@pytest.mark.parametrize("escaped_key", ["e\\u006dail", "access\\u0054oken"])
def test_escaped_sensitive_json_keys_fail_before_parse(tmp_path, escaped_key):
    payload = (
        '{"jsonrpc":"2.0","id":1,"result":{}}\n'
        + '{"jsonrpc":"2.0","id":2,"result":{"account":{"'
        + escaped_key + '":"private-value"}}}\n'
        + '{"jsonrpc":"2.0","id":3,"result":{"rateLimits":'
          '{"primary":{"usedPercent":25}}}}\n')
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(payload),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["status"] == "error"
    assert result["error_kind"] == "provider_response_invalid"
    assert not (tmp_path / "usage.json").exists()


@pytest.mark.parametrize("ids", [(True, 2, 3), (1, 2.0, 3.0)])
def test_jsonrpc_response_ids_must_be_exact_integers(tmp_path, ids):
    payload = "\n".join(json.dumps({"jsonrpc": "2.0", "id": value, "result": {}})
                        for value in ids) + "\n"
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(payload),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["status"] == "error"
    assert result["error_kind"] == "provider_response_incomplete"


def test_array_budget_and_unpaired_surrogate_fail_closed(tmp_path):
    too_many = [0] * (live.MAX_JSON_ITEMS + 1)
    payload = wire(extra_account={"ignored": too_many})
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(payload),
        store_file=str(tmp_path / "array.json"), now=NOW)
    assert result["error_kind"] == "provider_response_invalid"

    hostile = wire(extra_account={"ignored": "\ud800"})
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(hostile),
        store_file=str(tmp_path / "surrogate.json"), now=NOW)
    assert result["error_kind"] == "provider_response_invalid"


def test_refresh_projects_advisory_and_private_account_hmac(tmp_path):
    path = str(tmp_path / "usage.json")
    calls = []
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(), calls=calls),
        store_file=path, now=NOW)
    assert len(calls) == 1
    assert result["status"] == "success"
    assert result["remaining"] == {"value": 75.0, "unit": "percent"}
    assert result["advisory_only"] is True and result["routing_changed"] is False
    assert result["provider_contacted"] is True and result["attempts"] == 1
    rendered = json.dumps(result)
    assert "acct-a" not in rendered and "private@example.test" not in rendered
    private = Path(path).read_text(encoding="utf-8")
    assert "acct-a" not in private and "private@example.test" not in private
    cached = live.status(store_file=path, now=NOW)
    assert cached["provider_contacted"] is False
    assert cached["observations"][0]["account_scope_hmac"] == result["account_scope_hmac"]


def test_redacts_hostile_sensitive_fields_before_parse_and_drops_unknowns(tmp_path):
    payload = wire(extra_account={
        "accessToken": "Bearer super-secret-token-value",
        "refresh_token": "another-private-value", "name": "Private Person",
    })
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(payload),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["status"] == "success"
    rendered = json.dumps(result) + (tmp_path / "usage.json").read_text(encoding="utf-8")
    for private in ("super-secret", "another-private", "Private Person", "private@example"):
        assert private not in rendered


def test_email_only_account_identity_is_keyed_before_projection(tmp_path):
    payload = wire()
    payload = payload.replace('"id": "acct-a", ', "")
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(payload),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["status"] == "success"
    assert re.fullmatch(r"[0-9a-f]{64}", result["account_scope_hmac"])


def test_top_level_and_codex_bucket_disagreement_becomes_unknown(tmp_path):
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(used=25, nested=30)),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["status"] == "success"
    assert result["support"] == "unknown"
    assert "remaining" not in result


def test_nested_only_bucket_is_supported_and_hostile_nested_shape_is_typed(tmp_path):
    nested_only = wire(nested=30).replace(
        '"rateLimits": {"primary": {"usedPercent": 25, "resetsAt": 1787792400}}, ',
        "")
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(nested_only),
        store_file=str(tmp_path / "nested.json"), now=NOW)
    assert result["status"] == "success"
    assert result["remaining"] == {"value": 70.0, "unit": "percent"}

    hostile = wire().replace(
        '"rateLimits": {"primary": {"usedPercent": 25, "resetsAt": 1787792400}}',
        '"rateLimits": {}, "rateLimitsByLimitId": {"codex": "hostile"}')
    refused = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(hostile),
        store_file=str(tmp_path / "hostile.json"), now=NOW)
    assert refused["status"] == "error"
    assert refused["error_kind"] == "provider_response_invalid"


def test_account_switch_replaces_scope_without_leaking_identity(tmp_path):
    path = str(tmp_path / "usage.json")
    first = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(account="acct-a")),
        store_file=path, now=NOW)
    second = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(account="acct-b")),
        store_file=path, now="2026-08-27T00:01:00Z")
    assert first["account_scope_hmac"] != second["account_scope_hmac"]
    assert first["account_changed"] is False and second["account_changed"] is True
    assert "acct-a" not in Path(path).read_text(encoding="utf-8")
    assert "acct-b" not in Path(path).read_text(encoding="utf-8")


def test_equivalent_json_identity_spellings_share_one_private_scope(tmp_path):
    path = str(tmp_path / "usage.json")
    first = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(account="acct-a")),
        store_file=path, now=NOW)
    escaped = wire(account="acct-a").replace("acct-a", r"acct-\u0061")
    second = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(escaped),
        store_file=path, now="2026-08-27T00:01:00Z")
    assert first["account_scope_hmac"] == second["account_scope_hmac"]
    assert second["account_changed"] is False


def test_freshness_means_recent_local_command_observation(tmp_path):
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for(wire(), provider_contacted=False),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["provider_contacted"] is False
    assert result["freshness"] == "fresh"
    assert result["freshness_basis"] == "local_command_observation"


def test_auth_failure_preserves_fresh_cache_and_never_returns_stderr(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for("", stderr="token=very-private", exit_code=1),
        store_file=path, now="2026-08-27T00:01:00Z")
    assert result["status"] == "partial"
    assert result["error_kind"] == "backend_execution_failed"
    assert result["cache_preserved"] is True
    assert "very-private" not in json.dumps(result)


@pytest.mark.parametrize("field,size,kind", [
    ("stdout", 64 * 1024 + 1, "stdout_limit_exceeded"),
    ("stderr", 8 * 1024 + 1, "stderr_limit_exceeded"),
])
def test_output_bounds_fail_closed_without_persistence(tmp_path, field, size, kind):
    value = {
        "stdout": wire(), "stderr": "", "exit_code": 0, "elapsed_ms": 1,
        "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
    }
    value[field] = "x" * size
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=lambda _plan: value,
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == kind
    assert result["cache_preserved"] is False


def test_total_timeout_is_not_retried(tmp_path):
    calls = []
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for(wire(), elapsed_ms=25_001, calls=calls),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "usage_refresh_timeout"
    assert len(calls) == 1 and result["attempts"] == 1


def test_command_timeout_survives_refresh_projection_with_honest_contact(tmp_path):
    calls = []

    def stalled(_plan):
        calls.append(True)
        return {
            "stdout": b'{"jsonrpc":"2.0","id":1,"result":{}}\n',
            "stderr": b"private provider diagnostics must not escape",
            "exit_code": 124,
            "elapsed_ms": live.COMMAND_TIMEOUT_MS,
            "provider_contacted": None,
            "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
            "error_kind": "usage_refresh_command_timeout",
        }

    result = live.refresh_codex(
        allow_account_usage_read=True, runner=stalled,
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "usage_refresh_command_timeout"
    assert result["attempts"] == 1
    assert result["provider_contacted"] is None
    assert len(calls) == 1
    assert "private provider diagnostics" not in json.dumps(result)


def test_runner_exception_is_unknown_contact_not_auth_and_is_not_retried(tmp_path):
    calls = []

    def broken(_plan):
        calls.append(True)
        raise RuntimeError("private path and account text")

    result = live.refresh_codex(
        allow_account_usage_read=True, runner=broken,
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "runner_failed"
    assert result["provider_contacted"] is None
    assert result["attempts"] == 1 and len(calls) == 1
    assert "private path" not in json.dumps(result)


def test_jsonrpc_error_is_auth_only_with_specific_evidence(tmp_path):
    base = [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2,
         "error": {"code": 401, "message": "credential expired: private"}},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
    ]
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for("\n".join(json.dumps(item) for item in base)),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "authentication_failed"
    assert "private" not in json.dumps(result)


def test_store_and_anchor_tamper_or_rollback_mismatch_fail_closed(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    original_store = Path(path).read_bytes()
    original_anchor = Path(path + ".anchor").read_bytes()
    value = json.loads(original_store)
    value["observations"]["codex"]["remaining"]["value"] = 100
    Path(path).write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(live.UsageLiveError, match="authentication"):
        live.status(store_file=path, now=NOW)

    Path(path).write_bytes(original_store)
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(used=20)),
        store_file=path, now="2026-08-27T00:01:00Z")
    Path(path + ".anchor").write_bytes(original_anchor)
    with pytest.raises(live.UsageLiveError, match="disagree"):
        live.status(store_file=path, now=NOW)


def test_joint_key_store_anchor_delete_or_replay_cannot_reset_initialized_state(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(used=25)),
        store_file=path, now=NOW)
    old_store = Path(path).read_bytes()
    old_anchor = Path(path + ".anchor").read_bytes()
    old_key = Path(path + ".key").read_bytes()
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(used=20)),
        store_file=path, now="2026-08-27T00:01:00Z")

    Path(path).write_bytes(old_store)
    Path(path + ".anchor").write_bytes(old_anchor)
    Path(path + ".key").write_bytes(old_key)
    with pytest.raises(live.UsageLiveError, match="checkpoint"):
        live.status(store_file=path, now=NOW)

    for selected in (path, path + ".anchor", path + ".key"):
        Path(selected).unlink()
    with pytest.raises(live.UsageLiveError, match="missing"):
        live.status(store_file=path, now=NOW)


def test_independent_checkpoint_deletion_fails_closed(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    Path(live._checkpoint_path(path)).unlink()
    with pytest.raises(live.UsageLiveError, match="incomplete"):
        live.status(store_file=path, now=NOW)


def test_independent_checkpoint_tamper_fails_closed(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    checkpoint = Path(live._checkpoint_path(path))
    value = json.loads(checkpoint.read_text(encoding="utf-8"))
    value["generation"] = 0
    checkpoint.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(live.UsageLiveError, match="checkpoint disagrees"):
        live.status(store_file=path, now=NOW)


def test_key_rotation_invalidates_existing_store(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    key_file = Path(path + ".key")
    value = json.loads(key_file.read_text(encoding="utf-8"))
    value["key"] = "11" * 32
    key_file.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(live.UsageLiveError, match="authentication"):
        live.status(store_file=path, now=NOW)


def test_key_change_during_runner_is_detected_before_persistence(tmp_path):
    path = str(tmp_path / "usage.json")

    def changing_runner(_plan):
        selected = Path(path + ".key")
        value = json.loads(selected.read_text(encoding="utf-8"))
        value["key"] = "22" * 32
        selected.write_text(json.dumps(value), encoding="utf-8")
        return {
            "stdout": wire(), "stderr": "", "exit_code": 0,
            "elapsed_ms": 1, "provider_contacted": True,
            "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
        }

    with pytest.raises(live.UsageLiveError, match="key changed"):
        live.refresh_codex(
            allow_account_usage_read=True, runner=changing_runner,
            store_file=path, now=NOW)
    assert not Path(path).exists()


def test_key_checkpoint_is_authenticated(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    key_file = Path(path + ".key")
    value = json.loads(key_file.read_text(encoding="utf-8"))
    value["store_binding"]["generation"] = 0
    key_file.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(live.UsageLiveError, match="authentication"):
        live.status(store_file=path, now=NOW)


def test_concurrent_updates_are_serialized_and_monotonic(tmp_path):
    path = str(tmp_path / "usage.json")
    barrier = threading.Barrier(4)
    errors = []

    def worker(index):
        try:
            barrier.wait()
            result = live.refresh_codex(
                allow_account_usage_read=True,
                runner=runner_for(wire(used=10 + index)), store_file=path,
                now=f"2026-08-27T00:0{index}:00Z")
            assert result["status"] == "success"
        except BaseException as exc:  # pragma: no cover - diagnostic aggregation
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    # Four observation commits are mandatory. Out-of-order lock acquisition may
    # also advance the authenticated monotonic clock before a later observation
    # commit, so those safety checkpoints legitimately consume generations too.
    assert 4 <= stored["generation"] <= 7
    assert stored["last_seen_at"] == "2026-08-27T00:03:00Z"
    assert live.status(store_file=path, now="2026-08-27T00:10:00Z")["status"] == "success"


def test_clock_rollback_cannot_make_stale_observation_fresh(tmp_path):
    path = str(tmp_path / "usage.json")
    live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=path, now=NOW)
    stale = live.status(store_file=path, now="2026-08-27T00:10:00Z")
    assert stale["observations"][0]["freshness"] == "stale"
    with pytest.raises(live.UsageLiveError, match="clock moved behind"):
        live.status(store_file=path, now="2026-08-27T00:01:00Z")


def test_relative_store_and_checkpoint_paths_are_rejected(tmp_path, monkeypatch):
    with pytest.raises(live.UsageLiveError, match="must be absolute"):
        live.status(store_file="relative-usage.json", now=NOW)
    monkeypatch.setenv("SUMMON_USAGE_LIVE_CHECKPOINT_ROOT", "relative-checkpoints")
    with pytest.raises(live.UsageLiveError, match="must be absolute"):
        live.status(store_file=str(tmp_path / "usage.json"), now=NOW)


def test_hostile_surrogate_runner_output_is_typed_and_public_safe(tmp_path):
    result = live.refresh_codex(
        allow_account_usage_read=True,
        runner=lambda _plan: {
            "stdout": "\ud800", "stderr": "", "exit_code": 0,
            "elapsed_ms": 1, "provider_contacted": False,
            "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
        },
        store_file=str(tmp_path / "usage.json"), now=NOW)
    assert result["error_kind"] == "runner_contract_invalid"
    assert result["provider_contacted"] is False
    assert "surrogate" not in json.dumps(result)


def test_provider_contact_is_validated_tri_state_and_never_assumed(tmp_path):
    omitted = live.refresh_codex(
        allow_account_usage_read=True,
        runner=lambda _plan: {
            "stdout": wire(), "stderr": "", "exit_code": 0, "elapsed_ms": 1,
            "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
        },
        store_file=str(tmp_path / "unknown.json"), now=NOW)
    assert omitted["status"] == "success"
    assert omitted["provider_contacted"] is None

    local_failure = live.refresh_codex(
        allow_account_usage_read=True,
        runner=runner_for("", exit_code=1, provider_contacted=False),
        store_file=str(tmp_path / "local-failure.json"), now=NOW)
    assert local_failure["error_kind"] == "backend_execution_failed"
    assert local_failure["provider_contacted"] is False

    invalid = live.refresh_codex(
        allow_account_usage_read=True,
        runner=lambda _plan: {
            "stdout": wire(), "stderr": "", "exit_code": 0, "elapsed_ms": 1,
            "cli_version": live.CODEX_SCHEMA_CLI_VERSION,
            "provider_contacted": "yes",
        },
        store_file=str(tmp_path / "invalid.json"), now=NOW)
    assert invalid["error_kind"] == "runner_contract_invalid"
    assert invalid["provider_contacted"] is None


def test_duplicate_fields_deep_json_and_missing_response_fail_closed(tmp_path):
    path = str(tmp_path / "usage.json")
    hostile = '{"jsonrpc":"2.0","id":1,"id":1,"result":{}}\n'
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(hostile),
        store_file=path, now=NOW)
    assert result["error_kind"] == "provider_response_invalid"
    assert not Path(path).exists()

    duplicate_id = wire() + json.dumps({
        "jsonrpc": "2.0", "id": 3, "result": {"rateLimits": {}}}) + "\n"
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(duplicate_id),
        store_file=str(tmp_path / "usage-2.json"), now=NOW)
    assert result["error_kind"] == "provider_response_invalid"

    deep = (
        '{"jsonrpc":"2.0","id":1,"result":{"nested":'
        + "[" * 2000 + "0" + "]" * 2000 + "}}\n")
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(deep),
        store_file=str(tmp_path / "usage-3.json"), now=NOW)
    assert result["error_kind"] == "provider_response_invalid"

    missing = "\n".join(wire().splitlines()[:2]) + "\n"
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(missing),
        store_file=str(tmp_path / "usage-4.json"), now=NOW)
    assert result["error_kind"] == "provider_response_incomplete"


def test_pathological_json_integers_are_typed_failures(tmp_path):
    huge_float = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire(used=10 ** 400)),
        store_file=str(tmp_path / "overflow.json"), now=NOW)
    assert huge_float["error_kind"] == "provider_response_invalid"

    too_many_digits = wire().replace(
        '"usedPercent": 25', '"usedPercent": ' + "9" * 5000)
    huge_parse = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(too_many_digits),
        store_file=str(tmp_path / "digits.json"), now=NOW)
    assert huge_parse["error_kind"] == "provider_response_invalid"


def test_public_results_pass_private_text_guard(tmp_path):
    result = live.refresh_codex(
        allow_account_usage_read=True, runner=runner_for(wire()),
        store_file=str(tmp_path / "usage.json"), now=NOW)
    import _evidence
    _evidence._reject_private_public_text(result)
    assert hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()

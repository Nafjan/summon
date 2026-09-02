"""Account routing regressions: no vendor calls or real credential reads."""
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace

import pytest

import _auth
import _builder
import _cli
import _executor
import _profiles


@pytest.fixture
def account(tmp_path, monkeypatch):
    home = tmp_path / "account"
    home.mkdir()
    cwd = tmp_path / "task"
    cwd.mkdir()
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"profiles": {
        "work": {"cli": "codex", "config_dir": str(home), "auth_mode": "login"},
        "claude-work": {"cli": "claude", "config_dir": str(home), "auth_mode": "login"},
    }}), encoding="utf-8")
    monkeypatch.setenv("SUMMON_PROFILES_FILE", str(registry))
    return home, cwd, registry


def invocation(account, cli="codex", **changes):
    home, cwd, _ = account
    return replace(_builder.AgentInvocation(
        cli=cli, cwd=str(cwd), prompt="public test", permission="read-only",
        model="gpt-5.6-sol" if cli == "codex" else "claude-opus-5",
        profile="work", profile_auth_mode="login",
        profile_env={_profiles.PROFILE_ENV_VARS[cli]: str(home)}), **changes)


def test_codex_profile_resolves_and_legacy_profiles_keep_semantics(account):
    home, cwd, registry = account
    selected = _profiles.resolve_profile("work", "codex", str(cwd))
    assert selected["env"] == {"CODEX_HOME": str(home.resolve())}
    assert selected["auth_mode"] == "login"
    doc = json.loads(registry.read_text())
    del doc["profiles"]["work"]["auth_mode"]
    registry.write_text(json.dumps(doc))
    assert _profiles.resolve_profile("work", "codex", str(cwd))["auth_mode"] == "profile"
    with pytest.raises(ValueError, match="not the resolved backend"):
        _profiles.resolve_profile("work", "claude", str(cwd))


def test_codex_named_profile_requires_explicit_model(account):
    selected = _profiles.resolve_profile("work", "codex", str(account[1]))
    with pytest.raises(ValueError, match="explicit model"):
        _profiles.validate_model(selected, None)


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_named_builder_isolates_child_without_mutating_parent(account, monkeypatch, backend):
    monkeypatch.setenv("OPENAI_API_KEY", "private-personal-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-personal-key")
    monkeypatch.setenv("CODEX_API_KEY", "private-personal-key")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "private-personal-token")
    monkeypatch.setenv("SUBAGENTS_ALLOW_OPENAI_KEY", "1")
    inv = invocation(account, backend, profile_command="chosen.exe")
    command, args, delta = _builder.build_invocation_args(inv)
    env = _executor._merge_env(delta)
    assert command == "chosen.exe"
    assert env[_profiles.PROFILE_ENV_VARS[backend]] == str(account[0])
    if backend == "codex":
        assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
        assert 'cli_auth_credentials_store="file"' in args
        assert 'forced_login_method="chatgpt"' in args
        assert 'model_provider="openai"' in args
    else:
        assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert args[args.index("--setting-sources") + 1] == "user"
    assert _profiles.os.environ["OPENAI_API_KEY"] == "private-personal-key"


@pytest.mark.parametrize("changes", [
    {"extra_args": ("--config", "model_provider=other")},
    {"transport": "acp"},
    {"resume_id": "some-session"},
])
def test_account_rejects_unbound_overrides_and_codex_resume(account, changes):
    with pytest.raises(ValueError):
        _builder.build_invocation_args(invocation(account, **changes))


def test_claude_resume_rejects_paths(account):
    with pytest.raises(ValueError, match="UUID"):
        _builder.build_invocation_args(invocation(account, "claude", resume_id="../personal.jsonl"))
    _, args, env = _builder.build_invocation_args(invocation(
        account, "claude", resume_id="12345678-1234-1234-1234-123456789abc"))
    assert "--resume" in args and env["CLAUDE_CONFIG_DIR"] == str(account[0])


def test_profile_changes_bind_request_identity(account):
    home, cwd, registry = account
    agents = cwd / ".agents"
    agents.mkdir()
    (agents / "reviewer.md").write_text("---\nrun-agent: codex\nmodel: gpt-5.6-sol\n---\nReview.")
    kwargs = dict(agent="reviewer", prompt="public", cwd=str(cwd), agents_dir=str(agents), profile="work")
    before = _executor.build_request_identity(**kwargs)
    doc = json.loads(registry.read_text())
    other = home.parent / "personal"
    other.mkdir()
    doc["profiles"]["work"]["config_dir"] = str(other)
    registry.write_text(json.dumps(doc))
    after = _executor.build_request_identity(**kwargs)
    assert before["profile_path_sha256"] != after["profile_path_sha256"]
    assert before["profile_registry_sha256"] != after["profile_registry_sha256"]


def test_local_credential_rotation_invalidates_profile_state_without_echoing_it(account):
    before = _profiles.resolve_profile("work", "codex", str(account[1]))
    (account[0] / "auth.json").write_text('{"test_token":"private-value"}')
    after = _profiles.resolve_profile("work", "codex", str(account[1]))
    assert before["state_sha256"] != after["state_sha256"]
    assert "private-value" not in json.dumps(after)


def test_background_argv_preserves_account_selection(account):
    import _background
    ns = _cli.build_parser("test", 1).parse_args([
        "--agent", "sol", "--prompt", "public", "--cwd", str(account[1]),
        "--profile", "work", "--background"])
    args = _background.child_argv(ns, str(account[1] / "job.json"))
    assert args[args.index("--profile") + 1] == "work"


def test_dry_run_matches_account_builder_without_exposing_home(account):
    import run_subagent
    inv = invocation(account)
    args = SimpleNamespace(agent="sol", cwd=str(account[1]), agents_dir=str(account[1]),
                           worktree=None, timeout=600000, allow_text_only=False, require_tools=False)
    view = run_subagent._dry_run_view(inv, args, str(account[1]))
    assert "CODEX_HOME" in view["env_overrides"]
    assert 'cli_auth_credentials_store="file"' in view["args"]
    assert str(account[0]) not in json.dumps(view)


def test_profile_auth_status_is_local_until_probe(account, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unexpected child"))
    result = _auth.run_auth_action("status", backend="codex", profile="work")
    assert result["auth_status"] == "unverified" and not result["probe_ran"]
    assert str(account[0]) not in json.dumps(result)


@pytest.mark.parametrize("backend,name,stdout", [
    ("claude", "claude-work", '{"loggedIn":true,"email":"private@example.invalid"}'),
    ("codex", "work", "Logged in using ChatGPT: private@example.invalid"),
])
def test_auth_probe_uses_account_and_redacts_vendor_output(account, monkeypatch, backend, name, stdout):
    monkeypatch.setattr(_auth, "_command_for", lambda cli, plan: plan["argv"])
    def run(command, **kwargs):
        assert kwargs["env"][_profiles.PROFILE_ENV_VARS[backend]] == str(account[0])
        assert kwargs["timeout"] == 30
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="private key hint")
    monkeypatch.setattr(subprocess, "run", run)
    result = _auth.run_auth_action("status", backend=backend, profile=name, probe=True)
    assert result["auth_status"] == "authenticated"
    assert "private" not in json.dumps(result) and not result["model_call_started"]


def test_auth_repair_requires_consent_and_uses_selected_home(account, monkeypatch):
    monkeypatch.setattr(_auth, "_command_for", lambda cli, plan: plan["argv"])
    calls = []
    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(wait=lambda **kw: 0)
    monkeypatch.setattr(subprocess, "Popen", popen)
    blocked = _auth.run_auth_action("repair", backend="codex", profile="work")
    assert blocked["status"] == "blocked" and not calls
    assert "--profile work" in blocked["auth"]["command"]
    result = _auth.run_auth_action("repair", backend="codex", profile="work", allow=True)
    assert result["status"] == "success" and len(calls) == 1
    assert calls[0][1]["env"]["CODEX_HOME"] == str(account[0])
    assert 'cli_auth_credentials_store="file"' in calls[0][0]


def test_auth_cli_accepts_profile_without_dispatch_flags():
    argv, _ = _cli.rewrite_subcommand(["auth", "status", "--cli", "codex", "--profile", "work", "--json"])
    args = _cli.build_parser("test", 1).parse_args(argv)
    assert args.profile == "work"
    assert "profile" in _cli.MODE_FLAGS["auth"]


def test_fleet_accounts_remain_distinct_and_usage_not_assumed(account, monkeypatch):
    import _fleet_activation
    monkeypatch.setenv("OPENAI_API_KEY", "private")
    inv = invocation(account)
    env = _fleet_activation._profile_environment(inv)
    assert env["CODEX_HOME"] == str(account[0]) and "OPENAI_API_KEY" not in env
    assert _fleet_activation.derive_billing_class(inv)["class"] == "unknown"
    assert _fleet_activation.invocation_structural_sha256(inv)

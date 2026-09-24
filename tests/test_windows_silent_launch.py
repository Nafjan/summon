"""Provider-inert Windows launch and release-tool console-suppression tests."""

from __future__ import annotations

import ast
import ctypes
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
SCRIPTS = ROOT / "skills" / "summon" / "scripts"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GATES = _load("silent_launch_release_gates", TOOLS / "release_gates.py")
MANIFEST = GATES._MANIFEST
SPAWN = _load("silent_launch_spawn", SCRIPTS / "_spawn.py")


def _completed(stdout: str = "", *, returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _runner(outputs: list[str]):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        index = len(calls) - 1
        return _completed(outputs[index] if index < len(outputs) else "")

    return calls, fake_run


def _assert_common_run_kwargs(kwargs: dict, *, timeout: float, check: bool):
    assert kwargs["cwd"] == str(GATES.ROOT)
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["timeout"] == timeout
    assert kwargs["check"] is check
    assert kwargs["fixture_no_console"] is True
    assert "shell" not in kwargs


def _assert_hermetic_runner_env(env: dict):
    assert env["SUMMON_TELEMETRY"] == "0"
    assert env["SUMMON_ACP_FALLBACK"] == "0"
    assert env["SUMMON_TRANSIENT_RETRIES"] == "0"
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTEST_PLUGINS" not in env
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in env


def test_release_tools_forward_shared_run_flags_without_changing_baseline(
        monkeypatch):
    """Every owned release-tool subprocess carries the shared utility policy."""
    monkeypatch.setattr(MANIFEST, "run_flags", lambda: {"fixture_no_console": True})
    calls, fake_run = _runner([str(GATES.ROOT), "a" * 40])
    monkeypatch.setattr(GATES.subprocess, "run", fake_run)
    assert GATES._git_head() == "a" * 40
    assert len(calls) == 2
    expected_git = [
        ["git", "-c", f"safe.directory={GATES.ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "--show-toplevel"],
        ["git", "-c", f"safe.directory={GATES.ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "HEAD"],
    ]
    git_env = MANIFEST._git_env()
    assert [argv for argv, _kwargs in calls] == expected_git
    for _argv, kwargs in calls:
        _assert_common_run_kwargs(
            kwargs, timeout=GATES.GIT_METADATA_TIMEOUT_SECONDS, check=True)
        assert kwargs["env"] == git_env
        assert "errors" not in kwargs

    calls, fake_run = _runner([""])
    monkeypatch.setattr(GATES.subprocess, "run", fake_run)
    outcomes_calls = []
    monkeypatch.setattr(
        GATES, "_run_outcomes",
        lambda kind, name, timeout, source_hash, git_head: (
            outcomes_calls.append((kind, name, timeout, source_hash, git_head))
            or ("1/1", "", {"status": "pass"})))
    calls.clear()
    assert GATES.run_suite("fixture", 3.0, source_hash="c" * 64,
                           git_head="d" * 40)[0] == "1/1"
    assert calls == []
    assert outcomes_calls[0][:3] == ("suite", "fixture", 3.0)

    outcomes_calls.clear()
    monkeypatch.setitem(GATES.GATE_COMMANDS, "fixture", "python -c pass")
    monkeypatch.setattr(GATES, "_marker", lambda *_args: None)
    monkeypatch.setattr(GATES, "_artifact", lambda *args, **kwargs: {"gate": "fixture"})
    monkeypatch.setattr(GATES, "_write_artifact", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(MANIFEST._OUTCOMES, "validate_outcomes", lambda *args, **kwargs: "1/1")
    monkeypatch.setattr(
        GATES, "_run_outcomes",
        lambda kind, name, timeout, source_hash, git_head: (
            outcomes_calls.append((kind, name, timeout, source_hash, git_head))
            or ("1/1", "", {"status": "pass"})))
    assert GATES.run_gate(
        "fixture", 4.0, source_hash="a" * 64, git_head="b" * 40
    )[0] == "pass"
    assert calls == []
    assert outcomes_calls == [("gate", "fixture", 4.0, "a" * 64, "b" * 40)]

    calls, fake_run = _runner(["", ""])
    monkeypatch.setattr(GATES.subprocess, "run", fake_run)
    monkeypatch.setattr(GATES, "COMMANDS", {})
    monkeypatch.setattr(GATES, "GATE_COMMANDS", {})
    source_hashes = iter(["c" * 64, "c" * 64])
    git_heads = iter(["d" * 40, "d" * 40])
    monkeypatch.setattr(GATES, "_source_hash", lambda: next(source_hashes))
    monkeypatch.setattr(GATES, "_git_head", lambda: next(git_heads))
    monkeypatch.setattr(GATES.platform, "platform", lambda: "fixture-platform")
    GATES.build_evidence(5.0)
    assert len(calls) == 2
    expected_status = [
        ["git", "-c", f"safe.directory={GATES.ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
    ] * 2
    assert [argv for argv, _kwargs in calls] == expected_status
    for _argv, kwargs in calls:
        _assert_common_run_kwargs(
            kwargs, timeout=GATES.GIT_METADATA_TIMEOUT_SECONDS, check=True)
        assert kwargs["env"] == MANIFEST._git_env()
        assert "errors" not in kwargs

    calls, fake_run = _runner([str(ROOT), "a" * 40, ""])
    monkeypatch.setattr(MANIFEST.subprocess, "run", fake_run)
    facts = MANIFEST._git_facts(ROOT)
    assert facts["status_available"] is True
    assert len(calls) == 3
    expected_manifest = [
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "--show-toplevel"],
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "rev-parse", "HEAD"],
        ["git", "-c", f"safe.directory={ROOT.resolve()}",
         "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
         "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
    ]
    assert [argv for argv, _kwargs in calls] == expected_manifest
    for _argv, kwargs in calls:
        assert kwargs["cwd"] == str(ROOT)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["timeout"] == 5
        assert kwargs["check"] is False
        assert kwargs["fixture_no_console"] is True
        assert kwargs["env"] == MANIFEST._git_env()
        assert "errors" not in kwargs
        assert "shell" not in kwargs


def test_shared_run_flags_are_empty_on_posix(monkeypatch):
    monkeypatch.setattr(SPAWN.os, "name", "posix")
    assert SPAWN.run_flags() == {}


def _launch_target(node: ast.Call, aliases: set[str], imported: set[str]):
    func = node.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id in aliases:
            if func.attr in {"run", "Popen", "check_output", "check_call", "call"}:
                return func.attr
    if isinstance(func, ast.Name) and func.id in imported:
        return func.id
    return None


def _shared_flags_call(value: ast.AST) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    if isinstance(func, ast.Name):
        return func.id in {"run_flags", "popen_flags"}
    return (
        isinstance(func, ast.Attribute)
        and func.attr in {"run_flags", "popen_flags"}
        and isinstance(func.value, ast.Name)
        and func.value.id in {"_MANIFEST", "_spawn", "SPAWN"}
    )


def _assert_launch_inventory(paths: list[Path]):
    """AST guard implementation shared by the real and negative-fixture tests."""
    assert paths, "silent-launch inventory is empty"
    for path in paths:
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise AssertionError(f"syntax error in launch inventory: {path}: {exc}") from exc
        aliases = set()
        imported = set()
        os_aliases = set()
        os_system_aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    if item.name == "subprocess":
                        aliases.add(item.asname or "subprocess")
                    elif item.name == "os":
                        os_aliases.add(item.asname or "os")
            elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
                for item in node.names:
                    if item.name in {"run", "Popen", "check_output", "check_call", "call"}:
                        imported.add(item.asname or item.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                for item in node.names:
                    if item.name == "system":
                        os_system_aliases.add(item.asname or item.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in os_aliases
                and node.func.attr == "system"
            ):
                raise AssertionError(f"os.system is forbidden in launch inventory: {path}")
            if isinstance(node.func, ast.Name) and node.func.id in os_system_aliases:
                raise AssertionError(f"os.system alias is forbidden in launch inventory: {path}")
            launch = _launch_target(node, aliases, imported)
            if launch is None:
                continue
            for keyword in node.keywords:
                if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True:
                        raise AssertionError(f"shell=True is forbidden: {path}:{node.lineno}")
            if not any(keyword.arg is None and _shared_flags_call(keyword.value)
                       for keyword in node.keywords):
                raise AssertionError(
                    f"{path}:{node.lineno} {launch} lacks shared spawn flags")


def test_owned_launch_sites_have_shared_flags_and_no_shell_escape():
    """AST guard: syntax errors, aliases, shell launches, and new tools cannot hide."""
    _assert_launch_inventory(sorted(TOOLS.glob("*.py")) + [
        SCRIPTS / "test_job_control.py",
        SCRIPTS / "test_conversation_runtime.py",
        SCRIPTS / "test_agy_1_1_22.py",
        SCRIPTS / "test_background_read_roots.py",
        SCRIPTS / "test_discovery.py",
        SCRIPTS / "test_conversation.py",
        SCRIPTS / "test_conversation_ui.py",
        SCRIPTS / "test_deliberation_agents.py",
        SCRIPTS / "test_deliberation_cli.py",
        SCRIPTS / "test_deliberation_ui.py",
        SCRIPTS / "test_phase1_operator_workflow.py",
        SCRIPTS / "test_portable_result.py",
        SCRIPTS / "test_evidence_kernel.py",
        SCRIPTS / "test_phase0_contracts.py",
        SCRIPTS / "test_phase1_context_compile.py",
        SCRIPTS / "test_phase1_fleet.py",
        SCRIPTS / "test_phase1_fleet_approval.py",
        SCRIPTS / "test_phase1_fleet_runtime.py",
        SCRIPTS / "test_phase1_usage.py",
        SCRIPTS / "test_swarm_coordinator.py",
        SCRIPTS / "test_workspace_entry.py",
        Path(__file__),
    ])


def test_launch_guard_rejects_syntax_shell_and_alias_negative_fixtures(tmp_path):
    bad_alias = tmp_path / "alias.py"
    bad_alias.write_text(
        "import subprocess as sp\nsp.run(['echo', 'bad'])\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="lacks shared spawn flags"):
        _assert_launch_inventory([bad_alias])

    bad_import = tmp_path / "import_alias.py"
    bad_import.write_text(
        "from subprocess import Popen as launch_child\nlaunch_child(['echo', 'bad'])\n",
        encoding="utf-8")
    with pytest.raises(AssertionError, match="lacks shared spawn flags"):
        _assert_launch_inventory([bad_import])

    bad_syntax = tmp_path / "syntax.py"
    bad_syntax.write_text("if (\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="syntax error"):
        _assert_launch_inventory([bad_syntax])

    bad_shell = tmp_path / "shell.py"
    bad_shell.write_text(
        "import subprocess\n"
        "from _spawn import run_flags\n"
        "subprocess.run(['echo', 'bad'], shell=True, **run_flags())\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="shell=True"):
        _assert_launch_inventory([bad_shell])

    bad_os_alias = tmp_path / "os_alias.py"
    bad_os_alias.write_text("import os as operating\noperating.system('bad')\n",
                            encoding="utf-8")
    with pytest.raises(AssertionError, match="os.system"):
        _assert_launch_inventory([bad_os_alias])

    bad_os_import = tmp_path / "os_import.py"
    bad_os_import.write_text("from os import system as run_shell\nrun_shell('bad')\n",
                             encoding="utf-8")
    with pytest.raises(AssertionError, match="os.system"):
        _assert_launch_inventory([bad_os_import])


def test_discovery_generated_descendants_use_shared_flags(tmp_path):
    """Real Python child payloads are checked separately from quoted negative fixtures.

    These three lifecycle fixtures execute generated code. Their descendants must
    retain the leader's POSIX session, so use run_flags (not popen_flags).
    """
    tree = ast.parse((SCRIPTS / "test_discovery.py").read_text(encoding="utf-8"))
    fixtures = {
        "test_timeout_does_not_hang_on_grandchild_holding_stdout": "child",
        "test_v9_job_object_kills_a_tree_through_a_dead_leader": "leader_src",
        "test_v9_lifecycle_fixture_blocks_late_grandchild_writes": None,
    }
    checked = set()
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name not in fixtures:
            continue
        target = fixtures[function.name]
        payloads = []
        for node in ast.walk(function):
            if (target is not None and isinstance(node, ast.Assign)
                    and any(isinstance(name, ast.Name) and name.id == target
                            for name in node.targets)):
                payloads.append(node.value)
            elif (target is None and isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "helper" and node.func.attr == "write_text"):
                payloads.append(node.args[0])
        assert len(payloads) == 1, "generated launch fixture changed; audit its payload"
        payload = payloads[0]
        assert isinstance(payload, ast.JoinedStr)
        # Replace path interpolation with an inert literal; never evaluate source.
        code = "".join(value.value if isinstance(value, ast.Constant) else "'fixture'"
                       for value in payload.values)
        child_tree = ast.parse(code)
        launches = [node for node in ast.walk(child_tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "Popen"]
        assert len(launches) == 1, "generated lifecycle fixture lost its child launch"
        assert any(keyword.arg is None and isinstance(keyword.value, ast.Call)
                   and isinstance(keyword.value.func, ast.Name)
                   and keyword.value.func.id == "run_flags"
                   for keyword in launches[0].keywords), "descendant must inherit POSIX session"
        fixture = tmp_path / (function.name + ".py")
        fixture.write_text(code, encoding="utf-8")
        _assert_launch_inventory([fixture])
        checked.add(function.name)
    assert checked == set(fixtures), "generated launch inventory is incomplete"


def test_agy_generated_descendant_uses_shared_flags(tmp_path):
    """The fake AGY descendant is real executable test code, not a quoted fixture."""
    tree = ast.parse((SCRIPTS / "test_agy_1_1_22.py").read_text(encoding="utf-8"))
    payloads = []
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        if function.name != "test_proxy_timeout_kills_real_descendant_process_group":
            continue
        for node in ast.walk(function):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "write_text"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                payload = node.args[0].value
                if isinstance(payload, str) and "subprocess.Popen" in payload:
                    payloads.append(payload)
    assert len(payloads) == 1, "AGY generated launch fixture changed; audit its payload"
    child = ast.parse(payloads[0])
    launches = [node for node in ast.walk(child) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "Popen"]
    assert len(launches) == 1, "AGY fixture lost its descendant launch"
    assert any(keyword.arg is None and _shared_flags_call(keyword.value)
               for keyword in launches[0].keywords), (
                   "generated AGY descendant must use shared spawn flags")
    fixture = tmp_path / "agy_descendant.py"
    fixture.write_text(payloads[0], encoding="utf-8")
    _assert_launch_inventory([fixture])


def test_windows_silent_launch_module_is_in_normal_phase0_launch_gate():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    command = MANIFEST.REQUIRED_COMMANDS["phase0_launch"]
    assert "tests/test_windows_silent_launch.py" in ci
    assert "tests/test_windows_silent_launch.py" in command


def _console_snapshot():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    user32.IsWindowVisible.restype = ctypes.c_bool
    handle = int(kernel32.GetConsoleWindow() or 0)
    return {"handle": handle, "visible": bool(handle and user32.IsWindowVisible(handle))}


def _child_probe_code():
    return r'''
import ctypes, json, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32.GetConsoleWindow.restype = ctypes.c_void_p
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.IsWindowVisible.restype = ctypes.c_bool
handle = int(kernel32.GetConsoleWindow() or 0)
visible = bool(handle and user32.IsWindowVisible(handle))
print(json.dumps({"handle": handle, "visible": visible, "text": "snowman ☃"}, ensure_ascii=False), flush=True)
sys.stderr.write("stderr café\n")
sys.stderr.flush()
raise SystemExit(7)
'''


def _assert_probe_payload(completed, parent):
    assert completed.returncode == 7
    assert completed.stderr == "stderr café\n"
    payload = json.loads(completed.stdout)
    assert payload["text"] == "snowman ☃"
    assert payload["handle"] == 0
    assert payload["visible"] is False
    if parent["handle"]:
        assert payload["handle"] != parent["handle"]


@pytest.mark.skipif(os.name != "nt", reason="owned console visibility is Windows-only")
def test_windows_utility_run_is_headless_and_preserves_streams():
    parent = _console_snapshot()
    completed = subprocess.run(
        [sys.executable, "-c", _child_probe_code()],
        capture_output=True, text=True, encoding="utf-8", timeout=20,
        **SPAWN.run_flags(),
    )
    _assert_probe_payload(completed, parent)


@pytest.mark.skipif(os.name != "nt", reason="owned console visibility is Windows-only")
def test_windows_worker_popen_is_headless_and_preserves_streams():
    parent = _console_snapshot()
    process = subprocess.Popen(
        [sys.executable, "-c", _child_probe_code()],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", **SPAWN.popen_flags(),
    )
    try:
        stdout, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    _assert_probe_payload(SimpleNamespace(
        returncode=process.returncode, stdout=stdout, stderr=stderr), parent)


@pytest.mark.skipif(os.name != "nt", reason="owned console visibility is Windows-only")
def test_windows_detached_child_is_headless_and_writes_private_fixture(tmp_path):
    marker = tmp_path / "detached-result.json"
    code = (
        "import ctypes,json,pathlib\n"
        "k=ctypes.WinDLL('kernel32', use_last_error=True); u=ctypes.WinDLL('user32', use_last_error=True)\n"
        "k.GetConsoleWindow.restype=ctypes.c_void_p; u.IsWindowVisible.argtypes=[ctypes.c_void_p]\n"
        "h=int(k.GetConsoleWindow() or 0); v=bool(h and u.IsWindowVisible(h))\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{'handle':h,'visible':v}}, sort_keys=True), encoding='utf-8')\n"
        "raise SystemExit(7)\n"
    )
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", code], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **SPAWN.popen_flags(detached=True),
        )
        assert process.wait(timeout=20) == 7
        payload = json.loads(marker.read_text(encoding="utf-8"))
        assert payload == {"handle": 0, "visible": False}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_batch_hop_preserves_owned_probe_streams_and_exit(tmp_path):
    if shutil.which("py") is None:
        pytest.skip("Python launcher 'py' is unavailable")
    child = tmp_path / "child.py"
    child.write_text(_child_probe_code(), encoding="utf-8")
    batch = tmp_path / "standin.cmd"
    batch.write_text(
        "@echo off\r\n"
        "py -3 \"%~dp0child.py\"\r\n"
        "exit /b %ERRORLEVEL%\r\n",
        encoding="utf-8",
        newline="",
    )
    parent = _console_snapshot()
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "call", str(batch)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, **SPAWN.run_flags(),
    )
    _assert_probe_payload(completed, parent)


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_cmd_prompt_file_dry_run_preserves_utf8_and_no_provider(tmp_path):
    if shutil.which("py") is None:
        pytest.skip("Python launcher 'py' is unavailable")
    roster = tmp_path / "roster"
    roster.mkdir()
    (roster / "pf-probe.md").write_text(
        "---\nrun-agent: codex\npermission: read-only\n---\n# probe\n", encoding="utf-8")
    prompt = tmp_path / "task.md"
    prompt.write_text("Review THE-MAGIC-TOKEN carefully → now", encoding="utf-8-sig")
    wrapper = SCRIPTS / "summon.cmd"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper),
         "--agent", "pf-probe", "--prompt-file", str(prompt),
         "--cwd", str(ROOT), "--agents-dir", str(roster), "--dry-run"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, **SPAWN.run_flags(),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["dry_run"] is True
    assert report["provider_contacted"] is False
    assert any("THE-MAGIC-TOKEN" in item for item in report["args"])
    assert any("→" in item for item in report["args"])
    assert not any("\ufeff" in item for item in report["args"])


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher contract")
def test_windows_cmd_usage_status_is_provider_inert_json(tmp_path):
    if shutil.which("py") is None:
        pytest.skip("Python launcher 'py' is unavailable")
    wrapper = SCRIPTS / "summon.cmd"
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", "call", str(wrapper),
         "usage", "status", "--cache", str(tmp_path / "missing.json"), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, **SPAWN.run_flags(),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["cache_state"] == "missing"
    assert report["provider_contacted"] is False


@pytest.mark.skipif(os.name != "nt", reason="PowerShell caller contract")
def test_powershell_processstartinfo_recipe_executes_provider_inert_call(tmp_path):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell 7 is unavailable")
    if shutil.which("py") is None:
        pytest.skip("Python launcher 'py' is unavailable")
    script = tmp_path / "silent-caller.ps1"
    script.write_text(
        r'''
param([string]$Wrapper, [string]$Cache)
$psi = [System.Diagnostics.ProcessStartInfo]::new()
$psi.FileName = $env:ComSpec
[void]$psi.ArgumentList.Add('/d')
[void]$psi.ArgumentList.Add('/s')
[void]$psi.ArgumentList.Add('/c')
[void]$psi.ArgumentList.Add('call')
[void]$psi.ArgumentList.Add($Wrapper)
[void]$psi.ArgumentList.Add('usage')
[void]$psi.ArgumentList.Add('status')
[void]$psi.ArgumentList.Add('--cache')
[void]$psi.ArgumentList.Add($Cache)
[void]$psi.ArgumentList.Add('--json')
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $psi
try {
    [void]$process.Start()
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        try { $process.Kill($true) }
        finally { [void]$process.WaitForExit(5000) }
        throw 'Summon caller timed out'
    }
    $process.WaitForExit()
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0) { throw $stderr }
    $stdout
}
finally {
    $process.Dispose()
}
''', encoding="utf-8")
    completed = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(script), str(SCRIPTS / "summon.cmd"),
         str(tmp_path / "missing.json")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=40, **SPAWN.run_flags(),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["cache_state"] == "missing"
    assert report["provider_contacted"] is False

"""Provider-free parent-chat lifecycle acceptance.

This packet exercises the real ``ConversationRuntime`` parent, its dispatcher
child, the chat launch guard and an owned native Windows executable.  The
executable is compiled into a temporary directory during the test; it emits a
fixed Claude-shaped envelope and never contacts a provider.  Using a native
executable avoids the command-wrapper argument admission seam while preserving
the production ``--max-permission`` ceiling and launch-observation checks.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Production modules are imported only after the hermetic boundary fixture has
# installed its environment/audit fence.  This keeps import-time discovery from
# consulting the user's provider configuration or shell state.
ConversationJournal = None
ConversationRuntime = None
issue_qualification = None
family_path = None
read_family = None
material_contract_for = None
revocation_id_for = None

_ACTIVE = False
_BOUNDARY_ROOT: Path | None = None
_ALLOWED_COMPILER: Path | None = None
_ALLOWED_DISPATCHER: Path | None = None
_ALLOWED_NATIVE: Path | None = None
_PROCESSES: list[subprocess.Popen] = []
_AUDIT_HITS: list[str] = []
_READABLE_ROOTS: tuple[Path, ...] = ()


def _inside(path: Path, root: Path | None) -> bool:
    return root is not None and (path == root or root in path.parents)


def _deny_boundary(message: str) -> None:
    _AUDIT_HITS.append(message)
    raise AssertionError("parent-chat fixture crossed a forbidden boundary")


def _parent_audit(event: str, args: tuple[object, ...]) -> None:
    """Fail closed on network, credential, and non-owned process/file edges."""
    if not _ACTIVE:
        return
    if event == "subprocess.Popen":
        executable = args[0] if args else None
        argv = args[1] if len(args) > 1 else None
        cwd = args[2] if len(args) > 2 else None
        env = args[3] if len(args) > 3 else None
        if executable is None:
            if isinstance(argv, (list, tuple)) and argv:
                executable = argv[0]
            elif isinstance(argv, str) and argv.strip():
                executable = argv.strip().split(" ", 1)[0].strip('"')
        try:
            executable_path = Path(os.fsdecode(executable)).resolve()
        except (TypeError, OSError):
            _deny_boundary("unresolvable process executable")
            return
        command = ([str(item) for item in argv] if isinstance(argv, (list, tuple))
                   else [argv] if isinstance(argv, str) else [])
        if _ALLOWED_COMPILER is not None and executable_path == _ALLOWED_COMPILER:
            pass
        elif (_ALLOWED_DISPATCHER is not None
              and executable_path == Path(sys.executable).resolve()
              and (
                  any(Path(item).resolve() == _ALLOWED_DISPATCHER for item in command[1:2])
                  or (_ALLOWED_DISPATCHER.as_posix().lower().replace("/", "\\")
                      in command[0].lower().replace("/", "\\"))
              )):
            command_text = command[0].lower().replace("/", "\\") if command else ""
            if cwd is None:
                # ConversationRuntime deliberately leaves the dispatcher OS
                # cwd unchanged; the authenticated --cwd argument is the
                # actual project boundary.  Require that explicit argument to
                # point inside this owned fixture root.
                if str(_BOUNDARY_ROOT).lower().replace("/", "\\") not in command_text:
                    _deny_boundary("dispatcher --cwd escaped the owned fixture root")
            elif not _inside(Path(cwd).resolve(), _BOUNDARY_ROOT):
                _deny_boundary("dispatcher cwd escaped the owned fixture root: " + repr(cwd))
            # The audit event on Windows may normalize a list launch into a
            # command string and omit the keyword env.  The wrapped Popen
            # below checks the concrete kwargs before the OS call; this hook
            # therefore limits itself to executable/cwd/source identity.
            process = None  # populated by the fixture's wrapped Popen below
        else:
            _deny_boundary("unapproved process launch: " + executable_path.name)
        return
    if event in {"socket.connect", "socket.sendto", "os.system", "os.startfile"}:
        _deny_boundary(f"forbidden external boundary: {event}")
    if event == "open" and args and not isinstance(args[0], int):
        raw_path = os.fsdecode(args[0])
        # subprocess opens the Windows null device while wiring DEVNULL;
        # that handle is an intentional inert sink, not a fixture mutation.
        if os.path.normcase(raw_path) == os.path.normcase(os.devnull) or Path(raw_path).name.lower() == "nul":
            return
        try:
            path = Path(raw_path).resolve()
        except (TypeError, OSError):
            _deny_boundary("unresolvable file path")
            return
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = any(char in str(mode) for char in "wax+") or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        allowed_read = any(_inside(path, root) for root in _READABLE_ROOTS)
        if writing and not _inside(path, _BOUNDARY_ROOT):
            _deny_boundary("write escaped the owned fixture root")
        if not writing and not allowed_read and path != Path(os.devnull).resolve():
            _deny_boundary("read escaped the reviewed source/fixture roots")
    if event in {"os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.rename", "os.replace"}:
        paths = args[:2] if event in {"os.rename", "os.replace"} else args[:1]
        for raw in paths:
            if isinstance(raw, (str, os.PathLike)) and not _inside(Path(raw).resolve(), _BOUNDARY_ROOT):
                _deny_boundary("filesystem mutation escaped the owned fixture root")


sys.addaudithook(_parent_audit)


pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="owned native executable fixture requires Windows")


@pytest.fixture(autouse=True)
def _hermetic_parent_boundary(tmp_path, monkeypatch):
    """Install a temporary-only parent boundary before production imports."""
    global _ACTIVE, _BOUNDARY_ROOT, _ALLOWED_DISPATCHER, _ALLOWED_COMPILER, _ALLOWED_NATIVE, _READABLE_ROOTS
    global ConversationJournal, ConversationRuntime, issue_qualification
    global family_path, read_family, material_contract_for, revocation_id_for
    _BOUNDARY_ROOT = tmp_path.resolve()
    system_root = os.environ.get("SystemRoot", os.environ.get("WINDIR", r"C:\Windows"))
    isolated = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "HOME": str(_BOUNDARY_ROOT),
        "USERPROFILE": str(_BOUNDARY_ROOT),
        "APPDATA": str(_BOUNDARY_ROOT / "AppData"),
        "LOCALAPPDATA": str(_BOUNDARY_ROOT / "LocalAppData"),
        "TEMP": str(_BOUNDARY_ROOT),
        "TMP": str(_BOUNDARY_ROOT),
        "PATH": "",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "SUMMON_TELEMETRY": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    _ALLOWED_DISPATCHER = (HERE / "run_subagent.py").resolve()
    _READABLE_ROOTS = tuple({
        HERE.resolve(), HERE.parents[2].resolve(), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(),
        Path(system_root).resolve(),
        _BOUNDARY_ROOT,
    })
    _AUDIT_HITS.clear()
    _PROCESSES.clear()
    original_popen = subprocess.Popen
    def owned_popen(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        rendered = (" ".join(str(item) for item in command)
                    if isinstance(command, (list, tuple)) else str(command or ""))
        normalized = rendered.lower().replace("/", "\\")
        if (_ALLOWED_DISPATCHER is not None
                and str(_ALLOWED_DISPATCHER).lower().replace("/", "\\") in normalized
                and "python" in normalized):
            child_env = kwargs.get("env")
            if not isinstance(child_env, dict):
                _deny_boundary("dispatcher did not receive an explicit environment")
            for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP"):
                if not _inside(Path(str(child_env.get(name, ""))).resolve(), _BOUNDARY_ROOT):
                    _deny_boundary(
                        f"dispatcher {name} escaped the owned fixture root; "
                        f"keys={sorted(key for key in child_env if key in {'HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP'})!r}")
        process = original_popen(*args, **kwargs)
        _PROCESSES.append(process)
        return process
    monkeypatch.setattr(subprocess, "Popen", owned_popen)
    isolated["SUMMON_PARENT_FIXTURE_MODE"] = "fast"
    with patch.dict(os.environ, isolated, clear=True):
        _ACTIVE = True
        try:
            # These imports are intentionally after the temporary boundary is live.
            from _chat_launch_qualification import issue as _issue
            from _chat_source_family import family_path as _family_path, read as _read_family
            from _conversation import ConversationJournal as _Journal
            from _conversation_runtime import ConversationRuntime as _Runtime
            from _launch_qualification import material_contract_for as _material_contract_for
            from _launch_qualification import revocation_id_for as _revocation_id_for
            ConversationJournal, ConversationRuntime = _Journal, _Runtime
            issue_qualification = _issue
            family_path, read_family = _family_path, _read_family
            material_contract_for, revocation_id_for = _material_contract_for, _revocation_id_for
            yield
            assert not _AUDIT_HITS, "a caught exception concealed a forbidden parent boundary"
        finally:
            for process in list(_PROCESSES):
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise AssertionError(
                        "owned parent-chat child did not reap within cleanup bound") from exc
            _PROCESSES.clear()
            _ACTIVE = False
            _BOUNDARY_ROOT = None
            _ALLOWED_DISPATCHER = None
            _ALLOWED_COMPILER = None
            _ALLOWED_NATIVE = None
            _READABLE_ROOTS = ()


def _compiler() -> Path | None:
    system_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        system_root / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe",
        system_root / "Microsoft.NET" / "Framework" / "v4.0.30319" / "csc.exe",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _isolated_env(tmp_path: Path, *, registry: Path | None = None) -> dict[str, str]:
    """Return the smallest environment needed by the owned fixture.

    The parent/dispatcher test must not inherit a user's provider credentials,
    PATH shims, shell startup hooks, or unrelated profile state.  ``SystemRoot``
    and the temporary directory variables are the only host facts the native
    compiler/child need; the fixture executable is always passed by absolute
    path through the synthetic profile.
    """
    system_root = os.environ.get("SystemRoot", os.environ.get("WINDIR", r"C:\Windows"))
    env = {
        "SystemRoot": system_root,
        "WINDIR": system_root,
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "AppData"),
        "LOCALAPPDATA": str(tmp_path / "LocalAppData"),
        "TEMP": str(tmp_path),
        "TMP": str(tmp_path),
        "PATH": "",
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "COMSPEC": str(Path(system_root) / "System32" / "cmd.exe"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if registry is not None:
        env.update({
            "SUMMON_PROFILES_FILE": str(registry),
            "SUMMON_TELEMETRY": "0",
            "SUMMON_PARENT_FIXTURE_MODE": "fast",
        })
    return env


def _compile_owned_cli(tmp_path: Path) -> Path:
    global _ALLOWED_COMPILER
    compiler = _compiler()
    if compiler is None:
        pytest.skip("owned parent-chat fixture requires the .NET Framework csc.exe")
    _ALLOWED_COMPILER = compiler.resolve()
    source = tmp_path / "owned_parent_fixture.cs"
    executable = tmp_path / "vendor" / "claude.exe"
    executable.parent.mkdir()
    source.write_text(
        r'''using System;
using System.Threading;

internal static class Program
{
    public static int Main(string[] args)
    {
        foreach (string arg in args)
        {
            if (arg == "--version")
            {
                Console.WriteLine("claude 9.9.9");
                return 0;
            }
        }

        if (Environment.GetEnvironmentVariable("SUMMON_PARENT_FIXTURE_MODE") == "interrupt")
        {
            Thread.Sleep(30000);
            return 0;
        }

        Console.WriteLine("{\"status\":\"success\",\"result\":\"STATUS: COMPLETE\\nVERDICT: PASS\\nHANDOFF: parent fixture result\",\"model\":\"fixture-model\",\"session_id\":\"fixture-session\"}");
        return 0;
    }
}
''',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", "/debug-",
         f"/out:{executable}", str(source)],
        cwd=str(tmp_path), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=_isolated_env(tmp_path),
    )
    assert completed.returncode == 0, (
        "owned native parent-chat fixture failed to compile: "
        + (completed.stderr or completed.stdout)[-2000:])
    assert executable.is_file()
    return executable


def _fixture(tmp_path: Path):
    global _ALLOWED_NATIVE
    project = tmp_path / "project"
    rooms = tmp_path / "rooms"
    roster = tmp_path / "agents"
    profile = tmp_path / "profile"
    for path in (project, rooms, roster, profile):
        path.mkdir()
    executable = _compile_owned_cli(tmp_path)
    _ALLOWED_NATIVE = executable.resolve()
    (roster / "worker.md").write_text(
        "---\nrun-agent: claude\nmodel: fixture-model\n"
        "model-policy: exact\npermission: read-only\n"
        "profile: fixture-profile\n---\n\n"
        "Only the owned deterministic fixture is admitted.\n",
        encoding="utf-8",
    )
    registry = tmp_path / "profiles.json"
    registry.write_text(json.dumps({"profiles": {"fixture-profile": {
        "cli": "claude", "config_dir": str(profile),
        "command": str(executable), "models": ["fixture-model"],
    }}}), encoding="utf-8")
    for session_id in ("room-1", "interrupt-room"):
        ConversationJournal.create(
            rooms, session_id=session_id, project_id="fixture",
            project_root=project, initiator_host="codex", initiator_agent="human",
            mode="chat", participants=[{
                "agent": "worker", "role": "researcher", "name": "Worker",
                "version": "fixture", "permission": "read-only",
            }],
        )
    runtime = ConversationRuntime(
        rooms, cwd=project, agents_dir=roster, dispatcher=HERE / "run_subagent.py",
        timeout_ms=10_000, strict_agents_dir=True,
    )
    return runtime, rooms, project, profile, roster, registry, executable


def _last_observation(runtime: ConversationRuntime, rooms: Path, session_id: str):
    journal = ConversationJournal.open(rooms, session_id)
    started, finished = runtime._last_turn(journal.events(native=True), "worker")
    assert started is not None and finished is not None
    observation = finished.get("payload", {}).get("launch_observation")
    assert isinstance(observation, dict)
    return started["payload"]["turn_id"], observation


def _qualification(runtime: ConversationRuntime, rooms: Path, session_id: str,
                   turn_id: str, observation: dict) -> dict:
    owner_dir = runtime._runtime_owner_dir(session_id, "worker", create=False)
    source = read_family(family_path(owner_dir))
    fields = {
        key: observation[key] for key in (
            "backend", "transport", "registry_generation", "registry_digest",
            "adapter", "adapter_version", "external_cli_version",
            "executable_sha256", "launch_material_sha256")}
    fields["material_contract"] = material_contract_for(
        backend=observation["backend"], transport=observation["transport"],
        adapter=observation["adapter"], adapter_version=observation["adapter_version"])
    fields["revocation_id"] = revocation_id_for(dict(fields, operation="chat_resume"))
    return issue_qualification(
        source_family_id=source["source_family_id"], turn_id=turn_id,
        observation=observation, expires_at=time.time() + 600,
        token=source["nonce"], **fields)


def _wait_for_finish(rooms: Path, session_id: str, *, timeout: float = 8.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = ConversationJournal.open(rooms, session_id).events(native=True)
        finished = [event for event in events if event.get("event") == "turn_finished"]
        if finished:
            return finished[-1]
        time.sleep(0.05)
    raise AssertionError("parent-chat fixture did not publish a bounded turn finish")


def test_parent_chat_handoff_reopen_and_interruption_use_owned_native_child(tmp_path):
    runtime, rooms, project, profile, roster, registry, executable = _fixture(tmp_path)
    del project, profile, roster, executable
    restarted_runtime = None
    env = _isolated_env(rooms.parent, registry=registry)
    try:
        with patch.dict(os.environ, env, clear=True):
            first = runtime.start_turn("room-1", "worker", "first", wait=True)
            assert first["status"] == "success"
            assert first["model_served"] == "fixture-model"
            first_turn, observation = _last_observation(runtime, rooms, "room-1")
            assert observation["external_cli_version"] == "claude/9.9.9"
            assert observation["backend"] == "claude"
            assert observation["transport"] == "subprocess"
            assert _ALLOWED_NATIVE is not None
            assert observation["executable_sha256"] == hashlib.sha256(
                _ALLOWED_NATIVE.read_bytes()).hexdigest()
            private_first = ConversationJournal.open(rooms, "room-1").events(native=True)
            first_finish = next(event for event in private_first
                                if event["event"] == "turn_finished")
            first_payload = first_finish["payload"]
            economics = first_payload.get("turn_economics")
            summary = first_payload.get("turn_economics_summary")
            assert isinstance(economics, dict)
            assert isinstance(summary, dict)
            assert first["turn_id"] == first_turn == economics["turn"]["turn_id"]
            assert economics["turn"]["attempt_id"] == economics["submission"]["attempt"]["id"]
            assert economics["turn"]["owner_generation"] >= 1
            assert economics["submission"]["submission_state"] == "possible"
            assert economics["submission"]["unknown_spend"] is True
            assert summary["submission_state"] == "possible"
            assert summary["provider_contact"] == "unknown"
            assert summary["unknown_spend"] is True
            public_first = ConversationJournal.open(rooms, "room-1").events()
            public_finish = next(event for event in public_first
                                 if event["event"] == "turn_finished")
            public_payload = public_finish["payload"]
            assert "turn_economics" not in public_payload
            assert public_payload["turn_economics_summary"] == summary
            assert "attempt" not in public_payload["turn_economics_summary"]

            handoff = runtime.revalidate_chat_source(
                "room-1", "worker", turn_id=first_turn,
                observation=observation,
                qualification=_qualification(runtime, rooms, "room-1",
                                             first_turn, observation),
            )
            assert handoff["provider_contacted"] is False
            assert handoff["launch_started"] is False

            resumed = runtime.start_turn("room-1", "worker", "continue", wait=True)
            assert resumed["status"] == "success"
            assert resumed["resumed"] is True
            second_turn, second_observation = _last_observation(runtime, rooms, "room-1")
            runtime.revalidate_chat_source(
                "room-1", "worker", turn_id=second_turn,
                observation=second_observation,
                qualification=_qualification(runtime, rooms, "room-1",
                                             second_turn, second_observation),
            )
            reopened = runtime.start_turn("room-1", "worker", "reopen", wait=True)
            assert reopened["status"] == "success"
            assert reopened["resumed"] is True

            # Keep the interrupt marker set until the worker has published its
            # cancellation.  The child inherits its environment at Popen time;
            # restoring it immediately after start_turn() would race that launch.
            with patch.dict(os.environ, {"SUMMON_PARENT_FIXTURE_MODE": "interrupt"}, clear=False):
                interrupted_start = runtime.start_turn(
                    "interrupt-room", "worker", "interrupt", wait=False)
                assert interrupted_start["status"] == "started"
                cancellation = runtime.cancel_turn("interrupt-room", "worker")
                assert cancellation["durable"] is True
                interrupted_finish = _wait_for_finish(rooms, "interrupt-room")
                assert interrupted_finish["payload"]["status"] == "cancelled"
                interrupted_summary = interrupted_finish["payload"].get("turn_economics_summary")
                assert isinstance(interrupted_summary, dict)
                assert interrupted_summary["submission_state"] in {"possible", "indeterminate"}
                assert interrupted_summary["provider_contact"] == "unknown"
                assert interrupted_summary["unknown_spend"] is True

            # Reopen from a fresh parent runtime, not merely the same in-memory
            # coordinator.  The journal is the durable source of the cancelled
            # turn and must preserve its possible/unknown accounting after a
            # process restart before a new turn is admitted.
            runtime.close(timeout=5)
            restarted_runtime = ConversationRuntime(
                rooms, cwd=runtime.cwd, agents_dir=runtime.agents_dir,
                dispatcher=HERE / "run_subagent.py", timeout_ms=10_000,
                strict_agents_dir=True,
            )
            reopened_after_interrupt = restarted_runtime.start_turn(
                "interrupt-room", "worker", "reopen after interruption", wait=True)
            assert reopened_after_interrupt["status"] == "success"
            assert reopened_after_interrupt["resumed"] is False
            interrupt_events = ConversationJournal.open(
                rooms, "interrupt-room").events(native=True)
            interrupted_finished = [event for event in interrupt_events
                                    if event["event"] == "turn_finished"]
            assert len(interrupted_finished) == 2
            prior_summary = interrupted_finished[0]["payload"]["turn_economics_summary"]
            assert prior_summary["submission_state"] in {"possible", "indeterminate"}
            assert prior_summary["unknown_spend"] is True
            assert all(process.poll() is not None for process in _PROCESSES)
    finally:
        if restarted_runtime is not None:
            restarted_runtime.close(timeout=5)
        runtime.close(timeout=5)

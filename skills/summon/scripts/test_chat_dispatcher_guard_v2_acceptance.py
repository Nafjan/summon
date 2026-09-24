"""Actual public argv dispatcher/executor -> v2 authenticated chat guard.

Real create_v2 supplies parent lease/prompt/attempt; the executor produces all
launch evidence. This tests a fresh chat launch, not ConversationRuntime child
scheduling, native continuation qualification, or installed provider admission.
Only profile-command admission and workspace/git observation are adapted;
changes at executor entry are deliberate post-reservation fault injection.
"""
from pathlib import Path
import builtins
import contextlib
import copy
import importlib.util
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
# Load the Windows asyncio subclass definitions before denying process creation.
from unittest import mock

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
_ACTIVE = False
_WRITABLE = None
_HITS = []
_PROCESSES = []
_ALLOW_SPAWN = False
_OWNED_CLI = None
_CMD = None
_READABLE = [HERE.resolve(), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
for _dependency in ('pytest', '_pytest', 'pluggy', 'packaging', 'iniconfig', 'colorama', 'pygments'):
    _spec = importlib.util.find_spec(_dependency)
    if _spec and _spec.origin:
        _READABLE.append(Path(_spec.origin).resolve().parent)
_DENIED_IMPORTS = ('_auth', '_credentials', '_windows_credentials', '_nous_credentials',
                   '_arkcli_creds', 'keyring', 'win32cred')


def _inside(path, root):
    return root is not None and (path == root or root in path.parents)


def _denied(*args, **kwargs):
    _HITS.append('forbidden_boundary:' + sys._getframe(1).f_code.co_name + ':' + str(sys._getframe(1).f_lineno) + ':' + sys._getframe(2).f_code.co_name + ':' + sys._getframe(3).f_code.co_name)
    raise AssertionError('synthetic strict-entity boundary refused')


def _audit(event, args):
    if not _ACTIVE:
        return
    if event == 'subprocess.Popen':
        if not _ALLOW_SPAWN:
            _denied()
    elif event.startswith(('subprocess.', 'socket.', 'os.spawn', 'os.exec')) or event in {'os.system', 'os.startfile'}:
        _denied()
    if event == 'import' and args and str(args[0]).startswith(_DENIED_IMPORTS):
        _denied()
    if event == 'open' and args and not isinstance(args[0], int):
        if os.fsdecode(args[0]).lower() == os.devnull.lower():
            return
        path = Path(os.fsdecode(args[0])).resolve()
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = any(char in (args[1] or '') for char in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        if writing and not _inside(path, _WRITABLE):
            _denied()
        if not writing and not (_inside(path, _WRITABLE) or any(_inside(path, root) for root in _READABLE) or path == _CMD):
            _denied()
    if event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'}:
        paths = args[:2] if event == 'os.rename' else args[:1]
        if any(not _inside(Path(os.fsdecode(path)).resolve(), _WRITABLE) for path in paths):
            _denied()


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def inert(tmp_path, monkeypatch):
    global _ACTIVE, _WRITABLE, _CMD
    system = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in system.items():
        monkeypatch.setenv(key, value)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
        monkeypatch.setenv(key, str(tmp_path))
    _CMD = (Path(system['SystemRoot']) / 'System32' / 'cmd.exe').resolve()
    monkeypatch.setenv('COMSPEC', str(_CMD))
    monkeypatch.setenv('PATH', '')
    monkeypatch.setenv('SUMMON_TELEMETRY', '0')
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(_DENIED_IMPORTS):
            _denied()
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    original_popen = subprocess.Popen
    class OwnedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            global _ALLOW_SPAWN
            if not isinstance(args, (list, tuple)) or len(args) < 5:
                _denied()
            if Path(args[0]).resolve() != _CMD or '/c' not in args:
                _denied()
            command_index = list(args).index('/c')
            if list(args[command_index-2:command_index+1]) != ['/d', '/s', '/c']:
                _denied()
            script = Path(args[command_index+1]).resolve()
            if _OWNED_CLI is None or script != _OWNED_CLI[0] or hashlib.sha256(script.read_bytes()).hexdigest() != _OWNED_CLI[1]:
                _denied()
            if not _inside(Path(kwargs.get('cwd', '')).resolve(), _WRITABLE):
                _denied()
            env = kwargs.get('env')
            if not isinstance(env, dict) or not _inside(Path(env.get('USERPROFILE', '')).resolve(), _WRITABLE):
                _denied()
            if not kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW:
                _denied()
            if kwargs.get('shell') or any(any(char in str(arg) for char in '&|<>') for arg in args[1:] if arg != str(script)):
                _denied()
            _ALLOW_SPAWN = True
            try:
                super().__init__(args, *positional, **kwargs)
            finally:
                _ALLOW_SPAWN = False
            _PROCESSES.append(self)
    monkeypatch.setattr(subprocess, 'Popen', OwnedPopen)
    monkeypatch.setattr(socket.socket, 'connect', _denied)
    monkeypatch.setattr(os, 'kill', _denied)
    _HITS.clear(); _PROCESSES.clear(); _WRITABLE = tmp_path.resolve(); _ACTIVE = True
    try:
        yield
        assert not _HITS, 'a caught exception concealed a forbidden boundary'
    finally:
        try:
            for process in _PROCESSES:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            assert not _HITS, 'a caught exception concealed a forbidden boundary'
        finally:
            _ACTIVE = False; _WRITABLE = None
            _PROCESSES.clear()





pytestmark = pytest.mark.skipif(os.name != 'nt', reason='owned marked Windows CLI fixture')


@pytest.mark.parametrize('change', ['supported', 'prompt', 'physical_attempt', 'replay'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_public_dispatcher_v2_guard_uses_actual_payload_and_physical_attempt(tmp_path, monkeypatch, capsys, change):
    global _OWNED_CLI
    from dataclasses import replace
    import _receipt
    monkeypatch.setattr(_receipt, 'workspace_snapshot', lambda *args, **kwargs: None)
    monkeypatch.setattr(_receipt, 'git_head', lambda *args, **kwargs: None)
    import _profiles
    import _executor
    import _rundir
    import _context_policy
    import _chat_launch_guard as guard
    import run_subagent

    workspace, profile, roster, owner_root = (tmp_path / n for n in ('workspace', 'profile', 'agents', 'owner'))
    for path in (workspace, profile, roster, owner_root):
        path.mkdir()
    script = tmp_path / 'fake-claude.cmd'
    script.write_text(
        '@echo off\nREM SUMMON_CLAUDE_ENTRYPOINT_V1\n'
        'if "%1"=="--version" (echo claude 9.9.9&exit /b 0)\n'
        'echo invoked>> "%~dp0body-argv.txt"\n'
        'echo {"type":"result","result":"STATUS: COMPLETE VERDICT: PASS HANDOFF: done","model":"claude-opus-5","session_id":"fixture"}\n', encoding='utf-8')
    _OWNED_CLI = (script.resolve(), hashlib.sha256(script.read_bytes()).hexdigest())
    registry = tmp_path / 'profiles.json'
    registry.write_text(json.dumps({'profiles': {'fixture-profile': {
        'cli': 'claude', 'config_dir': str(profile), 'command': str(_CMD),
        'models': ['claude-opus-5']}}}), encoding='utf-8')
    monkeypatch.setenv('SUMMON_PROFILES_FILE', str(registry))
    def admit_owned_command(raw, cli, cwd):
        assert raw == str(_CMD) and cli == 'claude' and cwd == str(workspace)
        return raw, hashlib.sha256(raw.encode()).hexdigest()[:32]
    monkeypatch.setattr(_profiles, '_profile_command', admit_owned_command)
    (roster/'fixture-seat.md').write_text(
        '---\nrun-agent: claude\npermission: read-only\nmodel: claude-opus-5\n'
        'model-policy: exact\nprofile: fixture-profile\n'
        f'args: /d /s /c "{script}"\n---\nOnly the inert fixture runs.\n', encoding='utf-8')
    prompt, attempt = 'Synthetic guarded prompt reaches CLI', 'a'*32
    owner = _rundir.acquire_owner(str(owner_root), 120)
    path, token = str(owner_root/'guard.json'), 'synthetic-guard-token'
    guard.create_v2(path, token, expected=None, session_id='room', participant='worker',
        policy=_context_policy.make('off', policy_id='chat-context'),
        payload_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        context_selection_sha256=hashlib.sha256(b'[]').hexdigest(),
        backend='claude', transport='subprocess', turn_id='turn-a',
        owner_generation=owner.generation, owner_nonce=owner.nonce, attempt_id=attempt)
    for key, value in {'PATH':'', 'SUMMON_CHAT_LAUNCH_GUARD_PATH':path,
            'SUMMON_CHAT_LAUNCH_GUARD_TOKEN':token, 'SUMMON_CHAT_LAUNCH_GUARD_VERSION':'2',
            'SUMMON_CHAT_LAUNCH_ATTEMPT_ID':attempt}.items():
        monkeypatch.setenv(key, value)
    for name in ('_JOB_FILE', '_GOVERNED_RESUME_LINEAGE', '_EMIT_OPERATION'):
        monkeypatch.setattr(run_subagent, name, getattr(run_subagent, name))
    produced, physical, seen = [], [], []
    execute = _executor.execute_agent
    before = guard.before_launch_v2
    def observe_real_guard(*args, **kwargs):
        seen.append(copy.deepcopy(args[2]))
        return before(*args, **kwargs)
    monkeypatch.setattr(guard, 'before_launch_v2', observe_real_guard)
    def actual_execute(invocation, **kwargs):
        assert invocation.attempt_id == attempt, 'dispatcher did not adopt parent physical attempt'
        assert invocation.prompt == prompt
        if change == 'prompt':
            invocation = replace(invocation, prompt='Changed synthetic prompt')
        elif change == 'physical_attempt':
            invocation = replace(invocation, attempt_id='b'*32)
        physical.append(invocation)
        result = execute(invocation, **kwargs)
        produced.append(copy.deepcopy(result))
        return result
    monkeypatch.setattr(run_subagent, 'execute_agent', actual_execute)
    argv = ['run_subagent.py', 'dispatch', '--agent', 'fixture-seat', '--prompt', prompt,
        '--cwd', str(workspace), '--agents-dir', str(roster), '--strict-agents-dir',
        '--no-contract-repair', '--timeout', '5s', '--out', str(tmp_path/'out.json')]
    monkeypatch.setattr(sys, 'argv', argv)
    try:
        runs = 2 if change == 'replay' else 1
        pending = Path(path).read_bytes()
        for index in range(runs):
            with pytest.raises(SystemExit) as stopped:
                run_subagent.main()
            captured = capsys.readouterr()
            public = json.loads(captured.out)
            out = json.loads((tmp_path/'out.json').read_text())
            result = produced[-1]
            accepted = change == 'supported' or (change == 'replay' and index == 0)
            assert public['status'] == out['status'] == result['status']
            assert public['provider_contacted'] is out['provider_contacted'] is accepted
            assert '_private_launch_observation' not in captured.out
            assert '_private_launch_observation' not in json.dumps(out)
            assert token not in captured.out + captured.err + json.dumps(out)
            assert seen[-1]['dispatch_payload_sha256'] == hashlib.sha256(physical[-1].prompt.encode()).hexdigest()
            assert seen[-1]['attempt_id_sha256'] == hashlib.sha256(physical[-1].attempt_id.encode()).hexdigest()
            if accepted:
                assert stopped.value.code == 0 and result['status'] == 'success'
                assert result['attempt_id'] == attempt and result['attempts'] == 1
                assert seen[-1]['launch_observation']['external_cli_version'] == 'claude/9.9.9'
                receipt = guard.read_v2(path, token)
                assert receipt['attempt_id'] == attempt
                assert receipt['payload_sha256'] == hashlib.sha256(prompt.encode()).hexdigest()
                body_process = next(proc for proc in _PROCESSES if '-p' in proc.args)
                assert body_process.args[body_process.args.index('-p') + 1] == prompt
                assert len((tmp_path/'body-argv.txt').read_text().splitlines()) == 1
                consumed = Path(path).read_bytes()
            else:
                assert stopped.value.code == 1 and result['status'] == 'blocked'
                assert result['error_kind'] == 'chat_launch_observation_invalid'
                assert result['attempt_status'] == 'not_run'
                assert public['error_kind'] == out['error_kind'] == result['error_kind']
                assert Path(path).read_bytes() == (consumed if change == 'replay' else pending)
                assert (tmp_path/'body-argv.txt').exists() is (change == 'replay')
                if change == 'replay':
                    assert len((tmp_path/'body-argv.txt').read_text().splitlines()) == 1
        assert len(_PROCESSES) == (3 if change == 'replay' else 2 if change == 'supported' else 1)
    finally:
        _rundir.release_owner(owner)

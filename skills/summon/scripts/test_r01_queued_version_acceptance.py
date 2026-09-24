"""Queued R01 version drift at the real executor policy fence.

Synthetic authenticated source fixture and owned marked Windows CLI only.
The existing source fixture supplies historical model/session fields separately
from its actual executor observation; this does not qualify an installed CLI.
Workspace git observation is replaced with None; launch observation, version
query, authentication, current comparison, durable CAS and emission are real.
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
            if not _inside(script, _WRITABLE) or script.name != 'fake-claude.cmd':
                _denied()
            if not _inside(Path(kwargs.get('cwd', '')).resolve(), _WRITABLE):
                _denied()
            env = kwargs.get('env')
            if not isinstance(env, dict) or not _inside(Path(env.get('USERPROFILE', '')).resolve(), _WRITABLE):
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

@pytest.mark.parametrize('change', ['supported', 'registry', 'adapter', 'external_version'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_queued_version_revalidated_by_actual_executor_before_claim(tmp_path, monkeypatch, capsys, change):
    import _receipt
    # Workspace/git effects are out of scope; no observation/qualification seam is stubbed.
    monkeypatch.setattr(_receipt, 'workspace_snapshot', lambda *args, **kwargs: None)
    import _resume_capabilities as capabilities
    import _job_resume as resume
    import _executor
    import run_subagent
    import test_r01_r02_launch_chain_acceptance as chain

    def dynamic_cli(root):
        script = root / 'fake-claude.cmd'
        (root / 'version.txt').write_text('claude 9.9.9\n', encoding='utf-8')
        script.write_text(
            '@echo off\nREM SUMMON_CLAUDE_ENTRYPOINT_V1\n'
            'if "%1"=="--version" (type "%~dp0version.txt"&exit /b 0)\n'
            'echo invoked>> "%~dp0invocations.txt"\n'
            'echo {"type":"result","result":"STATUS: COMPLETE VERDICT: PASS HANDOFF: done","session_id":"fixture"}\n',
            encoding='utf-8')
        return str(_CMD), script
    monkeypatch.setattr(chain, '_fake_claude', dynamic_cli)
    root, source_id, reservation, context, child, script = chain._prepare_child(tmp_path, gate=False)
    executable_bytes = _CMD.read_bytes()
    script_bytes = script.read_bytes()
    before = resume.get_claim(reservation)
    ledger = Path(resume.ledger_path(root, source_id))
    before_ledger = ledger.read_bytes()
    assert before['provider_phase'] == 'pending'
    marker = tmp_path / 'invocations.txt'
    assert len(marker.read_text().splitlines()) == 1
    # These changes occur strictly after reservation and child-context authentication.
    if change == 'registry':
        monkeypatch.setattr(capabilities, 'REGISTRY_GENERATION', capabilities.REGISTRY_GENERATION + 1)
    elif change == 'adapter':
        monkeypatch.setattr(capabilities, 'V2_ADAPTER_VERSION_SCOPE', 'summon-executor/999.0.0')
    elif change == 'external_version':
        (tmp_path / 'version.txt').write_text('claude 9.9.10\n', encoding='utf-8')
    if change in {'registry', 'adapter'}:
        # Keep the synthetic current registry internally coherent, using its real serializer.
        monkeypatch.setattr(capabilities, 'REGISTRY_DIGEST', 'sha256:' + hashlib.sha256(
            capabilities._canonical_bytes({'generation': capabilities.REGISTRY_GENERATION,
                'rows': capabilities._v2_registry_material()})).hexdigest())
    result = _executor.execute_agent(child, timeout_ms=5000,
        launch_control=resume.provider_launch_control(context))
    assert _CMD.read_bytes() == executable_bytes and script.read_bytes() == script_bytes
    after = resume.get_claim(reservation)
    if change == 'supported':
        assert result['status'] == 'success'
        assert result['provider_contacted'] is True
        assert after['provider_phase'] in {'spawned', 'reaped'}
        assert len(marker.read_text().splitlines()) == 2
    else:
        assert result['status'] == 'blocked', result.get('error_kind')
        assert result['error_kind'] == 'resume_launch_qualification_invalid'
        assert result['attempt_status'] == 'not_run'
        assert result['provider_contacted'] is False
        assert after == before, 'pre-CAS refusal changed authenticated claim state'
        assert ledger.read_bytes() == before_ledger
        assert after['provider_phase'] == 'pending'
        assert after['provider_contacted'] is before['provider_contacted'] is None
        assert len(marker.read_text().splitlines()) == 1
    # Actual public --out projection, sourced from the real executor envelope.
    out = tmp_path / 'terminal.json'
    run_subagent._write_out(str(out), result)
    public = json.loads(out.read_text(encoding='utf-8'))
    assert public['status'] == result['status']
    assert public['provider_contacted'] is result['provider_contacted']
    assert '_private_launch_observation' not in public
    assert 'this_invocation_before_dispatch' not in json.dumps(public)
    if change != 'supported':
        assert public['error_kind'] == result['error_kind']
        assert public['attempt_status'] == 'not_run'
    # Actual stdout emitter too; no dispatcher child/public main invocation is claimed.
    monkeypatch.setattr(run_subagent, '_JOB_FILE', None)
    run_subagent._emit(result, trusted_executor_result=True)
    captured = capsys.readouterr()
    assert not captured.err
    stdout = json.loads(captured.out)
    assert stdout['status'] == public['status']
    assert stdout['provider_contacted'] is public['provider_contacted']
    assert '_private_launch_observation' not in stdout
    assert 'this_invocation_before_dispatch' not in captured.out
    if change != 'supported':
        assert stdout['error_kind'] == public['error_kind']
        assert stdout['attempt_status'] == 'not_run'
    # One real version query precedes each attempt; refused attempts never spawn the CLI body.
    assert len(_PROCESSES) == (4 if change == 'supported' else 3)

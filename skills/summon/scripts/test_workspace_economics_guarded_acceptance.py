"""Run two unchanged demo acceptance journeys with preconsumer fences.

Only the exact frozen owned Python transport source can spawn, using isolated
Python and the synthetic worker/consumer mode. Parent Python audit guards do not
constitute an OS sandbox for the trusted fixed child. Child cwd/home/env are
synthetic; no protocol, capability, receipt, economics or state decision is mocked.
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
_CHILD_SOURCE_SHA = None
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
        if not Path(os.fsdecode(args[0])).is_absolute():
            # POSIX descriptor-relative opens (openat with dir_fd, e.g. shutil.rmtree's
            # safe walk) are audited without their dir_fd; the anchoring directory was
            # itself opened and checked by absolute path.
            if '..' in Path(os.fsdecode(args[0])).parts:
                _denied()
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
        anchored = any(isinstance(item, int) and item >= 0 for item in args[len(paths):])
        for raw in paths:
            candidate = Path(os.fsdecode(raw))
            if candidate.is_absolute():
                if not _inside(candidate.resolve(), _WRITABLE):
                    _denied()
            elif not anchored or '..' in candidate.parts:
                # POSIX shutil.rmtree/openat work relative to a checked dir_fd;
                # an unanchored relative path is refused.
                _denied()


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def inert(tmp_path, monkeypatch):
    global _ACTIVE, _WRITABLE, _CMD, _CHILD_SOURCE_SHA
    system = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in system.items():
        monkeypatch.setenv(key, value)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
        monkeypatch.setenv(key, str(tmp_path))
    _CMD = Path(sys.executable).resolve()
    _CHILD_SOURCE_SHA = hashlib.sha256((HERE/'_workspace_transport.py').read_bytes()).hexdigest()
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
            if not isinstance(args, (list, tuple)) or len(args) != 5:
                _denied()
            if (Path(args[0]).resolve() != _CMD or list(args[1:3]) != ['-I', '-B']
                    or Path(args[3]).resolve() != (HERE/'_workspace_transport.py').resolve()):
                _denied()
            if args[4] not in {'--synthetic-worker', '--synthetic-supervisor-consumer'}:
                _denied()
            if hashlib.sha256((HERE/'_workspace_transport.py').read_bytes()).hexdigest() != _CHILD_SOURCE_SHA:
                _denied()
            if kwargs.get('shell') or kwargs.get('close_fds') is not True:
                _denied()
            if os.name == 'nt' and not kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW:
                _denied()
            if kwargs.get('stdin') != subprocess.PIPE or kwargs.get('stdout') != subprocess.PIPE or kwargs.get('stderr') != subprocess.DEVNULL:
                _denied()
            environment = kwargs.get('env')
            if not isinstance(environment, dict) or set(environment) - {'SystemRoot', 'WINDIR', 'SYSTEMROOT', 'TEMP', 'TMP'}:
                _denied()
            if any(not _inside(Path(environment.get(key, '')).resolve(), _WRITABLE) for key in ('TEMP', 'TMP')):
                _denied()
            # Environmental isolation only; the actual worker/consumer argv and protocol stay intact.
            kwargs['cwd'] = str(_WRITABLE)
            kwargs['env'] = dict(environment, HOME=str(_WRITABLE), USERPROFILE=str(_WRITABLE),
                                 APPDATA=str(_WRITABLE), LOCALAPPDATA=str(_WRITABLE), PATH='',
                                 SUMMON_TELEMETRY='0', PYTHONDONTWRITEBYTECODE='1')
            _ALLOW_SPAWN = True
            try:
                super().__init__(args, *positional, **kwargs)
            finally:
                _ALLOW_SPAWN = False
            _PROCESSES.append(self)
    monkeypatch.setattr(subprocess, 'Popen', OwnedPopen)
    monkeypatch.setattr(socket.socket, 'connect', _denied)
    real_kill = os.kill

    def owned_kill(pid, sig):
        # POSIX Popen.kill()/terminate() signal through os.kill; only the owned
        # synthetic children may be signalled.
        if pid not in {process.pid for process in _PROCESSES}:
            _denied()
        return real_kill(pid, sig)
    monkeypatch.setattr(os, 'kill', owned_kill)
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








def test_snapshot_accepts_concurrent_append_after_caller_sampled_now(tmp_path):
    """Field race (2026-09-25, ~1 in 14 WSL runs): a separate worker process
    appends to the journal after the caller sampled now_ms; the snapshot must
    treat that record as current activity, not a forged future event."""
    from _workspace_demo import ConductorDemo
    demo = ConductorDemo(tmp_path / "workspaces")
    try:
        setup = demo.supervisor_message_setup()
        coordinator = demo.runtime._coordinator
        state, torn, raw_map = coordinator._load_with_snapshot()
        records, records_torn, records_raw = coordinator._read_records_snapshot()
        assert not torn and not records_torn and records_raw == raw_map
        _KINDS = {"workspace_message_admitted", "workspace_message_sent",
                  "workspace_operator_message_sent", "workspace_supervisor_message_sent"}

        def _rate_kind(record):
            nested = record.get("workspace_event")
            if not isinstance(nested, dict) or nested.get("event") not in _KINDS:
                return False
            payload = nested.get("payload")
            message = payload.get("message") if isinstance(payload, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            return (isinstance(record.get("observed_at_ms"), int)
                    and isinstance(content, dict)
                    and isinstance(content.get("utf8_bytes"), int))

        stamped = [r for r in records if _rate_kind(r)]
        assert stamped
        caller_now = max(r["observed_at_ms"] for r in stamped)
        concurrent = copy.deepcopy(max(stamped, key=lambda r: r["observed_at_ms"]))
        concurrent["observed_at_ms"] = caller_now + 5_000
        records.append(concurrent)
        snapshot = coordinator.workspace_budget_snapshot(
            state=state, raw_map=raw_map, records=records, now_ms=caller_now)
        # Consensus design (KM-01/RX-01): a concurrent append stamped after the
        # caller's clock is clamped into the snapshot bound and counted at age
        # zero -- accepted, rate-limiting applies maximally, and the window's
        # trailing edge never moves. The snapshot bound stays at the caller's
        # clock; the low-level future gate survives for detached snapshots.
        assert snapshot["now_ms"] == caller_now
        concurrent_events = [item for item in snapshot["rate_events"]
                             if item["at_ms"] >= caller_now]
        assert concurrent_events, "the clamped concurrent append vanished"
        assert all(item["at_ms"] <= caller_now for item in concurrent_events)
        # R2-RR-04: the clamped event must carry its real count payload -- a
        # regression that zeroed messages/bytes would slip a presence-only pin.
        source_bytes = concurrent["workspace_event"]["payload"]["message"]["content"]["utf8_bytes"]
        assert concurrent_events[0]["messages"] == 1
        assert concurrent_events[0]["bytes"] == source_bytes

        # The low-level future gate still refuses detached fabricated events.
        import _workspace_budget as budget
        with pytest.raises(budget.WorkspaceBudgetError, match="from the future"):
            budget._rate_event({"at_ms": caller_now + 60_000, "messages": 1,
                                "bytes": 1}, now_ms=caller_now)
    finally:
        demo.cleanup()


@pytest.mark.parametrize('method', [
    'test_v2_economics_path_derives_policy_bound_reservation_and_settles',
    'test_v2_economics_covers_full_turn_settlement_reopen_and_indeterminate_hold',
], ids=['p001_case_001', 'p001_case_002'])
def test_actual_workspace_v2_economics_demo_under_preconsumer_guards(method):
    # Import the original retained suite only after the autouse isolation fixture.
    # Call its assertions unchanged; no capability, receipt or reducer substitution.
    import test_workspace_demo
    case = test_workspace_demo.ConductorDemoTests(method)
    getattr(case, method)()
    assert _PROCESSES, 'selected journey did not exercise the real owned child'
    assert all(process.poll() is not None for process in _PROCESSES)
    assert all(stream is None or stream.closed for process in _PROCESSES
               for stream in (process.stdin, process.stdout, process.stderr))
    assert len(_PROCESSES) == (1 if 'path_derives' in method else 2)

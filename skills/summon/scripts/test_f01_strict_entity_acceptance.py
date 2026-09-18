"""F01 strict Task, WorkerInstance and Command public-boundary acceptance.

All storage is beneath a pytest-owned temporary root. Source directories only
supply imports; no source ancestor grants write authority. No subprocess starts.
"""
from pathlib import Path
import builtins
import contextlib
import copy
import importlib.util
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
_READABLE = [HERE.resolve(), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
for _dependency in ('pytest', '_pytest', 'pluggy', 'packaging', 'iniconfig', 'colorama', 'pygments'):
    _spec = importlib.util.find_spec(_dependency)
    if _spec and _spec.origin:
        _READABLE.append(Path(_spec.origin).resolve().parent)
_DENIED_IMPORTS = ('_auth', '_credentials', '_windows_credentials', '_nous_credentials',
                   '_telemetry', '_executor', '_apibackend', 'keyring', 'win32cred')


def _inside(path, root):
    return root is not None and (path == root or root in path.parents)


def _denied(*args, **kwargs):
    _HITS.append('forbidden_boundary')
    raise AssertionError('synthetic strict-entity boundary refused')


def _audit(event, args):
    if not _ACTIVE:
        return
    if event.startswith(('subprocess.', 'socket.', 'os.spawn', 'os.exec')) or event in {'os.system', 'os.startfile'}:
        _denied()
    if event == 'import' and args and str(args[0]).startswith(_DENIED_IMPORTS):
        _denied()
    if event == 'open' and args and not isinstance(args[0], int):
        path = Path(os.fsdecode(args[0])).resolve()
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        writing = any(char in (args[1] or '') for char in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        if writing and not _inside(path, _WRITABLE):
            _denied()
        if not writing and not (_inside(path, _WRITABLE) or any(_inside(path, root) for root in _READABLE)):
            _denied()
    if event in {'os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'}:
        paths = args[:2] if event == 'os.rename' else args[:1]
        if any(not _inside(Path(os.fsdecode(path)).resolve(), _WRITABLE) for path in paths):
            _denied()


sys.addaudithook(_audit)


@pytest.fixture(autouse=True)
def inert(tmp_path, monkeypatch):
    global _ACTIVE, _WRITABLE
    system = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in system.items():
        monkeypatch.setenv(key, value)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP'):
        monkeypatch.setenv(key, str(tmp_path))
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
    monkeypatch.setattr(subprocess, 'Popen', _denied)
    monkeypatch.setattr(socket.socket, 'connect', _denied)
    _HITS.clear(); _WRITABLE = tmp_path.resolve(); _ACTIVE = True
    try:
        yield
        assert not _HITS, 'a caught exception concealed a forbidden boundary'
    finally:
        _ACTIVE = False; _WRITABLE = None


def _journals(root):
    result = {path.relative_to(root): path.read_bytes() for path in Path(root).rglob('journal-g*.jsonl')}
    assert result, 'journal preservation must compare an actual durable journal'
    return result


@pytest.mark.parametrize('reader', ['parse', 'validate'], ids=['p001_case_001', 'p001_case_002'])
@pytest.mark.parametrize('mutation', ['missing_schema', 'future_schema', 'duplicate_task_id'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_plan_readers_refuse_unsupported_schema_and_duplicate_identity(reader, mutation):
    import _workspace_plan as plan
    from test_workspace_plan import _plan
    value = _plan()
    if mutation == 'missing_schema': value.pop('schema')
    elif mutation == 'future_schema': value['schema'] = 'summon.workspace.plan/v999'
    else: value['tasks'].append(copy.deepcopy(value['tasks'][0]))
    before = copy.deepcopy(value)
    raw = json.dumps(value).encode()
    with pytest.raises(plan.WorkspacePlanError):
        plan.parse_plan_bytes(raw) if reader == 'parse' else plan.validate_plan(value)
    assert value == before and raw == json.dumps(value).encode()


def test_supported_plan_reads_and_compiles_the_same_task_identity():
    import _workspace_plan as plan
    import _workspace_protocol as protocol
    from test_workspace_plan import _plan
    value = _plan(); before = copy.deepcopy(value)
    raw = json.dumps(value).encode()
    parsed = plan.parse_plan_bytes(raw)
    validated = plan.validate_plan(parsed)
    compiled = plan.compile_plan(validated, raw_sha256=__import__('hashlib').sha256(raw).hexdigest(),
                                 workspace_id='workspace', run_id='run-a', now_ms=1000000)
    protocol.validate_goal_plan(compiled['goal'], compiled['lanes'])
    assert [lane['task_id'] for lane in compiled['lanes']] == [task['task_id'] for task in value['tasks']]
    parsed['tasks'][0]['outcome'] = 'Changed detached copy'
    assert value == before


def _coordinator(tmp_path):
    from _swarm_coordinator import SwarmCoordinator
    return SwarmCoordinator.create(str(tmp_path / 'runs'), 'run-1', project_root_sha256='a'*64,
                                   roster_definition_sha256='b'*64,
                                   tasks=[{'task_id':'task-1','request_sha256':'c'*64}],
                                   max_attempts=2, clock=lambda: 2000000000.0)


def _reopen(coordinator):
    from _swarm_coordinator import SwarmCoordinator
    return SwarmCoordinator(str(Path(coordinator.run_dir).parent), 'run-1', clock=lambda: 2000000000.0)


@pytest.mark.parametrize('mutation', ['empty_instance', 'invalid_capability', 'invalid_permission'], ids=['p003_case_001', 'p003_case_002', 'p003_case_003'])
def test_public_registration_refuses_malformed_fields_without_mutation(tmp_path, mutation):
    from _swarm_coordinator import SwarmCoordinatorError
    from _swarm_protocol import SwarmProtocolError
    coordinator = _coordinator(tmp_path)
    options = dict(worker_instance_id='instance-1', capabilities=['message'], permission_ceiling='read-only')
    if mutation == 'empty_instance': options['worker_instance_id'] = ''
    elif mutation == 'invalid_capability': options['capabilities'] = ['invalid\ncapability']
    else: options['permission_ceiling'] = 'administrator'
    before = copy.deepcopy(coordinator._load()[0]); journals = _journals(coordinator.run_dir)
    with pytest.raises((SwarmCoordinatorError, SwarmProtocolError)):
        coordinator.register_worker('worker-1', **options)
    assert coordinator._load()[0] == before
    assert _journals(coordinator.run_dir) == journals


@pytest.mark.parametrize('mutation', ['missing_instance', 'unknown_field', 'future_protocol'], ids=['p004_case_001', 'p004_case_002', 'p004_case_003'])
def test_public_registration_frame_reader_refuses_unknown_shape(tmp_path, mutation):
    from _swarm_protocol import make_frame
    from _swarm_coordinator import SwarmCoordinatorError
    coordinator = _coordinator(tmp_path)
    frame = make_frame('register_worker', run_id='run-1', message_id='register-fixture', sent_at_ms=1,
                       payload={'worker_id':'worker-1','worker_instance_id':'instance-1','project_root_sha256':'a'*64,
                                'roster_definition_sha256':'b'*64,'capabilities':['message'],'permission_ceiling':'read-only'})
    if mutation == 'missing_instance': frame['payload'].pop('worker_instance_id')
    elif mutation == 'unknown_field': frame['payload']['approved'] = True
    else: frame['protocol'] = 'summon.swarm/v999'
    before = copy.deepcopy(coordinator._load()[0]); journals = _journals(coordinator.run_dir)
    with pytest.raises(SwarmCoordinatorError, match="invalid swarm frame"):
        coordinator.apply_frame(frame, worker_id='worker-1')
    assert coordinator._load()[0] == before
    assert _journals(coordinator.run_dir) == journals


@pytest.mark.parametrize('reopen', [False, True], ids=['current', 'reopened'])
def test_worker_identity_refuses_a_different_instance_across_reopen(tmp_path, reopen):
    from _swarm_coordinator import SwarmConflictError
    coordinator = _coordinator(tmp_path)
    assert coordinator.register_worker('worker-1', worker_instance_id='instance-1')['status'] == 'registered'
    if reopen: coordinator = _reopen(coordinator)
    before = copy.deepcopy(coordinator._load()[0]); journals = _journals(coordinator.run_dir)
    assert before['workers']['worker-1']['worker_instance_id'] == 'instance-1'
    with pytest.raises(SwarmConflictError, match='already bound to another instance'):
        coordinator.register_worker('worker-1', worker_instance_id='instance-2')
    assert coordinator._load()[0] == before
    assert _journals(coordinator.run_dir) == journals
    assert _reopen(coordinator)._load()[0]['workers']['worker-1']['worker_instance_id'] == 'instance-1'


@contextlib.contextmanager
def _command_fixture():
    from test_workspace_runtime import OperatorCommandRuntimeTests
    fixture = OperatorCommandRuntimeTests()
    try:
        fixture.setUp()
        yield fixture
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize('schema', ['missing', 'future'], ids=['p006_case_001', 'p006_case_002'])
def test_public_operator_command_refuses_unsupported_source_schema_without_mutation(schema):
    with _command_fixture() as fixture:
        binding = fixture.binding()
        source = json.loads(binding.source_bytes)
        if schema == 'missing': source.pop('schema')
        else: source['schema'] = 'summon.workspace.operator-command-request/v999'
        raw = json.dumps(source, sort_keys=True, separators=(',', ':')).encode()
        before = copy.deepcopy(fixture.demo.state()); journals = _journals(fixture.demo.root)
        with pytest.raises(ValueError):
            fixture.demo.runtime.execute_operator_command(fixture.handle, raw)
        assert fixture.demo.state() == before
        assert _journals(fixture.demo.root) == journals


def test_supported_operator_command_producer_is_read_durably_without_task_authority():
    with _command_fixture() as fixture:
        binding = fixture.binding()
        assert json.loads(binding.source_bytes)['schema'] == 'summon.workspace.operator-command-request/v1'
        before = fixture.demo.state()
        result = fixture.demo.runtime.execute_operator_command(fixture.handle, binding.source_bytes)
        assert result['status'] == 'recorded' and result['durable_prefix_verified'] is True
        assert result['request_sha256'] == binding.request_sha256
        after = fixture.demo.state(); journals = _journals(fixture.demo.root)
        for key in ('tasks', 'claims', 'workers'):
            assert before[key] == after[key]
        assert fixture.demo.runtime.reconcile_operator_command(fixture.handle, binding.source_bytes) == result
        assert fixture.demo.state() == after and _journals(fixture.demo.root) == journals



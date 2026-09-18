"""L02 exact numeric versions at actual conversation open/recovery boundaries.

Synthetic journal/process records only. Recovery uses substituted liveness and
birth-token observations; no OS process probe or cancellation is qualified.
Guards activate before consumer imports, and writes remain pytest-owned.
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
                   '_arkcli_creds', '_resolver', 'keyring', 'win32cred')


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
    monkeypatch.setattr(os, 'kill', _denied)
    _HITS.clear(); _WRITABLE = tmp_path.resolve(); _ACTIVE = True
    try:
        yield
        assert not _HITS, 'a caught exception concealed a forbidden boundary'
    finally:
        _ACTIVE = False; _WRITABLE = None





VERSIONS = ['supported', 'missing', 'future', 'bool', 'float']
PROCESS_VERSIONS = ['legacy', *VERSIONS]


def _set_version(value, case):
    if case == 'missing':
        value.pop('schema_version')
    else:
        value['schema_version'] = {'supported': 2, 'legacy': 1, 'future': 99, 'bool': True, 'float': 2.0, 'mixed': 1}[case]


def _room(tmp_path):
    from _conversation import ConversationJournal
    project = tmp_path / 'project'; project.mkdir()
    roster = tmp_path / 'agents'; roster.mkdir()
    root = tmp_path / 'rooms'
    room = ConversationJournal.create(root, session_id='room-version', project_id='synthetic',
        project_root=project, initiator_host='fixture', initiator_agent='operator', mode='chat',
        participants=[{'agent':'worker','role':'researcher','name':'Worker','version':'synthetic'}])
    return root, project, roster, room


def _tree(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('record_kind', ['room', 'event'], ids=['p001_case_001', 'p001_case_002'])
@pytest.mark.parametrize('version', [*VERSIONS, 'mixed'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006'])
def test_actual_journal_open_refuses_noninteger_or_unsupported_versions_without_mutation(tmp_path, record_kind, version):
    from _conversation import ConversationJournal, ConversationError
    root, project, roster, room = _room(tmp_path)
    path = root / 'room-version.jsonl'
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    _set_version(records[0 if record_kind == 'room' else 1], version)
    path.write_text(''.join(json.dumps(record)+'\n' for record in records), encoding='utf-8')
    before = _tree(root)
    if version == 'supported':
        reopened = ConversationJournal.open(root, 'room-version')
        assert reopened.room.session_id == 'room-version'
        assert reopened.as_dict(native=True)['room']['session_id'] == 'room-version'
    else:
        try:
            with pytest.raises(ConversationError):
                ConversationJournal.open(root, 'room-version')
        finally:
            assert _tree(root) == before
    assert _tree(root) == before


def _recovery(tmp_path, monkeypatch, version, *, live=True):
    import _conversation_runtime as runtime
    root, project, roster, journal = _room(tmp_path)
    # Constructor sees only an owned inert file; it is never executed.
    dispatcher = tmp_path / 'dispatcher.py'; dispatcher.write_text('raise AssertionError("must not execute")\n')
    instance = runtime.ConversationRuntime(root, cwd=project, agents_dir=roster,
        dispatcher=dispatcher, strict_agents_dir=True)
    journal.append('turn_started', 'system', 'summon', {
        'turn_id':'turn-pending','participant':'worker','prompt_sha256':'a'*64,
        'prompt_chars':4,'provider':'synthetic','model_target':'synthetic-model',
        'permission':'read-only','transport':'subprocess','resumed':False})
    directory = Path(instance._runtime_owner_dir('room-version', 'worker'))
    path = directory / 'turn-turn-pending.json'
    record = {'schema_version':1,'turn_id':'turn-pending','session_id':'room-version',
        'participant':'worker','owner_nonce':'synthetic-owner','generation':1,
        'state':'running','pid':424242,'pid_start_token':'synthetic-birth','created_at':1.0}
    _set_version(record, version)
    runtime._write_process_record(str(path), record)
    probes = []
    def liveness(pid):
        assert pid == 424242
        probes.append('synthetic_liveness')
        return live
    def birth(pid):
        assert pid == 424242
        probes.append('synthetic_birth')
        return 'synthetic-birth'
    monkeypatch.setattr(runtime, '_pid_alive', liveness)
    monkeypatch.setattr(runtime, '_process_birth_token', birth)
    monkeypatch.setattr(runtime, '_kill_pid_tree', _denied)
    return runtime, instance, root, path, probes


@pytest.mark.parametrize('version', PROCESS_VERSIONS, ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005', 'p003_case_006'])
def test_actual_recovery_refuses_unsupported_process_version_before_probe_or_mutation(tmp_path, monkeypatch, version):
    runtime, instance, root, path, probes = _recovery(tmp_path, monkeypatch, version)
    before = _tree(root)
    try:
        refused = None
        try:
            result = instance.recover_turn('room-version', 'worker', confirm=True)
        except runtime.ConversationRuntimeError as error:
            refused = str(error)
        assert _tree(root) == before
        assert refused is not None, 'unsupported or synthetically live process must refuse recovery'
        if version in {'supported', 'legacy'}:
            assert 'still live' in refused
            assert probes == ['synthetic_liveness', 'synthetic_birth']
        else:
            assert probes == [], 'unsupported process schema reached process-identity decision probes'
        assert _tree(root) == before
    finally:
        instance.close()


@pytest.mark.parametrize('version', ['legacy', 'supported'], ids=['p004_case_001', 'p004_case_002'])
def test_supported_process_version_allows_explicit_dead_fixture_recovery_with_unknown_effects(tmp_path, monkeypatch, version):
    from _conversation import ConversationJournal
    runtime, instance, root, path, probes = _recovery(tmp_path, monkeypatch, version, live=False)
    record_before = path.read_bytes()
    try:
        result = instance.recover_turn('room-version', 'worker', confirm=True)
        assert result['status'] == 'recovered'
        assert result['recovery_kind'] == 'human_attested_indeterminate'
        assert probes == ['synthetic_liveness']
        assert path.read_bytes() == record_before
        events = ConversationJournal.open(root, 'room-version').events(native=True)
        finished = [e for e in events if e['event'] == 'turn_finished']
        assert len(finished) == 1
        payload = finished[0]['payload']
        assert payload['status'] == 'blocked' and payload['human_confirmed'] is True
        assert 'spend is unknown' in payload['summary']
        assert 'provider_contacted' not in payload
    finally:
        instance.close()


@pytest.mark.parametrize('version', PROCESS_VERSIONS, ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004', 'p005_case_005', 'p005_case_006'])
def test_actual_cancel_checks_process_schema_before_any_durable_mutation(tmp_path, monkeypatch, version):
    from _conversation import ConversationJournal
    runtime, instance, root, path, probes = _recovery(tmp_path, monkeypatch, version, live=False)
    before = _tree(root)
    try:
        if version in {'supported', 'legacy'}:
            result = instance.cancel_turn('room-version', 'worker')
            assert result['status'] == 'cancelling' and result['durable'] is True
            assert result['local_worker'] is False
            assert probes == ['synthetic_liveness']
            assert path.read_bytes() == before[path.relative_to(root).as_posix()]
            events = ConversationJournal.open(root, 'room-version').events(native=True)
            assert sum(e['event'] == 'turn_cancel_requested' for e in events) == 1
            assert not any(e['event'] == 'turn_finished' for e in events)
        else:
            try:
                with pytest.raises(runtime.ConversationRuntimeError):
                    instance.cancel_turn('room-version', 'worker')
            finally:
                assert probes == [], 'unsupported process metadata reached identity probes'
                assert _tree(root) == before, 'unsupported process schema mutated cancellation history'
    finally:
        instance.close()


def _historical_room(tmp_path, monkeypatch):
    runtime, instance, root, path, probes = _recovery(tmp_path, monkeypatch, 'legacy')
    journal_path = root / 'room-version.jsonl'
    records = [json.loads(line) for line in journal_path.read_text(encoding='utf-8').splitlines()]
    for record in records:
        record['schema_version'] = 1
    journal_path.write_text(''.join(json.dumps(record)+'\n' for record in records), encoding='utf-8')
    return runtime, instance, root, path, probes


def test_consistent_historical_v1_open_list_and_mutation_refusals_preserve_all_bytes(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from _conversation import ConversationJournal, ConversationError, list_rooms, run_command
    runtime, instance, root, path, probes = _historical_room(tmp_path, monkeypatch)
    before = _tree(root)
    try:
        room = ConversationJournal.open(root, 'room-version')
        assert room.schema_version == 1 and room.room.session_id == 'room-version'
        assert 'room-version' in json.dumps(list_rooms(root))
        assert run_command(SimpleNamespace(chat_action='open',chat_session='room-version',
            conversation_dir=str(root),cwd=instance.cwd,chat_browser=None)) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'opened'
        with pytest.raises(ConversationError):
            room.append_human_message('must refuse historical mutation')
        for operation in (
            lambda: instance.start_turn('room-version','worker','must not start'),
            lambda: instance.cancel_turn('room-version','worker'),
            lambda: instance.recover_turn('room-version','worker',confirm=True)):
            with pytest.raises(runtime.ConversationRuntimeError):
                operation()
            assert _tree(root) == before
        assert probes == []
    finally:
        instance.close()


def test_explicit_historical_v1_fork_creates_linked_v2_without_parent_mutation_or_contact(tmp_path, monkeypatch):
    from _conversation import ConversationJournal
    runtime, instance, root, path, probes = _historical_room(tmp_path, monkeypatch)
    before = _tree(root)
    prompt = 'Preserve this exact synthetic prompt.\nSecond line.'
    try:
        result = instance.fork_turn('room-version','worker',prompt,reason='explicit compatibility fork')
        assert result['status'] == 'forked' and result['prompt_preserved'] is True
        assert result['parent_session_id'] == 'room-version'
        child_id = result['session_id']
        assert child_id != 'room-version'
        child = ConversationJournal.open(root,child_id)
        assert child.schema_version == 2
        events = child.events(native=True)
        assert any(event['payload'].get('parent_session_id') == 'room-version'
                   and event['payload'].get('child_session_id') == child_id for event in events), 'child lacks durable parent lineage'
        assert any(event['event'] == 'human_message' and event['payload'].get('text') == prompt for event in events)
        assert not any(event['event'] in {'turn_started','turn_finished'} for event in events)
        assert set(_tree(root)) - set(before) == {child_id+'.jsonl'}
        assert probes == []
    finally:
        # The old journal and every old runtime record must remain byte-identical,
        # including when the desired fork unexpectedly refuses.
        after = _tree(root)
        assert all(after.get(name) == content for name,content in before.items())
        instance.close()

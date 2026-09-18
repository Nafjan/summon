"""Synthetic unqualified historical task -> refusal -> explicit fork journey."""
from pathlib import Path
import builtins
import contextlib
import copy
import hashlib
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
    _HITS.clear(); _WRITABLE = tmp_path.resolve(); _ACTIVE = True
    try:
        yield
        assert not _HITS, 'a caught exception concealed a forbidden boundary'
    finally:
        _ACTIVE = False; _WRITABLE = None




@pytest.mark.parametrize('change', ['unchanged_unqualified', 'rename', 'remove', 'model_change'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_real_turn_refusal_then_explicit_nonlaunching_fork_retains_history(tmp_path,change):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntime, ConversationRuntimeError
    project=tmp_path/'project';project.mkdir()
    roster=tmp_path/'agents';roster.mkdir()
    definition=roster/'worker.md'
    original='---\nrun-agent: claude\nmodel: synthetic-model\npermission: read-only\n---\nSynthetic historical participant.\n'.replace('\\n','\n')
    definition.write_text(original)
    root=tmp_path/'rooms'
    journal=ConversationJournal.create(root,session_id='room-1',project_id='synthetic',
        project_root=project,initiator_host='codex',initiator_agent='human',mode='chat',
        participants=[{'agent':'worker','role':'researcher','name':'Worker','version':'1'}])
    runtime=ConversationRuntime(root,cwd=project,agents_dir=roster,strict_agents_dir=True)
    try:
        identity,execution=runtime._identity(journal,'worker')
        # Actual local source-family producer binds the old roster. No launch
        # observation or native/provider qualification is invented or installed.
        family=runtime._ensure_chat_family(journal,'worker',identity,execution)
        assert family['source_family']['qualifications'] == []
        journal.append_human_message('Synthetic old question',message_id='old-question')
        journal.append('turn_started','system','summon',{
            'turn_id':'old-turn','participant':'worker','status':'started',
            'identity':identity},event_id='old-start')
        journal.append_agent_message('worker','human','Synthetic historical text',message_id='old-answer')
        journal.append('turn_finished','system','summon',{
            'turn_id':'old-turn','participant':'worker','status':'success',
            'identity':identity,'resume_session_id':'synthetic-unqualified-handle'},event_id='old-finish')
        before=(root/'room-1.jsonl').read_bytes()
        records=journal.events(native=True)
        def files():return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        if change=='rename':definition.rename(roster/'renamed-worker.md')
        elif change=='remove':definition.unlink()
        elif change=='model_change':definition.write_text(original.replace('synthetic-model','synthetic-new-model'))
        stored=files()
        if change in {'rename','remove'}:
            with pytest.raises(ConversationRuntimeError,match='not in the selected roster'):
                runtime.start_turn('room-1','worker','New contact must be refused',wait=True)
        else:
            result=runtime.start_turn('room-1','worker','New contact must be refused',wait=True)
            assert result['status']=='blocked'
            assert result['refusal']['provider_contacted'] is False
            assert result['refusal']['reason_code'] == ('resume_launch_qualification_missing'
                if change=='unchanged_unqualified' else 'continuation_identity_incompatible')
        assert files()==stored
        assert (root/'room-1.jsonl').read_bytes()==before
        assert runtime._active == {}
        fork=runtime.fork_turn('room-1','worker','Explicit fresh-context request',reason='operator roster recovery')
        assert fork['status']=='forked' and fork['prompt_preserved'] is True
        assert fork['session_id']!='room-1'
        assert (root/'room-1.jsonl').read_bytes().startswith(before)
        reopened=ConversationJournal.open(root,'room-1')
        assert reopened.events(native=True)[:-1]==records
        child=ConversationJournal.open(root,fork['session_id'])
        events=child.events(native=True)
        assert not any(e['event'] in {'turn_started','turn_finished'} for e in events)
        assert any(e['event']=='human_message' and e['payload']['text']=='Explicit fresh-context request' for e in events)
        assert runtime._active == {}
        if change=='model_change':
            replacement,_=runtime._identity(child,'worker')
            assert replacement['model_target']=='synthetic-new-model'
            assert replacement!=identity
        elif change in {'rename','remove'}:
            # Fork preserves membership rather than silently rebinding a name.
            with pytest.raises(ConversationRuntimeError,match='not in the selected roster'):
                runtime._identity(child,'worker')
    finally:
        runtime.close(timeout=1)


def _replacement_room(tmp_path, *, history=True, ceiling='yolo', participants=None):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntime
    project = tmp_path / 'project'
    project.mkdir()
    roster = tmp_path / 'agents'
    roster.mkdir()
    for name in ('worker', 'renamed', 'observer', 'new-observer', 'third'):
        _definition(roster, name)
    root = tmp_path / 'rooms'
    journal = ConversationJournal.create(
        root, session_id='room-1', project_id='synthetic', project_root=project,
        initiator_host='codex', initiator_agent='human', mode='chat',
        participants=participants or [{'agent': 'worker', 'role': 'researcher'}])
    runtime = ConversationRuntime(root, cwd=project, agents_dir=roster,
                                  strict_agents_dir=True, permission_ceiling=ceiling)
    if history:
        _record_permission(runtime, journal, 'worker')
    return runtime, journal, roster


def _definition(roster, name, permission='read-only', model='synthetic-model'):
    (roster / (name + '.md')).write_text(
        f'---\nrun-agent: claude\nmodel: {model}\npermission: {permission}\n---\nSynthetic fixture.\n')


def _record_permission(runtime, journal, participant, permission='read-only'):
    identity, _ = runtime._identity(journal, participant)
    identity['permission'] = permission
    for event, status in (('turn_started', 'started'), ('turn_finished', 'success')):
        journal.append(event, 'system', 'summon', {
            'turn_id': 'history-' + participant, 'participant': participant,
            'status': status, 'identity': identity}, event_id=event + '-' + participant)


def _stored(root):
    return {p.relative_to(root): p.read_bytes() for p in Path(root).rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['rename', 'remove', 'drift'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_replacement_uses_recorded_ceiling_without_original_catalog(tmp_path, change):
    from _conversation import ConversationJournal
    runtime, parent, roster = _replacement_room(tmp_path)
    try:
        if change == 'rename':
            (roster / 'worker.md').rename(roster / 'old-renamed.md')
        elif change == 'remove':
            (roster / 'worker.md').unlink()
        else:
            _definition(roster, 'worker', 'yolo', 'drifted-model')
        before = _stored(runtime.root)
        result = runtime.fork_turn('room-1', 'worker', ' Selected replacement prompt ',
                                   replacement_agent='renamed')
        assert result['parent_unchanged'] is True
        assert result['execution_authorized'] is False
        assert result['provider_contacted'] is False
        assert result['participant_replacement']['permission_ceiling'] == 'read-only'
        assert all((Path(runtime.root) / name).read_bytes() == content for name, content in before.items())
        child = ConversationJournal.open(runtime.root, result['session_id'])
        assert child._header['participants'] == [{'agent': 'renamed', 'role': 'researcher'}]
        records = child.events(native=True)
        lineage = next(e['payload'] for e in records if e['event'] == 'fork_created')
        assert lineage['parent_session_id'] == 'room-1'
        assert lineage['parent_history_sha256'] == hashlib.sha256(before[Path('room-1.jsonl')]).hexdigest()
        assert lineage['lineage'] == 'explicit-participant-replacement'
        assert [e['payload']['text'] for e in records if e['event'] == 'human_message'] == ['Selected replacement prompt']
        assert not any(e['event'] in {'turn_started', 'turn_finished'} for e in records)
        assert len(_stored(runtime.root)) == len(before) + 1
        assert runtime._active == {}
        identity, execution = runtime._identity(child, 'renamed')
        assert identity['permission'] == 'read-only'
        assert execution['permission_ceiling'] == 'read-only'
    finally:
        runtime.close(timeout=1)


@pytest.mark.parametrize('history', ['absent', 'missing', 'invalid', 'wrong_type'], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004'])
@pytest.mark.parametrize('catalog', ['unchanged', 'removed', 'widened'], ids=['p004_case_001', 'p004_case_002', 'p004_case_003'])
def test_unknown_history_never_falls_back_to_current_catalog(tmp_path, history, catalog):
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path, history=False)
    try:
        if history != 'absent':
            identity = {} if history == 'missing' else {'permission': 'unknown'}
            if history == 'wrong_type':
                identity = None
            journal.append('turn_started', 'system', 'summon', {
                'turn_id': 'unknown', 'participant': 'worker', 'status': 'started',
                'identity': identity}, event_id='unknown-history')
        if catalog == 'removed':
            (roster / 'worker.md').unlink()
        elif catalog == 'widened':
            _definition(roster, 'worker', 'yolo')
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError, match='historical permission unavailable'):
            runtime.fork_turn('room-1', 'worker', 'Refuse unknown authority', replacement_agent='renamed')
        assert _stored(runtime.root) == before
        assert runtime._active == {}
    finally:
        runtime.close(timeout=1)


@pytest.mark.parametrize('original_catalog', ['removed', 'widened'], ids=['p005_case_001', 'p005_case_002'])
def test_replacement_cannot_widen_recorded_authority(tmp_path, original_catalog):
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path)
    try:
        if original_catalog == 'removed':
            (roster / 'worker.md').unlink()
        else:
            _definition(roster, 'worker', 'yolo')
        _definition(roster, 'renamed', 'safe-edit')
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError, match='permission is incompatible'):
            runtime.fork_turn('room-1', 'worker', 'Refuse wider replacement', replacement_agent='renamed')
        assert _stored(runtime.root) == before
    finally:
        runtime.close(timeout=1)


def test_same_name_descendant_preserves_frozen_replacement_definition(tmp_path):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path)
    try:
        first = runtime.fork_turn('room-1', 'worker', 'First', replacement_agent='renamed')
        before = _stored(runtime.root)
        second = runtime.fork_turn(first['session_id'], 'renamed', 'Second')
        assert all((Path(runtime.root) / name).read_bytes() == content for name, content in before.items())
        child = ConversationJournal.open(runtime.root, second['session_id'])
        assert runtime._identity(child, 'renamed')[0]['permission'] == 'read-only'
        _definition(roster, 'renamed', model='changed-after-fork')
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError, match='replacement definition changed'):
            runtime.start_turn(second['session_id'], 'renamed', 'Must not launch', wait=True)
        assert _stored(runtime.root) == before
        assert runtime._active == {}
    finally:
        runtime.close(timeout=1)


def test_second_participant_replacement_carries_first_binding_with_one_lineage(tmp_path):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path, participants=[
        {'agent': 'worker'}, {'agent': 'observer'}])
    try:
        first = runtime.fork_turn('room-1', 'worker', 'First', replacement_agent='renamed')
        parent = ConversationJournal.open(runtime.root, first['session_id'])
        _record_permission(runtime, parent, 'observer')
        before = parent.path.read_bytes()
        second = runtime.fork_turn(first['session_id'], 'observer', 'Second', replacement_agent='new-observer')
        assert parent.path.read_bytes() == before
        child = ConversationJournal.open(runtime.root, second['session_id'])
        events = [e['payload'] for e in child.events(native=False) if e['event'] == 'fork_created']
        assert len(events) == 2
        assert {e['participant_replacement']['to_participant'] for e in events} == {'renamed', 'new-observer'}
        assert {e['parent_session_id'] for e in events} == {first['session_id']}
        assert {e['child_session_id'] for e in events} == {second['session_id']}
        assert {e['parent_history_sha256'] for e in events} == {hashlib.sha256(before).hexdigest()}
        for participant in ('renamed', 'new-observer'):
            assert runtime._identity(child, participant)[0]['permission'] == 'read-only'
        _definition(roster, 'renamed', model='drifted-first')
        with pytest.raises(ConversationRuntimeError, match='replacement definition changed'):
            runtime._identity(child, 'renamed')
        assert runtime._active == {}
    finally:
        runtime.close(timeout=1)


def test_chained_explicit_replacement_retains_ceiling_without_current_original(tmp_path):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path)
    try:
        first = runtime.fork_turn('room-1', 'worker', 'First', replacement_agent='renamed')
        (roster / 'renamed.md').unlink()
        _definition(roster, 'third', 'safe-edit')
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError, match='permission is incompatible'):
            runtime.fork_turn(first['session_id'], 'renamed', 'Refuse', replacement_agent='third')
        assert _stored(runtime.root) == before
        _definition(roster, 'third')
        second = runtime.fork_turn(first['session_id'], 'renamed', 'Third', replacement_agent='third')
        child = ConversationJournal.open(runtime.root, second['session_id'])
        assert runtime._identity(child, 'third')[0]['permission'] == 'read-only'
        assert second['participant_replacement']['permission_ceiling'] == 'read-only'
    finally:
        runtime.close(timeout=1)


@pytest.mark.parametrize('target', ['worker', 'observer', 'missing', '../invalid', ''], ids=['p006_case_001', 'p006_case_002', 'p006_case_003', 'p006_case_004', 'p006_case_005'])
def test_replacement_requires_a_distinct_available_member_binding(tmp_path, target):
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path, participants=[
        {'agent': 'worker'}, {'agent': 'observer'}])
    try:
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError):
            runtime.fork_turn('room-1', 'worker', 'Refuse target', replacement_agent=target)
        assert _stored(runtime.root) == before
        assert runtime._active == {}
    finally:
        runtime.close(timeout=1)


@pytest.mark.parametrize('fault', ['duplicate', 'conflicting_lineage'], ids=['p007_case_001', 'p007_case_002'])
def test_ambiguous_replacement_records_refuse_without_launch(tmp_path, fault):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntimeError
    runtime, journal, roster = _replacement_room(tmp_path)
    try:
        result = runtime.fork_turn('room-1', 'worker', 'First', replacement_agent='renamed')
        child = ConversationJournal.open(runtime.root, result['session_id'])
        payload = next(e['payload'] for e in child.events(native=True) if e['event'] == 'fork_created')
        if fault == 'conflicting_lineage':
            payload['parent_session_id'] = 'different-parent'
        child.append('fork_created', 'system', 'summon', payload, event_id='ambiguous-binding')
        before = _stored(runtime.root)
        with pytest.raises(ConversationRuntimeError, match='ambiguous'):
            runtime.start_turn(result['session_id'], 'renamed', 'Refuse ambiguous binding', wait=True)
        assert _stored(runtime.root) == before
        assert runtime._active == {}
    finally:
        runtime.close(timeout=1)


@pytest.mark.parametrize('value', [None, [], {}, 'unknown', True], ids=['p008_case_001', 'p008_case_002', 'p008_case_003', 'p008_case_004', 'p008_case_005'])
def test_replacement_projection_rejects_invalid_permission_types(tmp_path, value):
    from _conversation import ConversationError, _public_payload
    with pytest.raises(ConversationError, match='invalid participant replacement'):
        _public_payload('fork_created', {
            'parent_session_id': 'parent', 'child_session_id': 'child', 'reason': 'explicit',
            'participant_replacement': {'from_participant': 'worker', 'to_participant': 'renamed',
                'agent_definition_sha256': 'a' * 64, 'permission_ceiling': value}})


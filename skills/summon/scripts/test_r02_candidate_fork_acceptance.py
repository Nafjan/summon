"""R02 candidate/unsupported public runtime refusal and explicit fork table."""
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
    _HITS.clear(); _WRITABLE = tmp_path.resolve(); _ACTIVE = True
    try:
        yield
        assert not _HITS, 'a caught exception concealed a forbidden boundary'
    finally:
        _ACTIVE = False; _WRITABLE = None





@pytest.mark.parametrize('backend,state,reason',[
    ('cursor-agent','candidate','resume_candidate'),
    ('opencode','candidate','resume_candidate'),
    ('zcode','candidate','resume_candidate'),
    ('agy','unsupported','resume_unsupported'),
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_candidate_or_unsupported_turn_refuses_then_explicit_fork_is_nonlaunching(
        tmp_path,backend,state,reason):
    from _conversation import ConversationJournal
    from _conversation_runtime import ConversationRuntime
    from _resume_capabilities import (resume_capability,resume_capability_v2,
        is_exact_capability,is_exact_capability_v2)
    from _chat_resume import is_valid
    project=tmp_path/'project';project.mkdir()
    roster=tmp_path/'agents';roster.mkdir()
    (roster/'worker.md').write_text(
        f'---\nrun-agent: {backend}\nmodel: synthetic-model\npermission: read-only\n---\nSynthetic participant.\n')
    root=tmp_path/'rooms'
    journal=ConversationJournal.create(root,session_id='room-1',project_id='synthetic',
        project_root=project,initiator_host='codex',initiator_agent='human',mode='chat',
        participants=[{'agent':'worker','role':'researcher','name':'Worker','version':'1'}])
    runtime=ConversationRuntime(root,cwd=project,agents_dir=roster,strict_agents_dir=True)
    try:
        legacy=resume_capability(backend,'subprocess')
        current=resume_capability_v2('resume',backend,'subprocess')
        assert is_exact_capability(legacy) and is_exact_capability_v2(current)
        assert legacy['schema']=='summon.resume-capabilities/v1'
        assert current['schema']=='summon.resume-capabilities/v2'
        assert legacy['resume_state']==current['resume_state']==state
        assert current['launch_permission']=='not_granted'
        identity,execution=runtime._identity(journal,'worker')
        assert execution['resume_capability']==legacy
        family=runtime._ensure_chat_family(journal,'worker',identity,execution)
        assert family['source_family']['qualifications']==[]
        # Historical rows are explicitly synthetic and unqualified. No native
        # provider observation/model evidence/qualification is manufactured.
        journal.append_human_message('Synthetic historical question',message_id='old-question')
        journal.append('turn_started','system','summon',{
            'turn_id':'old-turn','participant':'worker','status':'started',
            'identity':identity},event_id='old-start')
        journal.append_agent_message('worker','human','Synthetic historical answer',message_id='old-answer')
        journal.append('turn_finished','system','summon',{
            'turn_id':'old-turn','participant':'worker','status':'success',
            'identity':identity,'resume_session_id':'synthetic-unqualified-handle'},event_id='old-finish')
        history=(root/'room-1.jsonl').read_bytes()
        records=journal.events(native=True)
        def files():return {p.relative_to(root):p.read_bytes() for p in root.rglob('*') if p.is_file()}
        before=files()
        result=runtime.start_turn('room-1','worker','Explicit continuation request',wait=True)
        assert result['status']=='blocked' and result['error_kind']=='chat_resume_refused'
        refusal=result['refusal']
        assert is_valid(refusal)
        assert refusal['reason_code']==reason
        assert refusal['capability']==legacy
        assert refusal['provider_contacted'] is False and refusal['attempts']==0
        assert refusal['action_offer']=={'kind':'fork','automatic':False}
        assert files()==before
        assert runtime._active=={}
        reopened=ConversationJournal.open(root,'room-1')
        assert reopened.events(native=True)==records
        assert (root/'room-1.jsonl').read_bytes()==history
        fork=runtime.fork_turn('room-1','worker','Explicit new conversation prompt',reason='operator fork after capability refusal')
        assert fork['status']=='forked' and fork['prompt_preserved'] is True
        assert fork['session_id']!='room-1'
        assert (root/'room-1.jsonl').read_bytes().startswith(history)
        parent=ConversationJournal.open(root,'room-1').events(native=True)
        assert parent[:-1]==records and parent[-1]['event']=='fork_created'
        child=ConversationJournal.open(root,fork['session_id'])
        events=child.events(native=True)
        assert any(e['event']=='human_message' and e['payload']['text']=='Explicit new conversation prompt' for e in events)
        assert not any(e['event'] in {'turn_started','turn_finished'} for e in events)
        assert runtime._active=={}
        assert not (root/'.chat-runtime'/fork['session_id']).exists()
    finally:
        runtime.close(timeout=1)

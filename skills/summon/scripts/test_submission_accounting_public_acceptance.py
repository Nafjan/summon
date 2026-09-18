"""Synthetic E07 public-boundary acceptance with self-contained I/O guards.

Runtime imports occur inside guarded tests. Reads are limited to this public
source directory, interpreter/test dependencies, and pytest-owned tmp_path;
all writes are confined to tmp_path. No source ancestor grants write authority.
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
                   '_arkcli_creds', 'keyring', 'win32cred')


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


def prepared(tmp_path):
 import _jobs
 job='8'*32
 _jobs.write_prepared(str(tmp_path),job,nonce='synthetic-nonce',agent='synthetic',prompt_sha256='0'*64,cwd=str(tmp_path),flags={},summon={},attempt_id=job)
 return _jobs,job,_jobs.record_path(str(tmp_path),job)

@pytest.mark.parametrize('schema',[None,False,True,2,'summon.background-launch/v999'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005'])
def test_review_unsupported_spawn_preserves_bytes(tmp_path,schema):
 jobs,job,path=prepared(tmp_path);record=jobs.read_json(path)
 if schema is None:record.pop('schema')
 else:record['schema']=schema
 jobs._atomic_write_json(path,record)
 before=open(path,'rb').read()
 with pytest.raises(ValueError):jobs.update_spawned(str(tmp_path),job,123)
 assert open(path,'rb').read()==before

def test_review_legacy_readable_but_not_mutable(tmp_path):
 jobs,job,path=prepared(tmp_path);record=jobs.read_json(path)
 record['schema']='summon.background-launch/v1'
 record.pop('accounting_handoff');record.pop('submission_accounting')
 jobs._atomic_write_json(path,record);before=open(path,'rb').read()
 assert jobs.job_status(str(tmp_path),job)['state']=='prepared'
 with pytest.raises(ValueError):jobs.update_spawned(str(tmp_path),job,123)
 assert open(path,'rb').read()==before

@pytest.mark.parametrize('kind',['initial','transient_retry','schema_correction','contract_repair'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004'])
def test_review_private_history_projections_and_durable_preservation(tmp_path,monkeypatch,capsys,kind):
 import _jobs,run_subagent as run
 import _submission_accounting as accounting
 from _builder import AgentInvocation
 from test_submission_accounting import _record
 inv=AgentInvocation(cli='codex',prompt='synthetic-private-task',cwd=str(tmp_path),system_context='synthetic-rules',request_sha256='6'*64,attempt_kind=kind)
 private=_record(inv,'6'*32,{'total_tokens':2},{'scope':'attempt_total','source':'synthetic'})
 model={'requested':'synthetic-model','targeted':'synthetic-model','served':'synthetic-model'}
 envelope={'status':'success','exit_code':0,'provider_contacted':True,'model':model,'served_model_evidence':'reported','model_match':True,'audit':{'synthetic_fact':True},'submission_accounting':private,'attempt_history':[{'attempt_id':'6'*32,'submission_accounting':copy.deepcopy(private)}]}
 original=copy.deepcopy(envelope);observed=[]
 monkeypatch.setattr(run._telemetry,'record',lambda obj,**kw:observed.append(copy.deepcopy(obj)))
 monkeypatch.setattr(run,'_JOB_FILE',None)
 run._emit(envelope)
 public=json.loads(capsys.readouterr().out)
 out=tmp_path/'out.json';run._write_out(str(out),envelope)
 assert json.loads(out.read_text())==public
 jobfile=tmp_path/'terminal.json';monkeypatch.setattr(run,'_JOB_FILE',str(jobfile));run._emit(envelope)
 terminal=json.loads(jobfile.read_text());assert not capsys.readouterr().out
 for value in (public,terminal):
  text=json.dumps(value)
  assert 'submission_accounting' not in text and 'material_sha256' not in text
  assert value['submission_summary']['physical_attempts']==1
  assert value['model']==model and value['audit']=={'synthetic_fact':True}
 assert envelope['submission_accounting']==original['submission_accounting']
 assert envelope['attempt_history']==original['attempt_history']
 assert all(value['submission_accounting']==private for value in observed)
 jobs,job,path=prepared(tmp_path/'durable')
 record=jobs.read_json(path);record['submission_accounting']=[private];jobs._atomic_write_json(path,record);before=open(path,'rb').read()
 status=jobs.job_status(str(tmp_path/'durable'),job);status['result']=envelope
 projected=jobs.public_job_status(status)
 assert 'submission_accounting' not in json.dumps(projected)
 assert projected['submission_summary']['physical_attempts']==1
 assert open(path,'rb').read()==before

def test_review_actual_retry_history_then_projection(monkeypatch,capsys):
 import run_subagent as run
 import _submission_accounting as accounting
 from test_submission_accounting import test_retry_dispatch_retains_each_attempt_accounting
 seen=[];attach=accounting.attach_public_summary
 def observe(value):seen.append(value);attach(value)
 monkeypatch.setattr(accounting,'attach_public_summary',observe)
 test_retry_dispatch_retains_each_attempt_accounting(monkeypatch)
 result=seen[-1];before=copy.deepcopy(result['attempt_history'])
 monkeypatch.setattr(run,'_JOB_FILE',None);monkeypatch.setattr(run._telemetry,'record',lambda *a,**kw:None)
 run._emit(result);public=json.loads(capsys.readouterr().out)
 assert 'submission_accounting' not in json.dumps(public)
 assert public['submission_summary']['physical_attempts']==2
 assert result['attempt_history']==before


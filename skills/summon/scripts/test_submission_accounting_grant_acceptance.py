"""Actual accounting grant producer and provider-free HTTP adapter acceptance."""
import io
import urllib.error
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




def _execute_with_produced_grants(tmp_path, monkeypatch, *, allow_payg, retries=0,
                                  transient=None):
    import argparse
    import _background
    import _executor
    import _apibackend
    import _jobs
    import _receipt
    import run_subagent
    from _builder import AgentInvocation
    args = argparse.Namespace(cli="openai-compat", allow_payg=allow_payg,
        retries=retries, no_acp_fallback=True, json_schema=None,
        no_contract_repair=True, timeout=5000, debug_dir=None,
        max_tool_output_bytes=None, gate_with=None,
        transient_retries=transient == "flag")
    grants = _background._submission_accounting_grants(args)
    job_id = "d" * 32
    nonce = "synthetic-grant-fence"
    _jobs.write_prepared(str(tmp_path), job_id, nonce=nonce,
        agent="synthetic", prompt_sha256="0" * 64, cwd=str(tmp_path),
        flags={"cli":"openai-compat"}, summon={}, attempt_id=job_id,
        accounting_grants=grants)
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", job_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    monkeypatch.setattr(_receipt, "workspace_snapshot", lambda *_a: None)
    monkeypatch.setattr(_apibackend, "resolve_api_credential",
        lambda *_a: ("synthetic-key", "env"))
    monkeypatch.setattr(run_subagent.time, "sleep", lambda *_a: None)
    inv = AgentInvocation(cli="openai-compat", prompt="synthetic task",
        cwd=str(tmp_path), system_context="synthetic rules", attempt_id=job_id,
        request_sha256="e" * 64, model="seed-1-6-250615",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        api_key_env="BYTEPLUS_CODING_API_KEY", allow_payg=allow_payg)
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def read(self):
            return json.dumps({"choices":[{"message":{"content":"ok"}}],
                "usage":{"total_tokens":3}}).encode()
    class SyntheticOpener:
        def open(self, request, **_kw):
            record = _jobs.read_json(_jobs.record_path(str(tmp_path),job_id))
            entries = record["submission_accounting"]
            assert len(entries) == len(calls) + 1
            assert entries[-1]["submission_state"] == "possible"
            calls.append(request.full_url)
            if "/api/coding/" in request.full_url:
                raise urllib.error.HTTPError(request.full_url,429,"quota",{},
                    io.BytesIO(b'{"error":"quota exceeded"}'))
            if (retries or transient) and len(calls) == 2:
                raise urllib.error.HTTPError(request.full_url,503,"temporary",{},
                    io.BytesIO(b'{"error":"temporarily unavailable"}'))
            return Response()
    monkeypatch.setattr(_apibackend,"_opener",lambda:SyntheticOpener())
    result = (run_subagent._dispatch_with_retries(inv,args) if retries or transient
              else _executor.execute_agent(inv,timeout_ms=5000))
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path),job_id))
    return grants,calls,result,durable


@pytest.mark.parametrize('mode', ['standing_env', 'fresh_only_refusal', 'explicit_fresh_flag'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_produced_grant_and_real_adapter_follow_existing_consent(tmp_path,monkeypatch,mode):
    monkeypatch.setenv('SUMMON_ALLOW_BYTEPLUS_PAYG','1')
    if mode != 'standing_env': monkeypatch.setenv('SUMMON_FRESH_CONSENT_ONLY','1')
    explicit = mode == 'explicit_fresh_flag'
    grants,calls,result,durable = _execute_with_produced_grants(
        tmp_path,monkeypatch,allow_payg=explicit)
    allowed = mode != 'fresh_only_refusal'
    assert grants['payg_fallback'] == int(allowed)
    assert len(calls) == (2 if allowed else 1)
    assert len(durable['submission_accounting']) == len(calls)
    if allowed:
        assert result['status'] == 'success'
        assert durable['submission_accounting'][1]['attempt']['kind'] == 'payg_fallback'
    else:
        assert result['status'] == 'error'
        assert result.get('error_kind') != 'submission_accounting_unavailable'
        assert 'consent is missing' in result['error']


def test_produced_grant_covers_fallback_for_each_authorized_outer_retry(tmp_path,monkeypatch):
    monkeypatch.setenv('SUMMON_ALLOW_BYTEPLUS_PAYG','1')
    grants,calls,result,durable = _execute_with_produced_grants(
        tmp_path,monkeypatch,allow_payg=False,retries=1)
    assert grants['transient_retry'] == 1
    # --retries=1 authorizes the second outer attempt; its existing standing
    # fallback consent remains valid, so accounting must not prevent request 4.
    observed = {'request_count':len(calls), 'error_kind':result.get('error_kind'),
        'payg_grant':grants['payg_fallback'],
        'durable_kinds':[r['attempt']['kind'] for r in durable['submission_accounting']],
        'durable_states':[r['submission_state'] for r in durable['submission_accounting']]}
    assert len(calls) == 4, observed
    assert result['status'] == 'success'
    assert len(durable['submission_accounting']) == 4


@pytest.mark.parametrize('opt_in',['flag','environment'], ids=['p002_case_001', 'p002_case_002'])
def test_opt_in_transient_extra_uses_real_produced_grant_and_http(tmp_path,monkeypatch,opt_in):
    monkeypatch.setenv('SUMMON_ALLOW_BYTEPLUS_PAYG','1')
    if opt_in=='environment':
        monkeypatch.setenv('SUMMON_TRANSIENT_RETRIES','1')
    grants,calls,result,durable=_execute_with_produced_grants(
        tmp_path,monkeypatch,allow_payg=False,retries=0,transient=opt_in)
    assert grants['transient_retry']==1 and grants['payg_fallback']==2
    assert len(calls)==4
    assert result['status']=='success'
    assert [r['attempt']['kind'] for r in durable['submission_accounting']] == [
        'initial','payg_fallback','transient_retry','payg_fallback']
    assert all(r['submission_state']=='submitted' for r in durable['submission_accounting'])


@pytest.mark.parametrize('extra_slot',[False,True],ids=['exact64','aggregate65'])
def test_actual_prepared_record_enforces_aggregate64_preflight(tmp_path,extra_slot):
    import argparse
    import _background
    import _jobs
    args=argparse.Namespace(cli='openai-compat',allow_payg=True,retries=31,
        transient_retries=False,no_acp_fallback=not extra_slot,
        json_schema=None,no_contract_repair=True)
    grants=_background._submission_accounting_grants(args)
    assert sum(grants.values())==64+int(extra_slot)
    root=str(tmp_path/'budget-jobs');job_id='a'*32
    path=Path(_jobs.record_path(root,job_id))
    kwargs=dict(nonce='synthetic-budget-fence',agent='synthetic',
        prompt_sha256='0'*64,cwd=str(tmp_path),flags={},summon={},
        attempt_id=job_id,accounting_grants=grants)
    if extra_slot:
        with pytest.raises(ValueError,match='64-record journal budget'):
            _jobs.write_prepared(root,job_id,**kwargs)
        assert not path.exists()
        assert not list(Path(root).glob('*.json'))
    else:
        _jobs.write_prepared(root,job_id,**kwargs)
        record=_jobs.read_json(str(path))
        assert record['accounting_handoff']['grants']==grants
        assert record['submission_accounting']==[]


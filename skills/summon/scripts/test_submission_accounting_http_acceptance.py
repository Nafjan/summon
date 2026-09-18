"""Provider-free E07 actual HTTP adapter/accounting acceptance.

The real executor, API call and request body builder reach only SyntheticOpener.
Guards are active before consumer imports; all state is under pytest tmp_path.
Credential resolution and consent are synthetic, and workspace snapshot effects
are excluded. No request/accounting/durable implementation is stubbed.
"""
import hashlib
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



def test_byteplus_payg_second_request_is_distinct_durable_child(tmp_path, monkeypatch):
    import _executor
    import _apibackend
    import _jobs
    import _submission_accounting as accounting
    import _receipt
    from _builder import AgentInvocation
    # Workspace filesystem effects are outside HTTP-accounting acceptance.
    # execute_agent imports this symbol locally; prevent its unrelated git probe.
    monkeypatch.setattr(_receipt, "workspace_snapshot", lambda *_args: None)
    attempt_id = "d" * 32
    nonce = "private-fence"
    inv = AgentInvocation(
        cli="openai-compat", prompt="task", cwd=str(tmp_path),
        system_context="rules", attempt_id=attempt_id,
        request_sha256="e" * 64, model="seed-1-6-250615",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        api_key_env="BYTEPLUS_CODING_API_KEY", allow_payg=True)
    _jobs.write_prepared(
        str(tmp_path), attempt_id, nonce=nonce, agent="synthetic",
        prompt_sha256=hashlib.sha256(b"task").hexdigest(), cwd=str(tmp_path),
        flags={"cli": "openai-compat"}, summon={}, attempt_id=attempt_id,
        accounting_grants={"initial": 1, "payg_fallback": 1})
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", attempt_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    monkeypatch.setattr(_apibackend, "resolve_api_credential",
                        lambda *_args: ("synthetic-key", "env"))
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda _flag: True)
    calls = []

    class SyntheticResponse:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}],
                               "usage": {"total_tokens": 3}}).encode()

    class SyntheticOpener:
        def open(self, request, **_kwargs):
            durable_before = _jobs.read_json(
                _jobs.record_path(str(tmp_path), attempt_id))
            pending = durable_before["submission_accounting"]
            assert len(pending) == len(calls) + 1
            assert pending[-1]["submission_state"] == "possible"
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, "quota", {},
                    io.BytesIO(b'{"error":"quota exceeded"}'))
            return SyntheticResponse()

    monkeypatch.setattr(_apibackend, "_opener", lambda: SyntheticOpener())
    result = _executor.execute_agent(inv, timeout_ms=5000)
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path), attempt_id))
    records = durable["submission_accounting"]
    assert len(calls) == 2
    assert len(records) == 2
    assert records[1]["attempt"]["id"] != attempt_id
    assert records[1]["attempt"]["parent_id"] == attempt_id
    assert records[1]["attempt"]["kind"] == "payg_fallback"
    summary = accounting.public_summary(result["submission_accounting"])
    assert summary["physical_attempts"] == 2
    assert summary["reported"]["total_tokens"]["known_subtotal"] == 3


def test_payg_preparation_failure_refuses_second_and_preserves_first(tmp_path, monkeypatch):
    import _executor
    import _apibackend
    import _jobs
    import _submission_accounting as accounting
    import _receipt
    from _builder import AgentInvocation
    # Workspace filesystem effects are outside HTTP-accounting acceptance.
    # execute_agent imports this symbol locally; prevent its unrelated git probe.
    monkeypatch.setattr(_receipt, "workspace_snapshot", lambda *_args: None)
    attempt_id = "f" * 32
    nonce = "private-fence"
    inv = AgentInvocation(
        cli="openai-compat", prompt="task", cwd=str(tmp_path),
        system_context="rules", attempt_id=attempt_id,
        request_sha256="1" * 64, model="seed-1-6-250615",
        base_url="https://ark.ap-southeast.bytepluses.com/api/coding/v3",
        api_key_env="BYTEPLUS_CODING_API_KEY", allow_payg=True)
    _jobs.write_prepared(
        str(tmp_path), attempt_id, nonce=nonce, agent="synthetic",
        prompt_sha256="0" * 64, cwd=str(tmp_path), flags={}, summon={},
        attempt_id=attempt_id, accounting_grants={"initial": 1})
    monkeypatch.setenv("SUMMON_JOB_DIR", str(tmp_path))
    monkeypatch.setenv("SUMMON_JOB_ID", attempt_id)
    monkeypatch.setenv("SUMMON_JOB_NONCE", nonce)
    monkeypatch.setattr(_apibackend, "resolve_api_credential",
                        lambda *_args: ("synthetic-key", "env"))
    monkeypatch.setattr(_apibackend, "payg_consent_allowed", lambda _flag: True)
    calls = []
    class SyntheticOpener:
        def open(self, request, **_kwargs):
            durable_before = _jobs.read_json(
                _jobs.record_path(str(tmp_path), attempt_id))
            pending = durable_before["submission_accounting"]
            assert len(pending) == len(calls) + 1
            assert pending[-1]["submission_state"] == "possible"
            calls.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 429, "quota", {},
                io.BytesIO(b'{"error":"quota exceeded"}'))
    monkeypatch.setattr(_apibackend, "_opener", lambda: SyntheticOpener())
    result = _executor.execute_agent(inv, timeout_ms=5000)
    durable = _jobs.read_json(_jobs.record_path(str(tmp_path), attempt_id))
    assert len(calls) == 1
    assert result["error_kind"] == "submission_accounting_unavailable"
    assert len(durable["submission_accounting"]) == 1
    assert durable["submission_accounting"][0]["submission_state"] == "submitted"




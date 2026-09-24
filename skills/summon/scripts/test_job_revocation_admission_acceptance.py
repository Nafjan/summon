"""R01 job revocation must precede the durable admission linearization point.

Admission is pending -> launch_claimed in the authenticated source claim.
A completed revocation before that transition must refuse; revocation after
admission is not retroactive cancellation. Synthetic qualified fixtures only.
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




@pytest.mark.parametrize('gate', [False,True], ids=['provider','gate'])
@pytest.mark.parametrize('ordering', ['before_check','between_check_and_cas','after_claim'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003'])
def test_revocation_ordering_at_actual_job_durable_admission(tmp_path,monkeypatch,gate,ordering):
    import _jobs
    import _job_resume as resume
    import _launch_qualification as qualification
    from _executor import ProviderLaunchRefusal
    from test_job_resume import _prepare, _launch_evidence
    root,source_id,reservation,prepared=_prepare(tmp_path,gate_with='synthetic-reviewer' if gate else None)
    resume.claim_transition(root,source_id,reservation.claim_id,field='parent_phase',
        expected='successor_prepared',target='child_launch_claimed')
    context=resume.load_child_context(_jobs.result_path(root,reservation.successor_job_id),prepared['claim_file'])
    field='gate_phase' if gate else 'provider_phase'
    assert resume.get_claim(reservation)[field]=='pending'
    ledger=Path(resume.ledger_path(root,source_id))
    before=ledger.read_bytes()
    control=resume.provider_launch_control(context,gate=gate)
    revocation_id=context.launch_qualification['revocation_id']
    revoked=[]
    import threading
    trace=[]
    worker=None
    completed=threading.Event()
    attempted=threading.Event()
    errors=[]
    def revoke_before_admission():
        assert resume.get_claim(reservation)[field]=='pending'
        assert ledger.read_bytes()==before
        qualification.revoke(root,source_id,revocation_id)
        assert qualification.is_revoked(root,source_id,revocation_id)
        assert ledger.read_bytes()==before
        revoked.append(True)
    if ordering=='before_check':
        revoke_before_admission()
    elif ordering=='between_check_and_cas':
        mutate=resume._mutate_claim
        write_authenticated=resume._write_authenticated
        def record_commit(path,nonce,domain,body):
            result=write_authenticated(path,nonce,domain,body)
            if Path(path)==ledger and any(c.get('claim_id')==reservation.claim_id
                    and c.get(field)=='launch_claimed' for c in body.get('claims',[])):
                trace.append('admission_committed')
            return result
        def revoker():
            attempted.set()
            try:
                qualification.revoke(root,source_id,revocation_id)
                trace.append('revocation_completed')
                revoked.append(True)
            except Exception as exc:
                errors.append(type(exc).__name__)
            finally:
                completed.set()
        def race_then_actual_cas(*args,**kwargs):
            nonlocal worker
            # A shared source lock may correctly block this real revoker until
            # admission commits. Never demand nested synchronous lock progress.
            worker=threading.Thread(target=revoker,daemon=True)
            worker.start()
            assert attempted.wait(2), 'revoker did not reach its attempted boundary'
            if completed.wait(0.3):
                assert not errors
                assert resume.get_claim(reservation)[field]=='pending'
                assert ledger.read_bytes()==before
            return mutate(*args,**kwargs)
        monkeypatch.setattr(resume,'_write_authenticated',record_commit)
        monkeypatch.setattr(resume,'_mutate_claim',race_then_actual_cas)
    reason=None
    try:
        control.before_provider_launch(_launch_evidence())
    except ProviderLaunchRefusal as exc:
        reason=exc.error_kind
    finally:
        if worker is not None:
            worker.join(3)
            assert not worker.is_alive(), 'owned revoker did not finish'
    assert not errors
    claim=resume.get_claim(reservation)
    if ordering=='after_claim':
        assert reason is None and claim[field]=='launch_claimed'
        admitted=ledger.read_bytes()
        qualification.revoke(root,source_id,revocation_id)
        assert qualification.is_revoked(root,source_id,revocation_id)
        assert ledger.read_bytes()==admitted
        assert resume.get_claim(reservation)[field]=='launch_claimed'
    else:
        assert revoked==[True]
        assert qualification.is_revoked(root,source_id,revocation_id)
        admitted_first=(ordering=='between_check_and_cas'
            and 'admission_committed' in trace
            and trace.index('admission_committed') < trace.index('revocation_completed'))
        if admitted_first:
            # Correct serialization: actual revocation completed after commit.
            assert reason is None and claim[field]=='launch_claimed'
        else:
            assert reason=='resume_launch_qualification_revoked', {
                'ordering':ordering,'phase':claim[field],
                'refusal_kind':reason,'boundary_order':trace}
            assert claim[field]=='pending'
            assert ledger.read_bytes()==before
    # No fake spawned callback or provider process is used in any ordering.
    assert claim.get('provider_contacted') in (None,False)



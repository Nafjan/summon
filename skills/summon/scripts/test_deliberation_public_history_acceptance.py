"""Synthetic historical context through real public durable readers."""
from pathlib import Path
import pytest
import _deliberation_store as store
import _rundir
from test_deliberation_cli import _receipt
from test_deliberation_replay import legacy_context_projection

@pytest.mark.parametrize('reader', [store.inspect_run, store.replay_run], ids=['status', 'replay'])
def test_historical_context_public_durable_reader_is_read_only(tmp_path, reader):
    root = str(tmp_path / 'runs')
    run_id = 'run-1'
    receipt = _receipt(run_id)
    legacy = legacy_context_projection()
    receipt['durable_context'] = legacy
    path = store.run_dir(root, run_id)
    # Reproduce the historical producer format without weakening the current
    # initializer, which must not create new legacy authority.
    owner = _rundir.acquire_owner(path, 600)
    try:
        _rundir.atomic_write_json(str(Path(path, 'receipt.json')), receipt)
        _rundir.journal_append(path, {
            'event': 'run_prepared', 'schema_version': store.SCHEMA_VERSION,
            'generation': owner.generation, 'run_id': run_id,
            'receipt_sha256': _rundir.content_sha256(receipt),
        }, owner=owner)
    finally:
        _rundir.release_owner(owner)
    def snapshot():
        return {p.relative_to(Path(path)).as_posix(): p.read_bytes() if p.is_file() else None
                for p in Path(path).rglob('*')}
    before = snapshot()
    result = reader(root, run_id)
    assert result['status'] == 'success'
    assert result['consistent'] is True
    assert result['run_id'] == run_id
    assert result['receipt']['decision_id'] == receipt['decision_id']
    projected = result['receipt']['durable_context']
    assert projected['legacy_read_only'] is True
    assert projected['routing_authority'] is False
    assert projected['packet_sha256'] == legacy['packet_sha256']
    assert projected['binding_sha256'] == legacy['binding_sha256']
    assert 'entries' not in projected
    assert 'run_id' not in projected
    assert snapshot() == before


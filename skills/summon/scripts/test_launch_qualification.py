"""Provider-free tests for the separate launch qualification authority."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _jobs
import _launch_qualification as qualification


def _packet(tmp_path):
    root = str(tmp_path / "jobs")
    job_id = "a" * 32
    (Path(root) / ".summon-records").mkdir(parents=True)
    _jobs._atomic_write_json(_jobs.record_path(root, job_id), {
        "attempt_id": "b" * 32, "nonce": "c" * 32,
    })
    observation = {
        "schema": "summon.resume-launch-observation/v1",
        "backend": "claude", "transport": "subprocess",
        "registry_generation": 1, "registry_digest": "sha256:" + "1" * 64,
        "adapter": "summon-cli-subprocess", "adapter_version": "summon-executor/3.4.0",
        "external_cli_version": "claude-test/1.0",
        "executable_sha256": "2" * 64, "launch_material_sha256": "3" * 64,
    }
    fields = dict(operation="resume", backend="claude", transport="subprocess",
                  registry_generation=1, registry_digest=observation["registry_digest"],
                  adapter=observation["adapter"], adapter_version=observation["adapter_version"],
                  external_cli_version=observation["external_cli_version"],
                  material_contract="summon-claude-subprocess-material/v1",
                  executable_sha256=observation["executable_sha256"],
                  launch_material_sha256=observation["launch_material_sha256"])
    fields["revocation_id"] = qualification.revocation_id_for(fields)
    record = qualification.issue(
        source_job_id=job_id, source_attempt_id="b" * 32,
        expires_at=time.time() + 600, source_nonce="c" * 32, **fields)
    return root, job_id, observation, record


def test_authenticated_qualification_matches_only_exact_observation(tmp_path):
    root, job_id, observation, record = _packet(tmp_path)
    qualification.write(root, job_id, record)
    loaded = qualification.read(root, job_id)
    assert loaded == record
    assert qualification.validate(loaded, observation, operation="resume",
                                   backend="claude", transport="subprocess")
    changed = dict(observation, executable_sha256="4" * 64)
    assert not qualification.validate(loaded, changed, operation="resume",
                                      backend="claude", transport="subprocess")


def test_qualification_rejects_expiry_revocation_and_placeholder_version(tmp_path):
    _root, _job_id, observation, record = _packet(tmp_path)
    assert not qualification.validate(
        dict(record, expires_at=time.time() - 1), observation,
        operation="resume", backend="claude", transport="subprocess")
    assert not qualification.validate(
        dict(record, revocation_id="f" * 64), observation,
        operation="resume", backend="claude", transport="subprocess")
    with_placeholder = dict(record, external_cli_version="not_declared")
    assert not qualification.validate(
        with_placeholder, observation, operation="resume",
        backend="claude", transport="subprocess")


def test_qualification_sidecar_tampering_is_rejected(tmp_path):
    root, job_id, _observation, record = _packet(tmp_path)
    qualification.write(root, job_id, record)
    path = Path(qualification.qualification_path(root, job_id))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["material_contract"] = "forged"
    path.write_text(json.dumps(value), encoding="utf-8")
    with __import__("pytest").raises(qualification.QualificationError):
        qualification.read(root, job_id)


def test_trusted_revocation_is_separate_from_signed_qualification(tmp_path):
    root, job_id, observation, record = _packet(tmp_path)
    qualification.write(root, job_id, record)
    assert qualification.validate(
        qualification.read(root, job_id), observation, operation="resume",
        backend="claude", transport="subprocess")
    qualification.revoke(root, job_id, record["revocation_id"])
    # The signed qualification bytes remain unchanged; current authority is
    # checked separately at the launch boundary.
    assert qualification.read(root, job_id) == record
    assert qualification.is_revoked(root, job_id, record["revocation_id"])

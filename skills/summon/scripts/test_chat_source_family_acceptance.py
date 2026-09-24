"""Source-family stale writer acceptance with synthetic authority only."""
from pathlib import Path
import sys
import time

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_source_family as family
import _chat_launch_qualification as qualification
import _launch_qualification as policy
from _launch_binding import binding_projection
from test_chat_launch_guard_acceptance import _evidence


def _packet(tmp_path):
    identity = dict(session_id="synthetic-room", participant="synthetic-agent",
                    project_root_sha256="1" * 64, identity_sha256="2" * 64)
    fid = family.source_family_id(**identity)
    path = family.family_path(tmp_path)
    original = family.ensure(path, source_family_id_value=fid, **identity)
    observation = _evidence()["launch_observation"]
    fields = {key: observation[key] for key in (
        "backend", "transport", "registry_generation", "registry_digest",
        "adapter", "adapter_version", "external_cli_version",
        "executable_sha256", "launch_material_sha256")}
    fields["material_contract"] = policy.material_contract_for(**{
        key: observation[key] for key in ("backend", "transport", "adapter", "adapter_version")})
    revocation = policy.revocation_id_for(dict(fields, operation="chat_resume"))

    def issue(turn):
        return qualification.issue(
            source_family_id=fid, turn_id=turn, observation=binding_projection(observation),
            expires_at=time.time() + 600, revocation_id=revocation,
            token=original["nonce"], **fields)

    return path, original, observation, issue


def _files(tmp_path):
    return {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}


def test_stale_qualification_writer_cannot_erase_revocation(tmp_path):
    path, original, observation, issue = _packet(tmp_path)
    qualified = issue("turn-one")
    family.revoke(path, original, qualified["revocation_id"])
    before = _files(tmp_path)
    with pytest.raises(family.SourceFamilyError):
        family.write_qualification(path, original, qualified, observation)
    assert family.is_revoked(path, original, qualified["revocation_id"])
    assert _files(tmp_path) == before


def test_stale_writer_preserves_previously_published_turn_link(tmp_path):
    path, original, observation, issue = _packet(tmp_path)
    family.write_qualification(path, original, issue("turn-one"), observation)
    before = _files(tmp_path)
    try:
        family.write_qualification(path, original, issue("turn-two"), observation)
    except family.SourceFamilyError:
        assert _files(tmp_path) == before
    else:
        current = family.read(path, expected_id=original["source_family_id"])
        assert {item["turn_id"] for item in current["qualifications"]} == {"turn-one", "turn-two"}

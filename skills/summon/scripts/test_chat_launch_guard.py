from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _chat_launch_guard
import _chat_launch_qualification
import _chat_source_family
from _launch_binding import observation
from _rundir import acquire_owner, release_owner


class ChatLaunchGuardTests(unittest.TestCase):
    def _evidence(self, backend="claude"):
        obs = observation(sys.executable, [__file__], os.getcwd(), os.environ,
                          backend=backend, transport="subprocess")
        return {
            "schema": "summon.fleet-launch-evidence/v1",
            "backend": backend,
            "transport": "subprocess",
            "launch_observation": obs,
        }, obs

    def test_guard_is_authenticated_single_use_and_binds_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = acquire_owner(tmp, 30)
            try:
                path = str(Path(tmp) / "launch-guard-turn.json")
                token = "t" * 64
                evidence, _obs = self._evidence()
                _chat_launch_guard.create(
                    path, token, expected=None, backend="claude",
                    transport="subprocess", turn_id="turn-1",
                    owner_generation=owner.generation, owner_nonce=owner.nonce,
                    attempt_id="attempt-1",
                )
                _chat_launch_guard.before_launch(
                    path, token, evidence, backend="claude",
                    transport="subprocess", attempt_id="attempt-1")
                self.assertIsInstance(_chat_launch_guard.read(path, token), dict)
                with self.assertRaises(ValueError):
                    _chat_launch_guard.before_launch(
                        path, token, evidence, backend="claude",
                        transport="subprocess", attempt_id="attempt-1")
            finally:
                release_owner(owner)

    def test_guard_rejects_stale_observation_and_wrong_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = acquire_owner(tmp, 30)
            try:
                evidence, obs = self._evidence()
                obs["observed_at_ns"] = time.time_ns() - 31 * 60 * 1_000_000_000
                path = str(Path(tmp) / "stale.json")
                _chat_launch_guard.create(
                    path, "s" * 64, expected=None, backend="claude",
                    transport="subprocess", turn_id="turn-2",
                    owner_generation=owner.generation, owner_nonce=owner.nonce,
                    attempt_id="attempt-2")
                with self.assertRaises(ValueError):
                    _chat_launch_guard.before_launch(
                        path, "s" * 64, evidence, backend="claude",
                        transport="subprocess", attempt_id="attempt-2")
                wrong = str(Path(tmp) / "wrong-owner.json")
                _chat_launch_guard.create(
                    wrong, "w" * 64, expected=None, backend="claude",
                    transport="subprocess", turn_id="turn-3",
                    owner_generation=owner.generation, owner_nonce="0" * 32,
                    attempt_id="attempt-3")
                fresh, _ = self._evidence()
                with self.assertRaises(ValueError):
                    _chat_launch_guard.before_launch(
                        wrong, "w" * 64, fresh, backend="claude",
                        transport="subprocess", attempt_id="attempt-3")
            finally:
                release_owner(owner)

    def test_continuation_guard_requires_authenticated_chat_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner = acquire_owner(tmp, 30)
            try:
                path = str(Path(tmp) / "qualified.json")
                token = "q" * 64
                family_owner = Path(tmp) / "family"
                project_digest = "1" * 64
                identity_digest = "2" * 64
                source_id = _chat_source_family.source_family_id(
                    session_id="test-room", participant="worker",
                    project_root_sha256=project_digest,
                    identity_sha256=identity_digest)
                family = _chat_source_family.ensure(
                    _chat_source_family.family_path(family_owner),
                    source_family_id_value=source_id, session_id="test-room",
                    participant="worker", project_root_sha256=project_digest,
                    identity_sha256=identity_digest)
                evidence, obs = self._evidence()
                # A real qualified adapter observation must carry a concrete
                # vendor version and resolver-owned material contract.
                obs.update(external_cli_version="claude-test/1.0")
                from _launch_qualification import material_contract_for, revocation_id_for
                common = {
                    "backend": "claude", "transport": "subprocess",
                    "registry_generation": obs["registry_generation"],
                    "registry_digest": obs["registry_digest"], "adapter": obs["adapter"],
                    "adapter_version": obs["adapter_version"],
                    "external_cli_version": obs["external_cli_version"],
                    "material_contract": material_contract_for(
                        backend="claude", transport="subprocess",
                        adapter=obs["adapter"], adapter_version=obs["adapter_version"]),
                    "executable_sha256": obs["executable_sha256"],
                    "launch_material_sha256": obs["launch_material_sha256"],
                }
                common["operation"] = "chat_resume"
                common["revocation_id"] = revocation_id_for(common)
                common.pop("operation", None)
                qualification = _chat_launch_qualification.issue(
                    source_family_id=source_id, turn_id="turn-qualified",
                    expires_at=time.time() + 600, token=family["nonce"],
                    observation=obs, **common)
                _chat_launch_guard.create(
                    path, token, expected=None, backend="claude", transport="subprocess",
                    turn_id="turn-qualified", owner_generation=owner.generation,
                    owner_nonce=owner.nonce, attempt_id="attempt-qualified",
                    qualification=qualification, qualification_required=True,
                    source_family_id=source_id,
                    qualification_source_turn_id="turn-qualified",
                    qualification_token=family["nonce"],
                    source_family_path=_chat_source_family.family_path(family_owner))
                with self.assertRaises(ValueError):
                    _chat_launch_guard.before_launch(
                        path, token, evidence, backend="claude", transport="subprocess",
                        attempt_id="attempt-qualified")
                evidence["launch_qualification"] = qualification
                _chat_launch_guard.before_launch(
                    path, token, evidence, backend="claude", transport="subprocess",
                    attempt_id="attempt-qualified",
                    qualification_token=family["nonce"],
                    source_family_path=_chat_source_family.family_path(family_owner))
                self.assertEqual(_chat_launch_guard.read_qualification(path, token), qualification)
            finally:
                release_owner(owner)


if __name__ == "__main__":
    unittest.main()

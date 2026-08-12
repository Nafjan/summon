"""Mutation-oriented tests for the side-effect-free deliberation roster phase."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_roster as roster


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class FrozenRosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cwd = self.root / "project"
        self.agents = self.cwd / ".agents"
        self.cwd.mkdir()
        self.agents.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_agent(self, name: str, *, cli: str = "claude", permission: str = "read-only",
                  extra: str = "", transport: str | None = None,
                  model: str | None = None, capability: str | None = None) -> Path:
        lines = ["---", f"run-agent: {cli}", f"permission: {permission}"]
        if transport:
            lines.append(f"transport: {transport}")
        if model:
            lines.append(f"model: {model}")
        if capability:
            lines.append(f"capability: {capability}")
        if extra:
            lines.append(f"args: {extra}")
        lines += ["---", "# Test seat", "A deterministic test definition.", ""]
        path = self.agents / f"{name}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def req(self, seat: str = "one", agent: str = "worker", **kwargs):
        return roster.SeatRequest(seat, agent, **kwargs)

    def proof(self, seat: str = "one") -> roster.WorktreeProof:
        worktree = self.root / f"wt-{seat}"
        worktree.mkdir(exist_ok=True)
        common = self.root / "repo.git"
        git = common / "worktrees" / seat
        git.mkdir(parents=True, exist_ok=True)
        (common / "objects").mkdir(exist_ok=True)
        (common / "refs").mkdir(exist_ok=True)
        (common / "config").write_text("[core]\nrepositoryformatversion = 0\n", encoding="ascii")
        commit = "0123456789abcdef0123456789abcdef01234567"
        (git / "HEAD").write_text(commit, encoding="ascii")
        (git / "commondir").write_text("../..", encoding="ascii")
        (git / "gitdir").write_text(f"gitdir: {worktree / '.git'}", encoding="utf-8")
        (worktree / ".git").write_text(f"gitdir: {git}", encoding="utf-8")
        return roster.WorktreeProof(str(worktree), roster._digest_text(commit), isolated=True,
                                    disposable=True)

    def freeze(self, requests=None, **kwargs):
        return roster.freeze_roster(
            requests or (self.req(),), cwd=str(self.cwd), agents_dir=str(self.agents),
            role_enabled=False, **kwargs)

    def test_read_only_freezes_same_frontmatter_and_transport(self):
        path = self.add_agent("worker", transport="subprocess", model="claude-sonnet-4-6")
        snap = self.freeze()
        seat = snap.seats[0]
        self.assertEqual(seat.transport, "subprocess")
        self.assertEqual(seat.model, "claude-sonnet-4-6")
        self.assertEqual(seat.definition_sha256,
                         hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(seat.authority_class, "enforceable")
        self.assertTrue(snap.revalidate())

    def test_definition_mutation_fails_revalidation(self):
        path = self.add_agent("worker")
        snap = self.freeze()
        path.write_text(path.read_text(encoding="utf-8").replace("read-only", "safe-edit"),
                        encoding="utf-8")
        self.assertFalse(snap.revalidate())

    def test_revalidation_inputs_are_immutable(self):
        self.add_agent("worker")
        snap = self.freeze()
        with self.assertRaises(TypeError):
            snap._kwargs["cli_overrides"]["one"] = "agy"
        self.assertTrue(snap.revalidate())

    def test_worktree_proof_is_revalidated_after_freeze(self):
        self.add_agent("worker", permission="safe-edit")
        proof = self.proof()
        snap = self.freeze(worktree_proofs={"one": proof})
        marker = Path(proof.path) / ".git"
        target = Path(marker.read_text(encoding="utf-8").split(":", 1)[1].strip())
        (target / "HEAD").write_text("f" * 40, encoding="ascii")
        self.assertFalse(snap.revalidate())

    def test_forged_git_directory_is_not_a_worktree_proof(self):
        fake = self.root / "fake"
        (fake / ".git").mkdir(parents=True)
        (fake / ".git" / "HEAD").write_text("1" * 40, encoding="ascii")
        with self.assertRaises(roster.RosterResolutionError):
            roster.WorktreeProof(str(fake), roster._digest_text("1" * 40),
                                 isolated=True, disposable=True)

    def test_profile_content_mutation_fails_revalidation(self):
        self.add_agent("worker")
        profile_dir = self.root / "profile"
        profile_dir.mkdir()
        credential = profile_dir / "credentials.json"
        credential.write_text('{"account":"A"}', encoding="utf-8")
        registry = self.root / "profiles.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile_dir)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}, clear=False):
            snap = self.freeze(profile_overrides={"one": "local"})
            credential.write_text('{"account":"B"}', encoding="utf-8")
            self.assertFalse(snap.revalidate())

    def test_profile_symlinked_content_is_refused(self):
        self.add_agent("worker")
        profile_dir = self.root / "profile"
        profile_dir.mkdir()
        target = self.root / "credentials-a"
        target.mkdir()
        (target / "token.json").write_text("A", encoding="utf-8")
        try:
            os.symlink(str(target), str(profile_dir / "auth"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        registry = self.root / "profiles.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile_dir)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}, clear=False):
            with self.assertRaises(roster.RosterResolutionError):
                self.freeze(profile_overrides={"one": "local"})

    def test_transport_is_from_loaded_snapshot_not_a_second_file_read(self):
        self.add_agent("worker", cli="kimi", transport="acp", permission="yolo")
        with mock.patch.object(roster, "load_agent_snapshot",
                               wraps=roster.load_agent_snapshot) as loaded:
            snap = self.freeze(full_authority_consent=("one",),
                               worktree_proofs={"one": self.proof()})
        self.assertEqual(loaded.call_count, 1)
        self.assertEqual(snap.seats[0].transport, "acp")

    def test_role_target_sha_is_bound(self):
        self.add_agent("worker")
        with mock.patch.object(
            roster, "resolve_for_dispatch",
            return_value={"resolved": "worker", "role": {
                "target_sha256": "0" * 64, "name": "reviewer"}},
        ):
            with self.assertRaises(roster.RosterResolutionError):
                self.freeze()

    def test_profile_and_memory_are_hashed_not_copied(self):
        self.add_agent("worker", extra="--path C:\\private\\fixture")
        memory = self.agents / "memory.md"
        memory.write_text("private project instructions", encoding="utf-8")
        profile_dir = self.root / "profile"
        profile_dir.mkdir()
        registry = self.root / "profiles.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile_dir)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}, clear=False):
            snap = self.freeze(profile_overrides={"one": "local"})
        seat = snap.seats[0]
        self.assertEqual(seat.memory_sha256, hashlib.sha256(memory.read_bytes()).hexdigest())
        self.assertIsNotNone(seat.profile_path_sha256)
        redacted = json.dumps(snap.as_dict(native=False), sort_keys=True)
        self.assertNotIn(str(profile_dir), redacted)
        self.assertNotIn("private", redacted)
        self.assertNotIn("C:\\\\private", redacted)
        native = snap.as_dict(native=True)
        self.assertIn("definition_source", native["seats"][0])
        self.assertIn("extra_args", native["seats"][0])

    def test_writable_requires_isolated_disposable_proof(self):
        self.add_agent("worker", permission="safe-edit")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze()
        snap = self.freeze(worktree_proofs={"one": self.proof()})
        self.assertEqual(snap.seats[0].authority_class, "writable")
        self.assertTrue(snap.seats[0].worktree_required)

    def test_full_bypass_requires_named_consent_and_worktree(self):
        self.add_agent("worker", permission="yolo")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(worktree_proofs={"one": self.proof()})
        snap = self.freeze(full_authority_consent=("one",),
                           worktree_proofs={"one": self.proof()})
        self.assertEqual(snap.seats[0].authority_class, "full-bypass")

    def test_invalid_permission_ceiling_fails_closed(self):
        self.add_agent("worker", permission="safe-edit")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(permission_ceilings={"one": "read-onyl"},
                        worktree_proofs={"one": self.proof()})

    def test_agy_read_only_never_uses_ambient_waiver(self):
        self.add_agent("worker", cli="agy", permission="read-only")
        with mock.patch.dict(os.environ, {"SUMMON_ALLOW_UNENFORCED_READONLY": "1"},
                             clear=False):
            with self.assertRaises(roster.RosterResolutionError):
                self.freeze()

    def test_agy_safe_edit_uses_effective_full_bypass(self):
        self.add_agent("worker", cli="agy", permission="safe-edit")
        snap = self.freeze(full_authority_consent=("one",),
                           worktree_proofs={"one": self.proof()})
        self.assertEqual(snap.seats[0].effective_permission, "yolo")
        self.assertEqual(snap.seats[0].authority_class, "full-bypass")

    def test_text_seat_needs_consent_but_stays_disabled(self):
        self.add_agent("worker", cli="openai-compat", permission="read-only")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze()
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(text_only_consent=("one",))

    def test_acp_non_yolo_fails_before_any_contact(self):
        self.add_agent("worker", cli="kimi", permission="safe-edit", transport="acp")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze(worktree_proofs={"one": self.proof()})

    def test_roster_and_seat_receipts_are_redaction_safe(self):
        self.add_agent("worker", extra="--secret-path C:\\Users\\nside\\private")
        request = self.req(role="TOP_SECRET_ROLE", persona="TOP_SECRET_PERSONA",
                           capabilities=("TOP_SECRET_CAPABILITY",))
        snap = self.freeze((request,))
        redacted = json.dumps(snap.as_dict(native=False), sort_keys=True)
        self.assertNotIn("definition_source", redacted)
        self.assertNotIn("Users", redacted)
        self.assertNotIn("secret-path", redacted)
        self.assertNotIn("private", redacted)
        self.assertNotIn("TOP_SECRET", redacted)
        self.assertIn("extra_args_sha256", redacted)
        self.assertNotIn("deterministic test definition", redacted)
        self.assertIn("deterministic test definition", snap.runtime_for("one")["definition_body"])

    def test_public_receipt_hashes_private_selected_identifiers(self):
        self.add_agent("secret-client-agent", model="confidential-model-alias")
        request = self.req(agent="secret-client-agent")
        snap = self.freeze((request,), profile_overrides={})
        redacted = json.dumps(snap.as_dict(native=False), sort_keys=True)
        for private in ("secret-client-agent", "confidential-model-alias", "one"):
            self.assertNotIn(private, redacted)

    def test_backend_environment_digest_is_read_once(self):
        self.add_agent("worker")
        with mock.patch.object(roster, "_backend_env_digest", return_value="a" * 64) as env:
            snap = self.freeze()
        self.assertEqual(env.call_count, 1)
        self.assertEqual(snap.seats[0].backend_env_sha256, "a" * 64)

    def test_no_provider_or_worktree_side_effects(self):
        self.add_agent("worker")
        with mock.patch.object(roster.shutil, "which", return_value=None):
            # PATH probing is evidence collection, not a provider side effect;
            # the resolver remains usable when no backend is installed.
            snap = self.freeze()
        self.assertIsNone(snap.seats[0].executable_sha256)


if __name__ == "__main__":
    unittest.main()

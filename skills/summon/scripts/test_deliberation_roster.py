"""Mutation-oriented tests for the side-effect-free deliberation roster phase."""

from __future__ import annotations

import hashlib
import ast
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
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
                  model: str | None = None, capability: str | None = None,
                  lifecycle: str | None = None, successor: str | None = None) -> Path:
        lines = ["---", f"run-agent: {cli}", f"permission: {permission}"]
        if transport:
            lines.append(f"transport: {transport}")
        if model:
            lines.append(f"model: {model}")
        if capability:
            lines.append(f"capability: {capability}")
        if lifecycle:
            lines.append(f"lifecycle: {lifecycle}")
        if successor:
            lines.append(f"successor: {successor}")
        if extra:
            lines.append(f"args: {extra}")
        lines += ["---", "# Test seat", "A deterministic test definition.", ""]
        path = self.agents / f"{name}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def add_custom_agent(self, slug: str = "custom-worker", *, root: Path | None = None,
                         cli: str = "claude", transport: str = "subprocess",
                         permission: str = "read-only", model: str = "model-a",
                         authority: str = "contained", consent: str = "none",
                         worktree: str = "none", body: str = "Use bounded evidence.") -> Path:
        package_root = root or (self.cwd / ".agents" / "agents")
        path = package_root / slug / "agent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join([
            "---", "schema_version: 1", f"name: {slug}",
            "description: Test custom participant", "role: participant",
            f"model: {model}", f"cli: {cli}", f"transport: {transport}",
            f"permission_ceiling: {permission}", f"authority_class: {authority}",
            "skills: [analysis]", f"consent_class: {consent}",
            f"worktree_mode: {worktree}", "---", body, "",
        ]), encoding="utf-8")
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

    def test_retired_seat_is_refused_at_shared_live_roster_boundary(self):
        self.add_agent("worker", cli="definitely-missing-provider", lifecycle="Retired",
                       successor="worker-v2")
        with self.assertRaisesRegex(
                roster.RosterResolutionError,
                "retired.*successor 'worker-v2'"):
            self.freeze()

        # Deprecated remains dispatchable for compatibility, but a live caller
        # receives the same normalized snapshot and can surface its warning.
        self.add_agent("worker", lifecycle="deprecated", successor="worker-v2")
        snap = self.freeze()
        self.assertEqual(snap.seat("one").resolved_agent, "worker")

    def test_public_roster_exposes_catalog_display_identity_without_private_model_bytes(self):
        self.add_agent("worker", model="claude-fable-5")
        snap = self.freeze()
        public = snap.as_dict(native=False)
        display = public["model_display_by_seat"]["one"]
        self.assertEqual(display["role"], "previous-version escalation")
        self.assertEqual(display["name"], "Fable")
        self.assertEqual(display["version"], "5")
        self.assertEqual(display["label"], "frontier")
        self.assertFalse(display["served_exact"])
        self.assertNotIn("claude-fable-5", json.dumps(public))

    def test_custom_agent_is_bound_as_redacted_immutable_evidence(self):
        self.add_agent("worker", model="model-a")
        package = self.add_custom_agent()
        snap = self.freeze((self.req(custom_agent="custom-worker"),))
        seat = snap.seat("one")
        self.assertIsNotNone(seat.custom_agent_definition_digest)
        self.assertEqual(seat.custom_agent_source_digest,
                         hashlib.sha256(package.read_bytes()).hexdigest())
        self.assertEqual(seat.custom_agent_identity["cli"], "claude")
        self.assertEqual(snap.runtime_for("one")["custom_agent_definition_body"],
                         "Use bounded evidence.")
        receipt = json.dumps(snap.as_dict(native=False), sort_keys=True)
        self.assertNotIn("custom-worker", receipt)
        self.assertNotIn("Use bounded evidence", receipt)
        self.assertIn(seat.custom_agent_definition_digest, receipt)
        with self.assertRaises(TypeError):
            seat.custom_agent_identity["cli"] = "agy"

    def test_custom_agent_missing_and_implicit_global_root_are_refused(self):
        self.add_agent("worker", model="model-a")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze((self.req(custom_agent="missing"),))
        global_root = self.root / "explicit-global"
        self.add_custom_agent(root=global_root)
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze((self.req(custom_agent="custom-worker"),))
        snap = self.freeze((self.req(custom_agent="custom-worker"),),
                           custom_agents_root=str(global_root))
        self.assertIsNotNone(snap.seat("one").custom_agent_definition_digest)

    def test_custom_agent_root_must_be_absolute_and_is_revalidation_bound(self):
        self.add_agent("worker", model="model-a")
        global_root = self.root / "explicit-global"
        self.add_custom_agent(root=global_root)
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze((self.req(custom_agent="custom-worker"),),
                        custom_agents_root="relative-agents")
        snap = self.freeze((self.req(custom_agent="custom-worker"),),
                           custom_agents_root=str(global_root))
        self.assertTrue(snap.revalidate())

    def test_custom_agent_identity_mismatch_fails_closed(self):
        cases = (
            {"cli": "codex"},
            {"transport": "subprocess", "permission": "safe-edit",
             "authority": "workspace-write", "worktree": "required"},
            {"model": "model-b"},
        )
        for index, manifest_changes in enumerate(cases):
            with self.subTest(case=index):
                package = self.cwd / ".agents" / "agents" / "custom-worker"
                if package.exists():
                    import shutil
                    shutil.rmtree(package)
                self.add_agent("worker", model="model-a")
                self.add_custom_agent(**manifest_changes)
                with self.assertRaises(roster.RosterResolutionError):
                    self.freeze((self.req(custom_agent="custom-worker"),))

        package = self.cwd / ".agents" / "agents" / "custom-worker"
        if package.exists():
            import shutil
            shutil.rmtree(package)
        self.add_agent("worker", cli="kimi", transport="acp", permission="yolo",
                       model="model-a")
        self.add_custom_agent(cli="kimi", transport="subprocess", permission="yolo",
                              authority="full-bypass", consent="full-authority",
                              worktree="required")
        with self.assertRaises(roster.RosterResolutionError):
            self.freeze((self.req(custom_agent="custom-worker"),),
                        full_authority_consent=("one",),
                        worktree_proofs={"one": self.proof()})

    def test_custom_agent_body_mutation_changes_receipt_and_revalidation(self):
        self.add_agent("worker", model="model-a")
        package = self.add_custom_agent(body="Original body.")
        before = self.freeze((self.req(custom_agent="custom-worker"),))
        package.write_text(package.read_text(encoding="utf-8").replace(
            "Original body.", "Mutated body."), encoding="utf-8")
        self.assertFalse(before.revalidate())
        after = self.freeze((self.req(custom_agent="custom-worker"),))
        self.assertNotEqual(before.roster_digest, after.roster_digest)
        self.assertNotEqual(before.seat("one").custom_agent_definition_digest,
                            after.seat("one").custom_agent_definition_digest)

    def test_forged_replaced_seat_cannot_keep_the_old_roster_seal(self):
        self.add_agent("worker", model="model-a")
        self.add_custom_agent()
        snap = self.freeze((self.req(custom_agent="custom-worker"),))
        forged_seat = replace(
            snap.seat("one"),
            custom_agent_identity={
                **dict(snap.seat("one").custom_agent_identity),
                "skills_sha256": ["f" * 64],
            },
        )
        forged = replace(snap, seats=(forged_seat,))
        self.assertFalse(forged.revalidate())

    def test_custom_agent_integration_has_no_provider_imports(self):
        imports = set()
        for filename in ("_deliberation_roster.py", "_deliberation_agents.py"):
            tree = ast.parse((HERE / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".", 1)[0])
        self.assertTrue({"_executor", "_deliberation_live", "subprocess", "urllib",
                         "requests"}.isdisjoint(imports))

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

    def test_profile_runtime_transcripts_logs_and_caches_do_not_invalidate(self):
        for cli, model in (("claude", "claude-fable-5-1"), ("codex", "gpt-5.6-sol")):
            with self.subTest(cli=cli):
                self.add_agent("worker", cli=cli, model=model)
                profile_dir = self.root / (cli + "-runtime-profile")
                profile_dir.mkdir()
                registry = self.root / (cli + "-runtime-registry.json")
                registry.write_text(json.dumps({"profiles": {
                    "local": {"cli": cli, "config_dir": str(profile_dir)}
                }}), encoding="utf-8")
                with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}):
                    snap = self.freeze(profile_overrides={"one": "local"})
                    for relative in ("projects/task/session.jsonl", "debug/session.log",
                                     "sessions/2026/turn.jsonl", "cache/usage.json"):
                        path = profile_dir / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("synthetic runtime output", encoding="utf-8")
                        self.assertTrue(snap.revalidate(), relative)
                        path.write_text("changed runtime output", encoding="utf-8")
                        self.assertTrue(snap.revalidate(), relative)
                        path.unlink()
                        self.assertTrue(snap.revalidate(), relative)

    def test_known_profile_identity_creation_change_and_removal_invalidate(self):
        cases = {
            "claude": {
                ".credentials.json": ('{"token":"A"}', '{"token":"B"}'),
                ".claude.json": ('{"oauthAccount":{"accountUuid":"A"}}',
                                 '{"oauthAccount":{"accountUuid":"B"}}'),
                "account.json": ('{"account":"A"}', '{"account":"B"}'),
                "auth.json": ('{"token":"A"}', '{"token":"B"}'),
                "credentials.json": ('{"token":"A"}', '{"token":"B"}'),
                "settings.json": ('{"model":"model-a"}', '{"model":"model-b"}'),
                "settings.local.json": ('{"env":{"ANTHROPIC_BASE_URL":"route-a"}}',
                                        '{"env":{"ANTHROPIC_BASE_URL":"route-b"}}'),
            },
            "codex": {
                "auth.json": ('{"account_id":"A"}', '{"account_id":"B"}'),
                "config.json": ('{"model":"model-a"}', '{"model":"model-b"}'),
                "config.toml": ('model_provider = "route-a"', 'model_provider = "route-b"'),
            },
        }
        for cli, files in cases.items():
            self.add_agent("worker", cli=cli,
                           model="gpt-5.6-sol" if cli == "codex" else "claude-fable-5-1")
            profile_dir = self.root / (cli + "-identity-profile")
            profile_dir.mkdir()
            registry = self.root / (cli + "-identity-registry.json")
            registry.write_text(json.dumps({"profiles": {
                "local": {"cli": cli, "config_dir": str(profile_dir)}
            }}), encoding="utf-8")
            with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}):
                for name, (before, after) in files.items():
                    with self.subTest(cli=cli, name=name):
                        path = profile_dir / name
                        absent = self.freeze(profile_overrides={"one": "local"})
                        path.write_text(before, encoding="utf-8")
                        self.assertFalse(absent.revalidate(), "creation must invalidate")
                        present = self.freeze(profile_overrides={"one": "local"})
                        path.write_text(after, encoding="utf-8")
                        self.assertFalse(present.revalidate(), "identity change must invalidate")
                        changed = self.freeze(profile_overrides={"one": "local"})
                        public = json.dumps(changed.as_dict(native=False))
                        self.assertNotIn(str(profile_dir), public)
                        self.assertNotIn(after, public)
                        path.unlink()
                        self.assertFalse(changed.revalidate(), "removal must invalidate")

    def test_profile_symlinked_identity_file_is_refused(self):
        self.add_agent("worker")
        profile_dir = self.root / "profile"
        profile_dir.mkdir()
        target = self.root / "credentials-a.json"
        target.write_text("A", encoding="utf-8")
        try:
            os.symlink(str(target), str(profile_dir / "credentials.json"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        registry = self.root / "profiles.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile_dir)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}, clear=False):
            with self.assertRaises(roster.RosterResolutionError):
                self.freeze(profile_overrides={"one": "local"})

    def test_profile_identity_file_bounds_and_non_regular_files_fail_closed(self):
        profile_dir = self.root / "bounded-profile"
        profile_dir.mkdir()
        profile = {"cli": "claude", "env": {"CLAUDE_CONFIG_DIR": str(profile_dir)}}
        path = profile_dir / "credentials.json"
        path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(roster.RosterResolutionError, "2 MiB"):
            roster._profile_content_digest(profile)
        path.unlink()
        path.mkdir()
        with self.assertRaisesRegex(roster.RosterResolutionError, "regular file"):
            roster._profile_content_digest(profile)

    def test_claude_cache_timestamp_does_not_refuse_prepared_invocation(self):
        from _deliberation import TurnContext
        from _deliberation_adapter import FreshDispatchAdapter
        from _deliberation_invocation import build_invocation_plans

        self.add_agent("worker", model="claude-fable-5-1")
        profile_dir = self.root / "cache-timestamp-profile"
        profile_dir.mkdir()
        path = profile_dir / ".claude.json"
        identity = {"oauthAccount": {"accountUuid": "synthetic-account"},
                    "model": "claude-fable-5-1"}
        path.write_text(json.dumps(identity), encoding="utf-8")
        registry = self.root / "cache-timestamp-registry.json"
        registry.write_text(json.dumps({"profiles": {
            "local": {"cli": "claude", "config_dir": str(profile_dir)}
        }}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"SUMMON_PROFILES_FILE": str(registry)}):
            frozen = self.freeze(profile_overrides={"one": "local"})
            plan = build_invocation_plans(
                frozen, decision_id="decision", cwd=str(self.cwd), ballot_only=True)["one"]
            text = "Synthetic choice."
            adapter = FreshDispatchAdapter(
                plan.template, snapshot_digest=plan.snapshot_digest,
                current_snapshot_digest=lambda: plan.snapshot_digest if frozen.revalidate() else "0" * 64,
                owner_is_current=lambda: True, timeout_ms=1000, generation=1,
                invocation_for_context=lambda context: plan.for_context(context, text),
                executor=lambda *a, **kw: self.fail("provider must not be called"))
            # Creation, update with a different raw length, deletion, and key
            # reordering/whitespace all retain the same semantic identity.
            for index, document in enumerate((
                    {**identity, "cachedGrowthBookFeaturesAt": 1},
                    {"cachedGrowthBookFeaturesAt": 1234567890123, **identity},
                    {"model": identity["model"], "oauthAccount": identity["oauthAccount"]})):
                with self.subTest(index=index):
                    path.write_text(json.dumps(document, indent=index + 1), encoding="utf-8")
                    context = TurnContext("decision", "one", f"turn-{index}", index, _sha(text))
                    adapter.prepare(context)
                    self.assertTrue(frozen.revalidate())

    def test_claude_json_preserves_account_config_and_unknown_keys(self):
        profile_dir = self.root / "semantic-profile"
        profile_dir.mkdir()
        path = profile_dir / ".claude.json"
        profile = {"cli": "claude", "env": {"CLAUDE_CONFIG_DIR": str(profile_dir)}}
        original = {"oauthAccount": {"accountUuid": "A"}, "model": "model-a",
                    "settings": {"route": "first-party"}, "unknownField": {"enabled": True}}
        path.write_text(json.dumps(original), encoding="utf-8")
        expected = roster._profile_content_digest(profile)
        for key, replacement in (
                ("oauthAccount", {"accountUuid": "B"}), ("model", "model-b"),
                ("settings", {"route": "other"}), ("unknownField", {"enabled": False}),
                ("futureUnknownField", "new"),
                ("settings", {"cachedGrowthBookFeaturesAt": 123})):
            with self.subTest(key=key, replacement=replacement):
                path.write_text(json.dumps({**original, key: replacement}), encoding="utf-8")
                self.assertNotEqual(expected, roster._profile_content_digest(profile))
        deleted = dict(original)
        del deleted["unknownField"]
        path.write_text(json.dumps(deleted), encoding="utf-8")
        self.assertNotEqual(expected, roster._profile_content_digest(profile))

    def test_claude_json_invalid_or_ambiguous_identity_is_refused(self):
        profile_dir = self.root / "invalid-metadata-profile"
        profile_dir.mkdir()
        path = profile_dir / ".claude.json"
        profile = {"cli": "claude", "env": {"CLAUDE_CONFIG_DIR": str(profile_dir)}}
        for raw in (b"not-json", b"[]", b"null", b"{\"model\":NaN}", b"{\"model\":Infinity}",
                    b"{\"model\":\"A\",\"model\":\"B\"}",
                    b"{\"cachedGrowthBookFeaturesAt\":1,\"cachedGrowthBookFeaturesAt\":2}",
                    b"{\"oauthAccount\":{\"accountUuid\":\"A\",\"accountUuid\":\"B\"}}", b"\xff"):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                with self.assertRaisesRegex(roster.RosterResolutionError, "JSON"):
                    roster._profile_content_digest(profile)

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
        self.add_agent("worker", extra="--secret-path C:\\Users\\test-user\\private")
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

    def test_kimi_environment_revision_is_receipt_evidence(self):
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "one"}, clear=False):
            first = roster._backend_env_digest("kimi")
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "two"}, clear=False):
            second = roster._backend_env_digest("kimi")
        self.assertIsNotNone(first)
        self.assertNotEqual(first, second)

    def test_no_provider_or_worktree_side_effects(self):
        self.add_agent("worker")
        with mock.patch.object(roster.shutil, "which", return_value=None):
            # PATH probing is evidence collection, not a provider side effect;
            # the resolver remains usable when no backend is installed.
            snap = self.freeze()
        self.assertIsNone(snap.seats[0].executable_sha256)


if __name__ == "__main__":
    unittest.main()

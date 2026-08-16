#!/usr/bin/env python3
"""Mutation tests for provider-inert custom-agent package discovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_agents as agents


def manifest(name="reviewer", **changes):
    fields = {
        "schema_version": "1", "name": name,
        "description": "Independent reviewer", "role": "reviewer",
        "model": "sonnet", "cli": "claude", "transport": "subprocess",
        "permission_ceiling": "read-only", "authority_class": "contained",
        "skills": "[code-review, evidence]", "consent_class": "none",
        "worktree_mode": "none",
    }
    fields.update({key: str(value) for key, value in changes.items()})
    body = fields.pop("_body", "Review the supplied evidence independently.")
    return "---\n" + "\n".join(f"{key}: {value}" for key, value in fields.items()) + (
        "\n---\n" + body + "\n")


class CustomAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name="reviewer", *, root=None, text=None):
        base = Path(root) if root is not None else self.workspace / ".agents" / "agents"
        package = base / name
        package.mkdir(parents=True, exist_ok=True)
        path = package / "agent.md"
        path.write_text(text if text is not None else manifest(name), encoding="utf-8")
        return path

    def test_workspace_discovery_returns_frozen_redacted_canonical_agent(self):
        path = self.add()
        found = agents.discover_agents(self.workspace)
        self.assertEqual(tuple(found), ("reviewer",))
        item = found["reviewer"]
        self.assertEqual(item.path, str(path.resolve()))
        public = item.as_dict()
        self.assertNotIn("name", public)
        self.assertNotIn("body", public)
        self.assertNotIn(str(self.workspace), json.dumps(public))
        self.assertEqual(item.canonical_json(), json.dumps(
            public, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False))

    def test_agents_validate_cli_is_provider_inert_and_redacted(self):
        path = self.add()
        result = subprocess.run(
            [sys.executable, str(HERE / "run_subagent.py"), "agents", "validate",
             "--cwd", str(self.workspace), "--json"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["provider_calls"], 0)
        self.assertEqual(report["count"], 1)
        self.assertNotIn(str(path), result.stdout)
        self.assertNotIn("Review the supplied evidence", result.stdout)
        self.assertIn("definition_digest", result.stdout)

    def test_global_root_is_ignored_unless_explicitly_supplied(self):
        global_root = self.root / "global"
        self.add("global-agent", root=global_root)
        self.assertEqual(agents.discover_agents(self.workspace), {})
        found = agents.discover_agents(self.workspace, global_root)
        self.assertIn("global-agent", found)

    def test_workspace_global_name_collision_is_rejected(self):
        global_root = self.root / "global"
        self.add("reviewer")
        self.add("reviewer", root=global_root)
        with self.assertRaises(agents.AgentManifestError):
            agents.discover_agents(self.workspace, global_root)

    def test_missing_unknown_duplicate_and_malformed_fields_fail_closed(self):
        cases = {
            "missing": manifest().replace("role: reviewer\n", ""),
            "unknown": manifest().replace("role: reviewer", "unknown: x\nrole: reviewer"),
            "duplicate": manifest().replace("role: reviewer", "role: reviewer\nrole: auditor"),
            "nested": manifest().replace("role: reviewer", "  role: reviewer"),
            "quote": manifest().replace("description: Independent reviewer",
                                        'description: "unterminated'),
            "unterminated": manifest().replace("\n---\nReview", "\nReview"),
        }
        for label, text in cases.items():
            with self.subTest(label=label):
                path = self.add(label, text=text.replace("name: reviewer", f"name: {label}"))
                with self.assertRaises(agents.AgentManifestError):
                    agents.load_agent(path)

    def test_body_mutation_changes_definition_digest(self):
        path = self.add()
        before = agents.load_agent(path)
        path.write_text(manifest(_body="Changed untrusted body."), encoding="utf-8")
        after = agents.load_agent(path)
        self.assertNotEqual(before.definition_digest, after.definition_digest)
        self.assertNotEqual(before.source_digest, after.source_digest)

    def test_authority_directives_in_body_are_refused(self):
        for directive in ("permission: yolo", "run-agent: evil", "executable: /tmp/x",
                          "args: --danger", "transport: acp"):
            with self.subTest(directive=directive):
                path = self.add(text=manifest(_body=directive))
                with self.assertRaises(agents.AgentManifestError):
                    agents.load_agent(path)

    def test_permission_escalation_is_clamped_and_invalid_tiers_refused(self):
        self.assertEqual(agents.clamp_permission("read-only", "yolo"), "read-only")
        self.assertEqual(agents.clamp_permission("yolo", "safe-edit"), "safe-edit")
        self.assertEqual(agents.clamp_permission("safe-edit", "read-only"), "read-only")
        for parent, declared in (("root", "read-only"), ("yolo", "admin")):
            with self.assertRaises(agents.AgentManifestError):
                agents.clamp_permission(parent, declared)

    def test_unsafe_transport_consent_worktree_and_cli_are_refused(self):
        cases = (
            {"transport": "acp"}, {"transport": "api"}, {"cli": "shell"},
            {"consent_class": "full-authority"}, {"worktree_mode": "required"},
            {"authority_class": "full-bypass"},
            {"authority_class": "workspace-write", "worktree_mode": "none"},
        )
        for index, changes in enumerate(cases):
            with self.subTest(changes=changes):
                name = f"case-{index}"
                path = self.add(name, text=manifest(name, **changes))
                with self.assertRaises(agents.AgentManifestError):
                    agents.load_agent(path)

    def test_allowlisted_authority_combinations_load(self):
        writable = self.add("writer", text=manifest(
            "writer", authority_class="workspace-write", worktree_mode="required"))
        bypass = self.add("operator", text=manifest(
            "operator", permission_ceiling="yolo", authority_class="full-bypass",
            consent_class="full-authority", worktree_mode="required"))
        self.assertEqual(agents.load_agent(writable).authority_class, "workspace-write")
        self.assertEqual(agents.load_agent(bypass).consent_class, "full-authority")

    def test_unsafe_ids_and_name_slug_mismatch_are_refused(self):
        bad_names = ("../escape", "UPPER", "has space", "_hidden")
        for value in bad_names:
            with self.subTest(value=value):
                path = self.add("safe-name", text=manifest(value))
                with self.assertRaises(agents.AgentManifestError):
                    agents.load_agent(path)
        path = self.add("safe-name", text=manifest("other-name"))
        with self.assertRaises(agents.AgentManifestError):
            agents.load_agent(path)
        with self.assertRaises(agents.AgentManifestError):
            agents.resolve_agent("../escape", self.workspace)

    def test_file_and_frontmatter_size_bounds(self):
        oversized = self.add(text=manifest(_body="x" * agents.MAX_AGENT_BYTES))
        with self.assertRaises(agents.AgentManifestError):
            agents.load_agent(oversized)
        lines = "\n".join(f"# line {index}" for index in range(
            agents.MAX_FRONTMATTER_LINES + 1))
        path = self.add(text=manifest().replace("schema_version: 1", lines + "\nschema_version: 1"))
        with self.assertRaises(agents.AgentManifestError):
            agents.load_agent(path)

    def test_symlink_package_or_file_is_refused(self):
        target = self.add("target")
        link_root = self.workspace / ".agents" / "agents" / "linked"
        try:
            link_root.symlink_to(target.parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(agents.AgentManifestError):
            agents.discover_agents(self.workspace)

    def test_windows_junction_or_reparse_package_is_refused(self):
        self.add("escape")
        junction = self.workspace / ".agents" / "agents" / "escape"
        isjunction = getattr(agents.os.path, "isjunction", None)
        if isjunction is None:
            self.skipTest("junction inspection is unavailable")
        with mock.patch.object(agents.os.path, "isjunction",
                               side_effect=lambda value: Path(value).name == "escape"):
            with self.assertRaises(agents.AgentManifestError):
                agents.discover_agents(self.workspace)

    def test_direct_agent_file_symlink_is_refused(self):
        target = self.add("target")
        package = self.workspace / ".agents" / "agents" / "linked-file"
        package.mkdir()
        try:
            (package / "agent.md").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(agents.AgentManifestError):
            agents.load_agent(package / "agent.md")

    def test_invalid_input_has_no_filesystem_side_effects(self):
        path = self.add(text=manifest().replace("cli: claude", "cli: shell"))
        before = sorted(str(item.relative_to(self.workspace))
                        for item in self.workspace.rglob("*"))
        with self.assertRaises(agents.AgentManifestError):
            agents.load_agent(path)
        after = sorted(str(item.relative_to(self.workspace))
                       for item in self.workspace.rglob("*"))
        self.assertEqual(before, after)

    def test_resolve_agent_and_mapping_are_immutable(self):
        self.add()
        item = agents.resolve_agent("reviewer", self.workspace)
        self.assertEqual(item.name, "reviewer")
        found = agents.discover_agents(self.workspace)
        with self.assertRaises(TypeError):
            found["other"] = item
        with self.assertRaises(agents.AgentManifestError):
            agents.resolve_agent("missing", self.workspace)

    def test_module_has_no_provider_process_or_network_imports(self):
        source = Path(agents.__file__).read_text(encoding="utf-8")
        tree = __import__("ast").parse(source)
        imports = {alias.name for node in __import__("ast").walk(tree)
                   if isinstance(node, (__import__("ast").Import,
                                        __import__("ast").ImportFrom))
                   for alias in node.names}
        self.assertTrue({"_executor", "subprocess", "socket", "urllib", "requests"}
                        .isdisjoint(imports))


if __name__ == "__main__":
    unittest.main(verbosity=2)

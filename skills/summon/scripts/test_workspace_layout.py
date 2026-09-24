"""Provider-inert layout compatibility tests; all writes use private temp roots."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import _rundir as rd
import _swarm_coordinator as swarm
import _workspace_layout as layout
from _fleet_approval import _secure_file, _secure_private_root

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
HISTORICAL_REVISION = "c4003dca3ed8cf26aac96c68d6c40c83a50511fd"
PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class WorkspaceLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-layout-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runs"
        _secure_private_root(str(self.root))
        self.run_id = "workspace-test"

    def snapshot(self, root):
        return {str(p.relative_to(root)): None if p.is_dir() else p.read_bytes()
                for p in root.rglob("*")}

    def create(self):
        return layout.create_workspace_layout(self.root, self.run_id)

    def prepare_inner(self, result):
        return swarm.SwarmCoordinator.create(
            result.inner_runs_root, result.run_id,
            project_root_sha256="1" * 64, roster_definition_sha256="2" * 64,
            tasks=[{"task_id": "main-1", "request_sha256": "3" * 64}],
        )

    def test_completed_guard_precedes_publication(self):
        original = layout._publish_new

        def inspected(source, destination):
            self.assertFalse(Path(destination).exists())
            with self.assertRaises(ValueError):
                rd.validate_run_id(Path(source).name)
            checked = layout._validate_outer(source, self.run_id)
            self.assertTrue(Path(checked.inner_run_directory).is_dir())
            self.assertTrue((Path(source) / layout.GUARD).is_dir())
            original(source, destination)

        with patch.object(layout, "_publish_new", side_effect=inspected):
            result = self.create()
        self.assertEqual(layout.resolve_workspace_layout(self.root, self.run_id), result)
        self.assertNotIn(str(self.root), repr(result))
        self.assertFalse(any(p.name.startswith(".workspace-stage-") for p in self.root.iterdir()))

    def test_old_normal_mutation_refuses_before_acquire_or_repair_then_new_inner_works(self):
        result = self.create()
        inner = self.prepare_inner(result)
        journal = next(Path(result.inner_run_directory).glob("journal-g*.jsonl"))
        with journal.open("ab") as stream:
            stream.write(b'{"torn":')
        before = self.snapshot(self.root)
        old = swarm.SwarmCoordinator(self.root, self.run_id)
        with patch.object(swarm, "acquire_owner", side_effect=AssertionError("old acquired")) as acquire:
            with patch.object(swarm, "journal_repair", side_effect=AssertionError("old repaired")) as repair:
                with self.assertRaises(swarm.SwarmCoordinatorError):
                    old.close()
        acquire.assert_not_called()
        repair.assert_not_called()
        self.assertEqual(self.snapshot(self.root), before)
        resolved = layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(resolved, result)
        # The new resolver authorizes no mutation itself. The existing sole
        # inner writer still owns acquisition and predecessor repair.
        with inner._mutation() as (_owner, state, torn):
            self.assertIn("main-1", state["tasks"])
            self.assertFalse(torn)
        self.assertFalse(rd.journal_read(result.inner_run_directory)[1])

    def test_historical_coordinator_refuses_published_address_before_mutation(self):
        """The released coordinator must hit the outer generation fence first.

        This deliberately imports the coordinator and its two direct protocol
        dependencies from the published 3.4.0 tree in a child process.  Using
        the current module here would only prove that today's implementation
        still has the guard; the compatibility promise is about an older
        ordinary swarm writer targeting the newly published address.
        """
        git = shutil.which("git")
        self.assertIsNotNone(git, "historical compatibility requires Git")
        result = self.create()
        inner = self.prepare_inner(result)
        journal = next(Path(result.inner_run_directory).glob("journal-g*.jsonl"))
        with journal.open("ab") as stream:
            stream.write(b'{"historical_torn":')
        before = self.snapshot(self.root)

        historical = Path(self.temp.name) / "historical-swarm"
        historical.mkdir()
        revision = subprocess.run(
            [git, "rev-parse", "--verify", f"{HISTORICAL_REVISION}^{{commit}}"],
            cwd=REPO_ROOT, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=PROCESS_FLAGS,
            text=True).stdout.strip()
        self.assertEqual(revision, HISTORICAL_REVISION)
        plugin = json.loads(subprocess.run(
            [git, "show", f"{revision}:plugin.json"], cwd=REPO_ROOT,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=PROCESS_FLAGS, text=True).stdout)
        self.assertEqual(plugin.get("version"), "3.4.0")
        historical_paths = subprocess.run(
            [git, "ls-tree", "-r", "--name-only", revision, "skills/summon/scripts"],
            cwd=REPO_ROOT, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, creationflags=PROCESS_FLAGS,
            text=True).stdout.splitlines()
        self.assertFalse([path for path in historical_paths
                          if Path(path).name.startswith("_workspace")])
        for relative in (
                "skills/summon/scripts/_swarm_coordinator.py",
                "skills/summon/scripts/_rundir.py",
                "skills/summon/scripts/_swarm_protocol.py"):
            raw = subprocess.run(
                [git, "show", f"{revision}:{relative}"], cwd=REPO_ROOT,
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=PROCESS_FLAGS).stdout
            (historical / Path(relative).name).write_bytes(raw)
        probe = "\n".join((
            "import sys",
            "sys.path.insert(0, sys.argv[3])",
            "import _swarm_coordinator as swarm",
            "try:",
            "    swarm.SwarmCoordinator(sys.argv[1], sys.argv[2]).close()",
            "except Exception as exc:",
            "    print(type(exc).__name__ + ':' + str(exc))",
            "    sys.exit(0)",
            "print('accepted')",
            "sys.exit(2)",
        ))
        attempted = subprocess.run(
            [sys.executable, "-I", "-B", "-c", probe, str(self.root), self.run_id,
             str(historical)], cwd=historical, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=PROCESS_FLAGS)
        self.assertEqual(attempted.returncode, 0,
                         attempted.stdout + attempted.stderr)
        self.assertIn("SwarmCoordinatorError", attempted.stdout)
        self.assertIn("generation.txt", attempted.stdout)
        self.assertIn("not a file", attempted.stdout)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse((Path(result.outer_directory) / "owner.lock").exists())

        resolved = layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(resolved, result)
        current = swarm.SwarmCoordinator(result.inner_runs_root, result.run_id)
        with current._mutation() as (_owner, state, torn):
            self.assertIn("main-1", state["tasks"])
            self.assertFalse(torn)
        self.assertFalse(rd.journal_read(result.inner_run_directory)[1])

    def test_outer_valid_prefix_torn_tail_is_untouched_by_old_command(self):
        result = self.create()
        self.prepare_inner(result)
        source = next(Path(result.inner_run_directory).glob("journal-g*.jsonl"))
        outer = Path(result.outer_directory) / source.name
        outer.write_bytes(source.read_bytes() + b'{"torn":')
        _secure_file(str(outer))
        before = self.snapshot(self.root)
        with patch.object(swarm, "acquire_owner", side_effect=AssertionError("old acquired")):
            with patch.object(swarm, "journal_repair", side_effect=AssertionError("old repaired")):
                with self.assertRaises(swarm.SwarmCoordinatorError):
                    swarm.SwarmCoordinator(self.root, self.run_id).close()
        self.assertEqual(self.snapshot(self.root), before)
        # Foreign outer history is not silently adopted by the new resolver.
        with self.assertRaises(layout.WorkspaceLayoutError) as caught:
            layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(caught.exception.kind, "incomplete_or_foreign_layout")

    def test_old_create_cannot_reinitialize_published_workspace(self):
        self.create()
        before = self.snapshot(self.root)
        with patch.object(swarm, "acquire_owner", side_effect=AssertionError("old acquired")):
            with self.assertRaises(swarm.SwarmCoordinatorError):
                swarm.SwarmCoordinator.create(
                    self.root, self.run_id, project_root_sha256="1" * 64,
                    roster_definition_sha256="2" * 64,
                    tasks=[{"task_id": "main-1", "request_sha256": "3" * 64}],
                )
        self.assertEqual(self.snapshot(self.root), before)

    def test_existing_v1_history_and_empty_destination_are_never_adopted(self):
        existing = self.root / self.run_id
        existing.mkdir()
        before = self.snapshot(self.root)
        with self.assertRaises(layout.WorkspaceLayoutError) as caught:
            self.create()
        self.assertEqual(caught.exception.kind, "run_exists")
        self.assertEqual(self.snapshot(self.root), before)
        marker = existing / "journal-g1.jsonl"
        marker.write_bytes(b"legacy bytes including incomplete tail")
        before = self.snapshot(self.root)
        with self.assertRaises(layout.WorkspaceLayoutError):
            self.create()
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(self.snapshot(self.root), before)

    def test_publication_never_replaces_late_empty_destination(self):
        original = layout._publish_new

        def competing_destination(source, destination):
            Path(destination).mkdir()
            self.destination_id = os.stat(destination).st_ino
            original(source, destination)

        with patch.object(layout, "_publish_new", side_effect=competing_destination):
            with self.assertRaises(layout.WorkspaceLayoutError):
                self.create()
        destination = self.root / self.run_id
        self.assertEqual(os.stat(destination).st_ino, self.destination_id)
        self.assertEqual(list(destination.iterdir()), [])

    def test_windows_publication_retries_transient_permission_without_replacement(self):
        if os.name != "nt":
            self.skipTest("Windows publication retry contract")
        source = self.root / ".staged-source"
        destination = self.root / "published"
        source.mkdir()
        attempts = []
        original = layout.os.rename

        def transient(source_name, destination_name):
            attempts.append((source_name, destination_name))
            if len(attempts) == 1:
                raise PermissionError(13, "transient host inspection")
            return original(source_name, destination_name)

        with patch.object(layout.os, "rename", side_effect=transient):
            layout._publish_new(str(source), str(destination))
        self.assertEqual(len(attempts), 2)
        self.assertTrue(destination.is_dir())
        self.assertFalse(source.exists())

    def test_unknown_format_and_changed_binding_refuse_without_mutation(self):
        result = self.create()
        manifest = Path(result.outer_directory) / layout.MANIFEST
        for value, kind in (
            ({"format": "future/v999"}, "unsupported_format"),
            ({"format": layout.FORMAT, "run_id": "other", "inner_namespace": layout.GUARD},
             "manifest_binding_mismatch"),
        ):
            manifest.write_text(json.dumps(value), encoding="utf-8")
            before = self.snapshot(self.root)
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                layout.resolve_workspace_layout(self.root, self.run_id)
            self.assertEqual(caught.exception.kind, kind)
            self.assertEqual(self.snapshot(self.root), before)

    def test_foreign_entry_and_invalid_root_refuse(self):
        result = self.create()
        extra = Path(result.outer_directory) / "unexpected"
        extra.write_bytes(b"foreign")
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(self.root, self.run_id)
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.create_workspace_layout(self.root, "../outside")
        with patch.object(layout, "_verify_private", side_effect=ValueError("PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))

    def test_missing_guard_and_foreign_inner_namespace_refuse(self):
        result = self.create()
        guard = Path(result.inner_runs_root)
        moved = self.root / "private-held-guard"
        guard.rename(moved)
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(self.root, self.run_id)
        moved.rename(guard)
        (guard / "unexpected").mkdir()
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(self.root, self.run_id)

    def test_actual_symlink_root_is_refused(self):
        self.create()
        linked = Path(self.temp.name) / "linked-root"
        try:
            os.symlink(self.root, linked, target_is_directory=True)
        except OSError:
            self.skipTest("host cannot create a symlink fixture")
        before = self.snapshot(self.root)
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(linked, self.run_id)
        self.assertEqual(self.snapshot(self.root), before)

    def test_actual_manifest_hardlink_is_refused(self):
        result = self.create()
        manifest = Path(result.outer_directory) / layout.MANIFEST
        outside = Path(self.temp.name) / "manifest-copy"
        outside.write_bytes(manifest.read_bytes())
        _secure_file(str(outside))
        manifest.unlink()
        os.link(outside, manifest)
        before = self.snapshot(self.root)
        with self.assertRaises(layout.WorkspaceLayoutError):
            layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(self.snapshot(self.root), before)

    def test_observed_reparse_and_unverifiable_handle_refuse(self):
        self.create()
        with patch.object(layout, "_reject_reparse_ancestors", side_effect=ValueError("reparse")):
            with self.assertRaises(layout.WorkspaceLayoutError):
                layout.resolve_workspace_layout(self.root, self.run_id)
        with patch.object(layout, "_final_open_path", return_value=None):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                layout.resolve_workspace_layout(self.root, self.run_id)
        self.assertEqual(caught.exception.kind, "manifest_changed")

    def assert_safe_failure(self, caught, *, subreason, publication):
        self.assertEqual(caught.exception.subreason, subreason)
        self.assertEqual(caught.exception.publication, publication)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertNotRegex(str(caught.exception), r"(?:errno|Errno|\b(?:13|22|28|32|11)\b)")

    def test_staging_failure_is_categorized_without_publishing(self):
        with patch.object(layout.tempfile, "mkdtemp",
                          side_effect=OSError(layout.errno.EAGAIN, "PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assert_safe_failure(caught, subreason="staging_host_load_sensitive",
                                 publication="not_published")
        self.assertFalse((self.root / self.run_id).exists())

    def test_workspace_error_at_acl_is_categorized_without_publishing(self):
        with patch.object(layout, "_secure_private_root",
                          side_effect=layout.WorkspaceLayoutError("acl_refused")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assert_safe_failure(caught, subreason="acl_refused",
                                 publication="not_published")
        self.assertFalse((self.root / self.run_id).exists())

    def test_manifest_failure_is_categorized_without_publishing(self):
        with patch.object(layout, "_manifest", side_effect=ValueError("PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assert_safe_failure(caught, subreason="manifest_invalid",
                                 publication="not_published")
        self.assertFalse((self.root / self.run_id).exists())

    def test_validation_failure_is_categorized_without_publishing(self):
        with patch.object(layout, "_validate_outer",
                          side_effect=OSError(layout.errno.EINVAL, "PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assert_safe_failure(caught, subreason="validate_invalid",
                                 publication="not_published")
        self.assertFalse((self.root / self.run_id).exists())

    def test_manifest_fsync_failure_leaves_no_published_run(self):
        with patch.object(layout.os, "fsync", side_effect=OSError("PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assertEqual(caught.exception.publication, "not_published")
        self.assertEqual(caught.exception.subreason, "fsync_os_error")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertFalse((self.root / self.run_id).exists())
        self.assertTrue(any(p.name.startswith(".workspace-stage-") for p in self.root.iterdir()))

    def test_publish_then_failed_return_reports_unknown_without_retry(self):
        original = layout._publish_new

        def publish_then_fail(source, destination):
            original(source, destination)
            raise OSError("PRIVATE")

        with patch.object(layout, "_publish_new", side_effect=publish_then_fail) as publish:
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assertEqual(caught.exception.publication, "unknown")
        self.assertEqual(caught.exception.subreason, "publish_os_error")
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(layout.resolve_workspace_layout(self.root, self.run_id).run_id, self.run_id)

    def test_resolve_failure_after_publication_is_unknown_and_safe(self):
        original = layout.resolve_workspace_layout

        with patch.object(layout, "resolve_workspace_layout",
                          side_effect=OSError(layout.errno.EAGAIN, "PRIVATE")):
            with self.assertRaises(layout.WorkspaceLayoutError) as caught:
                self.create()
        self.assertEqual(caught.exception.kind, "published_validation_failed")
        self.assert_safe_failure(caught, subreason="resolve_host_load_sensitive",
                                 publication="unknown")
        with patch.object(layout, "resolve_workspace_layout", original):
            self.assertEqual(layout.resolve_workspace_layout(self.root, self.run_id).run_id,
                             self.run_id)


if __name__ == "__main__":
    unittest.main()

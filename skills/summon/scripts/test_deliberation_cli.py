"""Focused storage and CLI tests for the deliberation command surface."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _cli
import _deliberation_store as store
import _rundir


SCRIPT = HERE / "run_subagent.py"


def _receipt(run_id: str) -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "decision_id": "decision-1", "question_sha256": hashlib.sha256(b"q").hexdigest(),
        "seat_ids": ["a", "b"], "option_ids": ["yes", "no"],
        "created_at": 1.0,
    }


class DeliberationCliTests(unittest.TestCase):
    def test_git_style_rewrites_are_explicit(self) -> None:
        self.assertEqual(
            _cli.rewrite_subcommand(["deliberate", "--question", "q"]),
            (["--deliberate", "--question", "q"], None))
        for action in ("resume", "status", "replay", "cancel"):
            rewritten, mode = _cli.rewrite_subcommand(
                ["deliberate", action, "run-1", "--json"])
            self.assertEqual(mode, None)
            self.assertEqual(rewritten,
                             [f"--deliberate-{action}", "run-1", "--json"])
            _, missing = _cli.rewrite_subcommand(["deliberate", action])
            self.assertIn("needs a run id", missing)

    def test_operation_matrix_rejects_silent_flag_drops(self) -> None:
        parser = _cli.build_parser("test", 1)
        argv = ["--deliberate-status", "run-1", "--agent", "reviewer"]
        args = parser.parse_args(argv)
        message = _cli.unsupported_mode_flags(argv, args)
        self.assertIn("--agent", message)
        self.assertIn("deliberate status", message)

        fresh = ["--deliberate", "--question", "q", "--seats", "a,b",
                 "--options", "yes,no", "--max-attempts", "2", "--deadline", "30s"]
        self.assertIsNone(_cli.unsupported_mode_flags(
            fresh, parser.parse_args(fresh)))

    def test_quorum_parser_preserves_council_integer_and_deliberation_fraction(self) -> None:
        parser = _cli.build_parser("test", 1)
        self.assertEqual(parser.parse_args(["--council", "--quorum", "2"]).quorum, 2)
        self.assertEqual(parser.parse_args(
            ["--deliberate", "--quorum", "2/3"]).quorum, "2/3")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--deliberate", "--quorum", "0/3"])

    def test_fresh_command_is_structurally_blocked_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            run_root = os.path.join(temp, "runs")
            command = [
                sys.executable, str(SCRIPT), "deliberate", "--question", "q",
                "--seats", "a,b", "--options", "yes,no", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", temp, "--run-dir", run_root,
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(body["status"], "blocked")
            self.assertEqual(body["error_kind"], "integration_pending")
            self.assertIn("no provider was called", body["error"])
            self.assertFalse(os.path.exists(run_root))

    def test_consents_are_bound_to_known_non_overlapping_seats(self) -> None:
        parser = _cli.build_parser("test", 1)
        base = ["--deliberate", "--question", "q", "--seats", "a,b",
                "--options", "yes,no", "--max-attempts", "2", "--deadline", "30s"]
        for extra, expected in ((["--text-only-consent", "c"], "unknown seat"),
                                (["--text-only-consent", "a",
                                  "--full-authority-consent", "a"], "both")):
            args = parser.parse_args(base + extra)
            with self.assertRaises(ValueError) as caught:
                store._validate_fresh_args(args)
            self.assertIn(expected, str(caught.exception))

    def test_initialize_status_and_replay_are_journal_derived_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-1"))
            _rundir.release_owner(owner)
            before = {name: Path(path, name).read_bytes()
                      for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))}
            status = store.inspect_run(root, "run-1")
            replay = store.replay_run(root, "run-1")
            after = {name: Path(path, name).read_bytes()
                     for name in os.listdir(path) if os.path.isfile(os.path.join(path, name))}
            self.assertEqual(status["projection"]["state"], "prepared")
            self.assertEqual(status["journal_records"], 1)
            self.assertTrue(status["consistent"])
            self.assertEqual(replay["records"][0]["event"], "run_prepared")
            self.assertEqual(before, after)

    def test_cancel_is_exclusive_and_only_claims_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-2"))
            _rundir.release_owner(owner)
            queued = store.queue_cancel(root, "run-2", "operator-1")
            self.assertEqual(queued["command_status"], "queued")
            self.assertFalse(queued["applied"])
            command = json.loads(Path(path, "commands", "operator-1.json").read_text())
            self.assertEqual(command["action"], "cancel")
            self.assertEqual(command["actor"], "human")
            with self.assertRaises(store.DeliberationStoreError):
                store.queue_cancel(root, "run-2", "operator-1")

    def test_cancel_inbox_bound_is_enforced_under_enqueue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            _path, owner = store.initialize_run(root, _receipt("run-bound"))
            _rundir.release_owner(owner)
            original = store.MAX_PENDING_COMMANDS
            try:
                store.MAX_PENDING_COMMANDS = 1
                store.queue_cancel(root, "run-bound", "first")
                with self.assertRaises(store.DeliberationStoreError) as caught:
                    store.queue_cancel(root, "run-bound", "second")
                self.assertIn("inbox is full", str(caught.exception))
            finally:
                store.MAX_PENDING_COMMANDS = original

    def test_resume_requires_explicit_uncertain_spend_consent_then_stays_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = os.path.join(temp, "runs")
            root = os.path.join(base, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-3"))
            _rundir.journal_append(path, {
                "event": "attempt_started", "schema_version": 1,
                "generation": owner.generation, "attempt_id": "attempt-1",
                "launch_spec_sha256": "a" * 64,
            }, owner=owner)
            _rundir.release_owner(owner)
            common = [sys.executable, str(SCRIPT), "deliberate", "resume", "run-3",
                      "--run-dir", base, "--cwd", temp]
            first = subprocess.run(common, capture_output=True, text=True, timeout=20)
            first_body = json.loads(first.stdout)
            self.assertEqual(first_body["status"], "blocked")
            self.assertEqual(first_body["error_kind"], "uncertain_spend")
            second = subprocess.run(common + ["--retry-indeterminate"],
                                    capture_output=True, text=True, timeout=20)
            second_body = json.loads(second.stdout)
            self.assertEqual(second_body["status"], "blocked")
            self.assertEqual(second_body["error_kind"], "integration_pending")

    def test_path_traversal_and_foreign_receipts_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(store.DeliberationStoreError):
                store.inspect_run(temp, "../outside")
            path = os.path.join(temp, "run-4")
            os.makedirs(path)
            _rundir.atomic_write_json(os.path.join(path, "receipt.json"), {
                "mode": "council", "schema_version": 1, "run_id": "run-4"})
            with self.assertRaises(store.DeliberationStoreError):
                store.inspect_run(temp, "run-4")

    def test_receipt_mutation_is_not_reported_as_a_consistent_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-5"))
            _rundir.release_owner(owner)
            tampered = _receipt("run-5")
            tampered["seat_ids"] = ["attacker"]
            _rundir.atomic_write_json(os.path.join(path, "receipt.json"), tampered)
            with self.assertRaises(store.DeliberationStoreError):
                store.inspect_run(root, "run-5")

    def test_status_generation_change_during_read_is_inconsistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-6"))
            _rundir.release_owner(owner)
            # A takeover can acquire and release between the reader's two
            # generation observations.  A lock snapshot alone cannot see that
            # ABA; generation evidence must still make the view untrusted.
            with mock.patch.object(
                    store._rundir, "_last_generation",
                    side_effect=[1, 2, 3, 4]):
                status = store.inspect_run(root, "run-6")
            self.assertFalse(status["consistent"])


if __name__ == "__main__":
    unittest.main()

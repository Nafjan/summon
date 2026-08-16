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
        "quorum_rule": "all", "max_attempts": 4,
        "require_human_approval": False,
        "rounds": 1, "deadline_unix_ms": 4_000_000_000_000,
        "created_at": 1.0,
    }


class DeliberationCliTests(unittest.TestCase):
    def test_git_style_rewrites_are_explicit(self) -> None:
        self.assertEqual(
            _cli.rewrite_subcommand(["deliberate", "--question", "q"]),
            (["--deliberate", "--question", "q"], None))
        for action in ("resume", "status", "replay", "cancel", "recover", "open"):
            rewritten, mode = _cli.rewrite_subcommand(
                ["deliberate", action, "run-1", "--json"])
            self.assertEqual(mode, None)
            self.assertEqual(rewritten,
                             [f"--deliberate-{action}", "run-1", "--json"])
            _, missing = _cli.rewrite_subcommand(["deliberate", action])
            self.assertIn("needs a run id", missing)

        for action, extra in (("recover", ["--chat-confirm"]),
                              ("fork", ["--message", "new context", "--chat-reason", "manual"])):
            rewritten, mode = _cli.rewrite_subcommand(
                ["chat", action, "room-1", "worker", *extra])
            self.assertIsNone(mode)
            self.assertEqual(
                rewritten,
                ["--chat-action", action, "--chat-session", "room-1",
                 "--chat-participant", "worker", *extra])

    def test_chat_recovery_and_fork_flags_are_not_silently_dropped(self) -> None:
        parser = _cli.build_parser("test", 1)
        for argv in (
            ["--chat-action", "recover", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-confirm"],
            ["--chat-action", "fork", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-message", "next",
             "--chat-reason", "manual"],
        ):
            args = parser.parse_args(argv)
            self.assertIsNone(_cli.unsupported_mode_flags(argv, args))

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

        opened = ["--deliberate-open", "run-1", "--browser", "link"]
        self.assertIsNone(_cli.unsupported_mode_flags(
            opened, parser.parse_args(opened)))

        recover = ["--deliberate-recover", "run-1", "--agent", "reviewer"]
        args = parser.parse_args(recover)
        message = _cli.unsupported_mode_flags(recover, args)
        self.assertIn("--agent", message)
        self.assertIn("deliberate recover", message)

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

    def test_recover_subcommand_reconciles_sealed_batch_without_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = os.path.join(temp, "runs")
            root = os.path.join(base, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-recover"))
            command = {"command_id": "cmd-1", "sequence": 1, "action": "cancel"}
            typed = (store._replay.ReplayCommand(**command),)
            digest = store._replay.command_batch_sha256(typed)
            _rundir.journal_append(path, {
                "event": "human_command_batch", "schema_version": 1,
                "generation": owner.generation, "batch_id": "batch-" + digest[:32],
                "source_generation": owner.generation, "commands": [command],
                "command_batch_sha256": digest,
            }, owner=owner)
            _rundir.release_owner(owner)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "deliberate", "recover", "run-recover",
                 "--run-dir", base, "--cwd", temp, "--json"],
                capture_output=True, text=True, timeout=20)
            body = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(body["status"], "recovered")
            self.assertEqual(body["decision_status"], "cancelled")
            self.assertEqual(body["provider_calls"], 0)

    def test_management_commands_do_not_echo_path_like_invalid_ids(self) -> None:
        parser = _cli.build_parser("test", 1)
        for flag in ("--deliberate-status", "--deliberate-replay",
                     "--deliberate-cancel", "--deliberate-resume"):
            args = parser.parse_args([flag, r"C:\Users\nside\private-project"])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = store.run_command(args)
            self.assertEqual(code, 1)
            body = json.loads(output.getvalue())
            self.assertEqual(body["error_kind"], "validation")
            self.assertNotIn("private-project", output.getvalue())

    def test_management_unknown_run_redacts_absolute_runs_root(self) -> None:
        parser = _cli.build_parser("test", 1)
        args = parser.parse_args(["--deliberate-status", "run-missing",
                                  "--run-dir", r"C:\Users\nside\private-project"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = store.run_command(args)
        self.assertEqual(code, 1)
        self.assertNotIn("private-project", output.getvalue())

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
            self.assertEqual(status["receipt"]["quorum_rule"], "all")
            self.assertEqual(status["receipt"]["max_attempts"], 4)
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
                "event": "state_transition", "schema_version": 1,
                "generation": owner.generation, "from": "PREPARED",
                "to": "RUNNING", "reason": "started",
            }, owner=owner)
            _rundir.journal_append(path, {
                "event": "turn_prepared", "schema_version": 1,
                "generation": owner.generation, "decision_id": "decision-1",
                "seat_id": "a", "turn_id": "turn-a-0", "turn_ordinal": 0,
                "request_digest": "a" * 64,
            }, owner=owner)
            _rundir.journal_append(path, {
                "event": "attempt_started", "schema_version": 1,
                "generation": owner.generation, "attempt_id": "attempt-1",
                "decision_id": "decision-1", "seat_id": "a",
                "turn_id": "turn-a-0", "turn_ordinal": 0,
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

    def test_status_and_replay_ignore_tampered_projection_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-cache"))
            _rundir.release_owner(owner)
            _rundir.atomic_write_json(os.path.join(path, "state.json"), {
                "state": "decided", "decision_option": "no",
                "uncertain_spend": False,
            })
            status = store.inspect_run(root, "run-cache")
            replayed = store.replay_run(root, "run-cache")
            self.assertEqual(status["projection"]["state"], "prepared")
            self.assertEqual(replayed["projection"]["state"], "prepared")
            self.assertNotEqual(status["projection"]["decision_option"], "no")

    def test_status_rejects_missing_canonical_replay_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            value = _receipt("run-missing")
            value.pop("quorum_rule")
            with self.assertRaises(store.DeliberationStoreError):
                store.initialize_run(root, value)
            self.assertFalse(os.path.exists(store.run_dir(root, "run-missing")))

    def test_initialize_rejects_malformed_policy_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            for field, invalid in (("quorum_rule", None),
                                   ("seat_ids", "a,b"),
                                   ("max_attempts", 0),
                                   ("require_human_approval", "false")):
                value = _receipt("run-invalid-" + field.replace("_", "-"))
                value[field] = invalid
                with self.subTest(field=field), self.assertRaises(
                        (store.DeliberationStoreError, ValueError)):
                    store.initialize_run(root, value)
                self.assertFalse(os.path.exists(
                    store.run_dir(root, value["run_id"])))

    def test_replay_exposes_checkpoint_seal_not_projection_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-seal"))
            _rundir.release_owner(owner)
            replayed = store.replay_run(root, "run-seal")
            self.assertRegex(replayed["checkpoint_digest"], r"^[0-9a-f]{64}$")
            self.assertEqual(replayed["checkpoint_digest"],
                             replayed["projection"]["replay_digest"])

    def test_replay_records_are_allowlisted_and_private_paths_are_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-redact"))
            _rundir.journal_append(path, {
                "event": "state_transition", "schema_version": 1,
                "generation": owner.generation, "from": "PREPARED",
                "to": "CANCELLED", "reason": "cancelled",
                "decision_option": None,
            }, owner=owner)
            _rundir.journal_append(path, {
                "event": "cleanup_receipt", "schema_version": 1,
                "generation": owner.generation, "verified": False, "clean": False,
                "retained_resources": [r"C:\Users\nside\private-project"],
                "secret": "TOKEN-DO-NOT-EXPORT", "argv": ["--password", "TOKEN"],
            }, owner=owner)
            _rundir.journal_append(path, {
                "event": "advisory_left_behind", "schema_version": 1,
                "generation": owner.generation,
                "items": [r"C:\Users\nside\private-project", "TOKEN=secret"],
                "source": "model_output",
            }, owner=owner)
            _rundir.release_owner(owner)
            output = json.dumps(store.replay_run(root, "run-redact"))
            self.assertNotIn("private-project", output)
            self.assertNotIn("TOKEN", output)
            self.assertNotIn("argv", output)
            self.assertNotIn("run_dir", output)

    def test_torn_tail_is_visible_and_not_reported_as_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-torn"))
            _rundir.release_owner(owner)
            with open(_rundir._journal_path(path, 1), "ab") as handle:
                handle.write(b"{torn")
            status = store.inspect_run(root, "run-torn")
            replayed = store.replay_run(root, "run-torn")
            self.assertTrue(status["journal_torn_tail"])
            self.assertTrue(status["recovery_required"])
            self.assertFalse(status["consistent"])
            self.assertEqual(status["status"], "blocked")
            self.assertTrue(replayed["recovery_required"])
            self.assertFalse(replayed["consistent"])

    def test_empty_newer_journal_segment_does_not_hide_torn_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-empty-segment"))
            _rundir.release_owner(owner)
            with open(_rundir._journal_path(path, 1), "ab") as handle:
                handle.write(b"{torn")
            Path(_rundir._journal_path(path, 2)).touch()
            successor = _rundir.acquire_owner(path, 600)
            self.assertTrue(_rundir.journal_repair(path, successor))
            _rundir.release_owner(successor)
            self.assertTrue(store.inspect_run(root, "run-empty-segment")["consistent"])


if __name__ == "__main__":
    unittest.main()

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
import threading
import time
import unittest
from unittest import mock
from pathlib import Path


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _cli
import _deliberation_context as context
import _deliberation_store as store
import _rundir


SCRIPT = HERE / "run_subagent.py"


def _install_fake_claude_on_path(root: Path):
    """Supply executable evidence while the provider executor is mocked.

    CI deliberately has no real provider CLIs installed.  The fresh live lane
    must still require executable evidence, so these tests install a tiny
    inert launcher and replace PATH only for roster resolution.  No test ever
    executes this file: the executor seam is patched below.
    """
    bin_dir = root / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        launcher = bin_dir / "claude.cmd"
        launcher.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    else:
        launcher = bin_dir / "claude"
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(0o755)
    previous = os.environ.get("PATH")
    os.environ["PATH"] = str(bin_dir) + os.pathsep + (previous or "")
    return previous


def _restore_path(previous: str | None):
    if previous is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = previous


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
    def test_windows_unc_context_input_is_refused_before_path_touch(self) -> None:
        with mock.patch.object(store.os, "name", "nt"), \
                mock.patch.object(store.Path, "lstat") as lstat:
            with self.assertRaisesRegex(ValueError, "local file"):
                store._read_bounded_context_file(
                    r"\\server\share\context.json", "--context-file")
        lstat.assert_not_called()

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

        rewritten, mode = _cli.rewrite_subcommand(
            ["chat", "message", "room-1", "worker", "human", "--message", "context"])
        self.assertIsNone(mode)
        self.assertEqual(
            rewritten,
            ["--chat-action", "message", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-to", "human",
             "--message", "context"])
        rewritten, mode = _cli.rewrite_subcommand(
            ["chat", "inbox", "room-1", "worker", "--after", "3"])
        self.assertIsNone(mode)
        self.assertEqual(
            rewritten,
            ["--chat-action", "inbox", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-after", "3"])

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
        for argv in (
            ["--chat-action", "message", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-to", "human",
             "--chat-message", "context"],
            ["--chat-action", "inbox", "--chat-session", "room-1",
             "--chat-participant", "worker", "--chat-after", "3"],
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

    def test_fresh_command_requires_a_resolvable_readonly_roster(self) -> None:
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
            self.assertEqual(body["status"], "error")
            self.assertEqual(body["mode"], "deliberation-command")
            self.assertNotEqual(body.get("error_kind"), "integration_pending")
            self.assertFalse(os.path.exists(run_root))

    def test_fresh_live_lane_binds_receipt_before_scheduler(self) -> None:
        class FakeReport:
            status = "success"

            def as_dict(self):
                return {
                    "mode": "deliberation-run", "status": "success",
                    "state": "ATTEMPT_BUDGET_EXHAUSTED", "uncertain_spend": False,
                }

        class FakeScheduler:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

            def run(self):
                return FakeReport()

        with tempfile.TemporaryDirectory() as temp:
            previous_path = _install_fake_claude_on_path(Path(temp))
            self.addCleanup(_restore_path, previous_path)
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            run_root = Path(temp) / "runs"
            manifest = Path(temp) / "revision-manifest.json"
            manifest_bytes = b'{"revision":"bounded"}\n'
            manifest.write_bytes(manifest_bytes)
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            context_file = Path(temp) / "durable-context.json"
            context_file.write_text(json.dumps({
                "schema": "summon.deliberation-context/v1",
                "source": {"kind": "revision_manifest",
                           "revision": "sha256:" + manifest_sha,
                           "source_digest": manifest_sha,
                           "captured_at_unix_ms": int(time.time() * 1000)},
                "freshness_policy": {"fresh_max_age_ms": 60_000,
                                     "hard_max_age_ms": 120_000,
                                     "hard_max_revision_delta": 0},
                "entries": [{"id": "constraint-1", "kind": "constraint",
                             "body": "PRIVATE DURABLE CONTEXT",
                             "provenance": {"kind": "authored",
                                            "source_sha256": "a" * 64}}],
            }), encoding="utf-8")
            parser = _cli.build_parser("test", 1)
            args = parser.parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(run_root), "--json",
                "--context-file", str(context_file),
                "--context-observation-file", str(manifest),
            ])
            self.assertIsNone(_cli.unsupported_mode_flags([
                "--deliberate", "--context-file", str(context_file),
                "--context-observation-file", str(manifest)], args))
            fake = FakeScheduler()
            with mock.patch("_deliberation_live.build_live_scheduler",
                            return_value=fake):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = store.run_command(args)
            self.assertEqual(code, 0)
            body = json.loads(output.getvalue())
            self.assertEqual(body["status"], "success")
            self.assertEqual(body["receipt"]["seat_ids"], ["one", "two"])
            self.assertEqual(body["receipt"]["quorum_rule"], "all")
            self.assertEqual(body["receipt"]["plan_identity_by_seat"]["one"][
                "effective_permission"], "read-only")
            self.assertEqual(body["receipt"]["durable_context"]["state"], "fresh")
            self.assertNotIn("PRIVATE DURABLE CONTEXT", output.getvalue())
            self.assertNotIn(str(project), output.getvalue())
            self.assertFalse(fake.cancelled)

    def test_stale_acceptance_identity_is_reachable_from_fresh_cli(self) -> None:
        class FakeReport:
            def as_dict(self):
                return {"mode": "deliberation-run", "status": "success",
                        "state": "UNRESOLVED", "uncertain_spend": False}

        class FakeScheduler:
            def cancel(self):
                pass

            def run(self):
                return FakeReport()

        with tempfile.TemporaryDirectory() as temp:
            previous_path = _install_fake_claude_on_path(Path(temp))
            self.addCleanup(_restore_path, previous_path)
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            manifest = Path(temp) / "revision-manifest.json"
            manifest_bytes = b'{"revision":"bounded"}\n'
            manifest.write_bytes(manifest_bytes)
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            now_ms = int(time.time() * 1000)
            packet = {
                "schema": "summon.deliberation-context/v1",
                "source": {"kind": "revision_manifest",
                           "revision": "sha256:" + manifest_sha,
                           "source_digest": manifest_sha,
                           "captured_at_unix_ms": now_ms - 5_000},
                "freshness_policy": {"fresh_max_age_ms": 1_000,
                                     "hard_max_age_ms": 120_000,
                                     "hard_max_revision_delta": 0},
                "entries": [{"id": "constraint-1", "kind": "constraint",
                             "body": "Bounded stale instruction.",
                             "provenance": {"kind": "authored",
                                            "source_sha256": "a" * 64}}],
            }
            parsed = context.parse_context_packet(packet)
            run_id = "deliberation-operator-selected"
            decision_id = "decision-operator-selected"
            run_root = Path(temp) / "runs"
            namespace = os.path.normcase(os.path.realpath(
                os.path.abspath(run_root / "deliberations")))
            intent = {
                "schema": "summon.accept-stale-intent/v2",
                "packet_sha256": parsed.packet_sha256,
                "source_revision_sha256": parsed.source_revision_sha256,
                "runs_root_sha256": hashlib.sha256(
                    namespace.encode("utf-8")).hexdigest(),
                "run_id": run_id, "decision_id": decision_id,
                "actor": {"kind": "human", "id": "operator"},
                "reason": "Reviewed the bounded age delta.",
                "scope": {"entry_ids": ["constraint-1"],
                          "use": "deliberation_prompt"},
                "expires_at_unix_ms": now_ms + 60_000,
                "max_age_ms": 30_000, "max_revision_delta": 0,
            }
            context_file = Path(temp) / "context.json"
            context_file.write_text(json.dumps(packet), encoding="utf-8")
            acceptance_file = Path(temp) / "acceptance.json"
            acceptance_file.write_text(json.dumps(intent), encoding="utf-8")
            args = _cli.build_parser("test", 1).parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(run_root), "--json",
                "--context-file", str(context_file),
                "--context-observation-file", str(manifest),
                "--accept-stale-file", str(acceptance_file),
            ])
            with mock.patch("_deliberation_live.build_live_scheduler",
                            return_value=FakeScheduler()):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = store.run_command(args)
            body = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(body["run_id"], run_id)
            self.assertEqual(body["receipt"]["decision_id"], decision_id)
            self.assertEqual(body["receipt"]["durable_context"]["state"],
                             "accepted_stale")

    def test_live_context_revalidates_run_namespace_before_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            previous_path = _install_fake_claude_on_path(Path(temp))
            self.addCleanup(_restore_path, previous_path)
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            manifest = Path(temp) / "revision-manifest.json"
            manifest_bytes = b'{"revision":"bounded"}\n'
            manifest.write_bytes(manifest_bytes)
            digest = hashlib.sha256(manifest_bytes).hexdigest()
            packet = {
                "schema": "summon.deliberation-context/v1",
                "source": {"kind": "revision_manifest", "revision": "sha256:" + digest,
                           "source_digest": digest,
                           "captured_at_unix_ms": int(time.time() * 1000)},
                "freshness_policy": {"fresh_max_age_ms": 60_000,
                                     "hard_max_age_ms": 120_000,
                                     "hard_max_revision_delta": 0},
                "entries": [{"id": "constraint-1", "kind": "constraint",
                             "body": "Bounded instruction.",
                             "provenance": {"kind": "authored",
                                            "source_sha256": "a" * 64}}],
            }
            context_file = Path(temp) / "context.json"
            context_file.write_text(json.dumps(packet), encoding="utf-8")
            args = _cli.build_parser("test", 1).parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(Path(temp) / "runs"),
                "--json", "--context-file", str(context_file),
                "--context-observation-file", str(manifest),
            ])
            output = io.StringIO()
            with mock.patch.object(store, "_runs_root_sha256",
                                   side_effect=["a" * 64, "b" * 64]), \
                    mock.patch("_deliberation_live.build_live_scheduler") as scheduler, \
                    contextlib.redirect_stdout(output):
                code = store.run_command(args)
            self.assertEqual(code, 1)
            self.assertIn("namespace changed", json.loads(output.getvalue())["error"])
            scheduler.assert_not_called()

    def test_fresh_live_lane_refuses_retired_seat_before_scheduler_or_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            (agents / "one.md").write_text(
                "---\nrun-agent: definitely-missing-provider\npermission: yolo\n"
                "lifecycle: retired\nsuccessor: one-v2\n---\n",
                encoding="utf-8")
            (agents / "two.md").write_text(
                "---\nrun-agent: claude\npermission: read-only\n---\n",
                encoding="utf-8")
            run_root = Path(temp) / "runs"
            parser = _cli.build_parser("test", 1)
            args = parser.parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(run_root), "--json",
            ])
            output = io.StringIO()
            with mock.patch("_deliberation_live.build_live_scheduler") as scheduler:
                with contextlib.redirect_stdout(output):
                    code = store.run_command(args)
            body = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(body["status"], "error")
            self.assertIn("retired", body["error"])
            self.assertIn("one-v2", body["error"])
            scheduler.assert_not_called()
            self.assertFalse(run_root.exists())

    def test_fresh_live_lane_rejects_authority_consent_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            run_root = Path(temp) / "runs"
            parser = _cli.build_parser("test", 1)
            args = parser.parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--text-only-consent", "one",
                "--cwd", str(project), "--agents-dir", str(agents),
                "--run-dir", str(run_root), "--json",
            ])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = store.run_command(args)
            body = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(body["error_kind"], "integration_pending")
            self.assertEqual(body["status"], "blocked")
            self.assertFalse(run_root.exists())

    def test_fresh_live_lane_runs_real_scheduler_with_fake_provider(self) -> None:
        def fake_execute(invocation, **kwargs):
            control = kwargs["launch_control"]
            control.before_provider_launch({"backend": invocation.cli,
                                            "transport": invocation.transport})
            packet = json.loads(invocation.prompt.split(
                "DELIBERATION_PACKET:\n", 1)[1])
            ordinal = packet["turn_ordinal"]
            ballot = {
                "schema_version": 1, "decision_id": packet["decision_id"],
                "seat_id": packet["seat"]["id"], "turn_id": packet["turn_id"],
                "attempt_id": f"g1-a{ordinal}", "decision": "vote",
                "option_id": "yes", "confidence": "high", "evidence_refs": [],
            }
            return {"status": "success", "exit_code": 0,
                    "result": json.dumps({"ballot": ballot})}

        with tempfile.TemporaryDirectory() as temp:
            previous_path = _install_fake_claude_on_path(Path(temp))
            self.addCleanup(_restore_path, previous_path)
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            run_root = Path(temp) / "runs"
            parser = _cli.build_parser("test", 1)
            args = parser.parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(run_root), "--json",
            ])
            output = io.StringIO()
            with mock.patch("_deliberation_live._executor.execute_agent",
                            side_effect=fake_execute):
                with contextlib.redirect_stdout(output):
                    code = store.run_command(args)
            self.assertEqual(code, 0)
            body = json.loads(output.getvalue())
            self.assertEqual(body["state"], "DECIDED")
            self.assertEqual(body["decision_option"], "yes")
            self.assertEqual(body["turns_started"], 2)
            status = store.inspect_run(str(run_root / "deliberations"), body["run_id"])
            self.assertEqual(status["projection"]["state"], "decided")
            self.assertEqual(status["projection"]["physical_attempts"]["started"], 2)

    def test_fresh_live_lane_consumes_durable_cancel(self) -> None:
        provider_contacted = threading.Event()

        def blocking_execute(invocation, **kwargs):
            control = kwargs["launch_control"]
            control.before_provider_launch({"backend": invocation.cli,
                                            "transport": invocation.transport})
            provider_contacted.set()
            while not control.is_cancelled():
                time.sleep(0.01)
            return {"status": "error", "exit_code": 1, "result": ""}

        with tempfile.TemporaryDirectory() as temp:
            previous_path = _install_fake_claude_on_path(Path(temp))
            self.addCleanup(_restore_path, previous_path)
            project = Path(temp) / "project"
            agents = project / "agents"
            agents.mkdir(parents=True)
            for name in ("one", "two"):
                (agents / f"{name}.md").write_text(
                    "---\nrun-agent: claude\npermission: read-only\n---\n",
                    encoding="utf-8")
            run_root = Path(temp) / "runs"
            parser = _cli.build_parser("test", 1)
            args = parser.parse_args([
                "--deliberate", "--question", "q", "--seats", "one,two",
                "--options", "yes,no", "--quorum", "all", "--max-attempts", "2",
                "--deadline", "30s", "--cwd", str(project),
                "--agents-dir", str(agents), "--run-dir", str(run_root), "--json",
            ])
            emitted = mock.Mock()
            outcome: list[object] = []

            def run() -> None:
                with mock.patch.object(store, "_emit", emitted):
                    try:
                        outcome.append(store.run_command(args))
                    except BaseException as exc:  # surface worker failures below
                        outcome.append(exc)

            with mock.patch("_deliberation_live._executor.execute_agent",
                            side_effect=blocking_execute):
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                self.assertTrue(provider_contacted.wait(10), "fake provider did not start")
                deliberations = run_root / "deliberations"
                run_id = None
                for _ in range(200):
                    candidates = [item for item in deliberations.iterdir()
                                  if item.is_dir()] if deliberations.exists() else []
                    if candidates:
                        run_id = candidates[0].name
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(run_id)
                queued = store.queue_cancel(str(deliberations), run_id, "stop-1")
                self.assertFalse(queued["applied"])
                worker.join(15)

            self.assertFalse(worker.is_alive(), "live lane did not consume cancel")
            self.assertEqual(outcome, [1])
            self.assertTrue(emitted.called)
            result = emitted.call_args.args[0]
            self.assertEqual(result["state"], "CANCELLED")
            self.assertEqual(result["cancel"], "applied")
            status = store.inspect_run(str(deliberations), run_id)
            self.assertEqual(status["projection"]["state"], "cancelled")
            self.assertTrue(any(record.get("event") == "human_command"
                                for record in store.replay_run(
                                    str(deliberations), run_id)["records"]))
            self.assertFalse((deliberations / run_id / "commands" / "stop-1.json").exists())

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
            args = parser.parse_args([flag, r"C:\Users\test-user\private-project"])
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
                                  "--run-dir", r"C:\Users\test-user\private-project"])
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
                "retained_resources": [r"C:\Users\test-user\private-project"],
                "secret": "TOKEN-DO-NOT-EXPORT", "argv": ["--password", "TOKEN"],
            }, owner=owner)
            _rundir.journal_append(path, {
                "event": "advisory_left_behind", "schema_version": 1,
                "generation": owner.generation,
                "items": [r"C:\Users\test-user\private-project", "TOKEN=secret"],
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

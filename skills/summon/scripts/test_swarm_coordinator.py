from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _swarm_coordinator import (SwarmConflictError, SwarmCoordinator,
                                SwarmCoordinatorError, SwarmIndeterminateError)
import _swarm_coordinator as swarm_module
from _swarm_protocol import PROTOCOL, SwarmProtocolError, make_frame


class _Clock:
    def __init__(self, value: float = 2_000_000_000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value


class SwarmCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-swarm-")
        self.clock = _Clock()
        self.request = hashlib.sha256(b"task-a").hexdigest()
        self.project = "a" * 64
        self.roster = "b" * 64
        self.coordinator = SwarmCoordinator.create(
            self.temp.name, "swarm-1", project_root_sha256=self.project,
            roster_definition_sha256=self.roster,
            tasks=[{"task_id": "task-a", "request_sha256": self.request}],
            max_attempts=2, clock=self.clock)
        self.coordinator.register_worker("worker-a", worker_instance_id="instance-a",
                                        capabilities=["message", "cancel"])
        self.coordinator.register_worker("worker-b", worker_instance_id="instance-b",
                                        capabilities=["message", "cancel"])

    def tearDown(self):
        self.temp.cleanup()

    def test_prepared_status_is_redacted_and_bound(self):
        status = self.coordinator.status()
        self.assertEqual(status["status"], "prepared")
        self.assertEqual(status["project_root_sha256"], self.project)
        self.assertEqual(status["roster_definition_sha256"], self.roster)
        self.assertEqual(status["worker_count"], 2)
        self.assertEqual(status["tasks"][0]["status"], "pending")
        self.assertFalse(status["uncertain_spend"])

    def test_only_one_live_claim_and_terminal_fence(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.assertEqual(claim["status"], "claimed")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.complete(
                "worker-b", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, envelope_sha256="c" * 64)
        done = self.coordinator.complete(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, envelope_sha256="c" * 64)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(self.coordinator.status()["status"], "completed")

    def test_two_coordinator_instances_share_one_durable_claim(self):
        # Separate coordinator objects model separate worker processes.  Each
        # operation must reacquire the run owner and observe the same journal;
        # a process-local active map is not sufficient protection.
        other = SwarmCoordinator(self.temp.name, "swarm-1", clock=self.clock)
        other.register_worker("worker-c", worker_instance_id="instance-c",
                              capabilities=["message"])
        first = other.claim("worker-c", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        done = self.coordinator.complete(
            "worker-c", task_id="task-a", claim_id=first["claim_id"],
            attempt=first["attempt"], lease_generation=first["lease_generation"],
            request_sha256=self.request, envelope_sha256="f" * 64)
        self.assertEqual(done["status"], "completed")

    def test_duplicate_frame_is_idempotent_and_replay_conflict_is_rejected(self):
        claim_id = "claim-fixed"
        frame = make_frame(
            "claim", run_id="swarm-1", message_id="claim-message",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"task_id": "task-a", "claim_id": claim_id, "attempt": 1,
                     "lease_generation": 1,
                     "lease_expires_at_ms": int(self.clock.value * 1000) + 10_000,
                     "request_sha256": self.request})
        first = self.coordinator.apply_frame(frame, worker_id="worker-a")
        second = self.coordinator.apply_frame(frame, worker_id="worker-a")
        self.assertEqual(first, second)
        altered = dict(frame)
        altered["payload"] = {**frame["payload"], "request_sha256": "d" * 64}
        with self.assertRaises((SwarmCoordinatorError, SwarmProtocolError)):
            self.coordinator.apply_frame(altered, worker_id="worker-a")

    def test_renewal_extends_only_the_current_worker_claim(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        renewed = self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"],
                                         lease_ms=50_000)
        self.assertEqual(renewed["status"], "renewed")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.renew("worker-b", claim["claim_id"], claim["lease_generation"])
        with self.assertRaises(SwarmConflictError):
            self.coordinator.renew("worker-a", claim["claim_id"], claim["lease_generation"] + 1)

    def test_expired_claim_requires_explicit_uncertain_spend_policy(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        self.assertEqual(self.coordinator.status()["status"], "uncertain_spend")
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        blocked = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=False, reason=r"provider may have touched C:\private\repo",
            human_confirmed=True)
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["uncertain_spend"])
        status = self.coordinator.status()
        self.assertEqual(status["tasks"][0]["status"], "blocked")
        self.assertTrue(status["uncertain_spend"])

    def test_expired_claim_can_retry_only_after_human_authorization(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.acknowledge_indeterminate(
                "task-a", allow_retry=True, reason="reviewed", human_confirmed=False)
        retry = self.coordinator.acknowledge_indeterminate(
            "task-a", allow_retry=True, reason="reviewed and explicitly re-authorized",
            human_confirmed=True)
        self.assertEqual(retry["status"], "retry_authorized")
        claim2 = self.coordinator.claim("worker-b", "task-a", request_sha256=self.request)
        self.assertEqual(claim2["attempt"], 2)
        self.assertEqual(claim2["lease_generation"], claim["lease_generation"] + 1)

    def test_expired_claim_rejects_stale_worker_and_operator_mutations(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request,
                                       lease_ms=1_000)
        self.clock.value += 2
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.complete(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, envelope_sha256="c" * 64)
        with self.assertRaises(SwarmIndeterminateError):
            self.coordinator.cancel("task-a")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.acknowledge_indeterminate(
                "task-a", allow_retry="false", reason="reviewed", human_confirmed="false")
        self.assertEqual(self.coordinator.status()["status"], "uncertain_spend")

    def test_indeterminate_recovery_requires_real_booleans(self):
        self.coordinator.claim("worker-a", "task-a", request_sha256=self.request, lease_ms=1_000)
        self.clock.value += 2
        for allow_retry, human_confirmed in (("false", True), (False, "true"), (1, True)):
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.acknowledge_indeterminate(
                    "task-a", allow_retry=allow_retry, reason="reviewed",
                    human_confirmed=human_confirmed)

    def test_closed_run_is_immutable(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        self.coordinator.fail(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, envelope_sha256="e" * 64)
        self.coordinator.close()
        with self.assertRaises(SwarmConflictError):
            self.coordinator.register_worker("worker-c", worker_instance_id="instance-c")
        with self.assertRaises(SwarmConflictError):
            self.coordinator.send_message("worker-a", "worker-b", "late message")

    def test_message_admission_is_bounded_not_only_status_projection(self):
        with mock.patch.object(swarm_module, "MAX_JOURNAL_BYTES", 1):
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.send_message("worker-a", "worker-b", "journal blocked")
        with mock.patch.object(swarm_module, "MAX_STATUS_MESSAGES", 1):
            self.coordinator.send_message("worker-a", "worker-b", "first")
            with self.assertRaises(SwarmCoordinatorError):
                self.coordinator.send_message("worker-a", "worker-b", "second")

    def test_managed_journal_symlink_is_rejected_before_write(self):
        target = Path(self.temp.name) / "outside-journal.jsonl"
        target.write_text("outside", encoding="utf-8")
        link = Path(self.coordinator.run_dir) / "journal-g99.jsonl"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.send_message("worker-a", "worker-b", "must not follow link")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside")

    def test_cancel_requires_ack_and_terminal_outcome(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        requested = self.coordinator.cancel("task-a", reason="operator requested")
        self.assertEqual(requested["status"], "cancel_requested")
        ack = make_frame("ack_cancel", run_id="swarm-1", message_id="cancel-ack",
                         sent_at_ms=int(self.clock.value * 1000),
                         payload={"claim_id": claim["claim_id"],
                                  "lease_generation": claim["lease_generation"]})
        self.assertEqual(self.coordinator.apply_frame(ack, worker_id="worker-a")["status"],
                         "cancel_acknowledged")
        cancelled = make_frame("cancelled", run_id="swarm-1", message_id="cancelled",
                               sent_at_ms=int(self.clock.value * 1000),
                               payload={"claim_id": claim["claim_id"],
                                        "lease_generation": claim["lease_generation"],
                                        "reason": "stopped"})
        self.assertEqual(self.coordinator.apply_frame(cancelled, worker_id="worker-a")["status"],
                         "cancelled")
        self.assertEqual(self.coordinator.status()["tasks"][0]["status"], "cancelled")

    def test_message_is_context_only_and_publicly_redacted(self):
        sent = self.coordinator.send_message("worker-a", "worker-b", r"look at C:\private\repo")
        self.assertEqual(sent["status"], "posted")
        event = [e for e in self.coordinator.events() if e.get("event") == "message_posted"][-1]
        self.assertNotIn(r"C:\private\repo", json_text(event))
        self.assertEqual(event["to"], "worker-b")
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.send_message("worker-a", "not-registered", "no")

    def test_message_preview_redacts_paths_and_credentials(self):
        samples = [
            r"C:\private folder\repo",
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
            "SECRET_TOKEN=do-not-publish",
            "cookie: session=do-not-publish",
        ]
        for index, sample in enumerate(samples):
            self.coordinator.send_message("worker-a", "worker-b", sample,
                                          message_id=f"redaction-{index}")
        text = json_text(self.coordinator.events())
        for sample in samples:
            self.assertNotIn(sample, text)
        self.assertNotIn("do-not-publish", text)

    def test_artifact_is_claim_fenced_and_never_dereferenced(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        artifact = self.coordinator.publish_artifact(
            "worker-a", task_id="task-a", claim_id=claim["claim_id"],
            attempt=claim["attempt"], lease_generation=claim["lease_generation"],
            request_sha256=self.request, artifact_id="artifact-a", sha256="d" * 64,
            bytes_count=12, media_type="text/plain", relative_path="out.txt")
        self.assertEqual(artifact["status"], "artifact_published")
        with self.assertRaises(SwarmProtocolError):
            self.coordinator.publish_artifact(
                "worker-a", task_id="task-a", claim_id=claim["claim_id"],
                attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                request_sha256=self.request, artifact_id="artifact-b", sha256="d" * 64,
                bytes_count=1, media_type="text/plain", relative_path=r"..\secret.txt")

    def test_foreign_frame_run_and_timestamp_are_refused(self):
        frame = make_frame("poll", run_id="other-run", message_id="poll-1",
                           sent_at_ms=int(self.clock.value * 1000))
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.apply_frame(frame)
        stale = make_frame("poll", run_id="swarm-1", message_id="poll-2",
                           sent_at_ms=int(self.clock.value * 1000) - 600_001)
        with self.assertRaises(SwarmCoordinatorError):
            self.coordinator.apply_frame(stale)

    def test_close_requires_no_live_claim_and_is_idempotent(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        with self.assertRaises(SwarmConflictError):
            self.coordinator.close()
        self.coordinator.fail("worker-a", task_id="task-a", claim_id=claim["claim_id"],
                              attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                              request_sha256=self.request, envelope_sha256="e" * 64)
        self.assertEqual(self.coordinator.close()["status"], "closed")
        self.assertEqual(self.coordinator.close()["status"], "closed")

    def test_torn_tail_is_repaired_on_next_mutation(self):
        claim = self.coordinator.claim("worker-a", "task-a", request_sha256=self.request)
        journal = max(Path(self.coordinator.run_dir).glob("journal-g*.jsonl"),
                      key=lambda path: int(path.stem.removeprefix("journal-g")))
        with journal.open("ab") as handle:
            handle.write(b'{"torn":')
        self.assertTrue(self.coordinator.status()["torn_tail"])
        self.coordinator.fail("worker-a", task_id="task-a", claim_id=claim["claim_id"],
                              attempt=claim["attempt"], lease_generation=claim["lease_generation"],
                              request_sha256=self.request, envelope_sha256="e" * 64)
        self.assertFalse(self.coordinator.status()["torn_tail"])

    def test_cli_create_and_status_are_provider_inert(self):
        task_file = Path(self.temp.name) / "tasks.json"
        task_file.write_text(json.dumps([{"task_id": "task-a", "request_sha256": self.request}]),
                             encoding="utf-8")
        command = [sys.executable, str(HERE / "run_subagent.py")]
        create = subprocess.run(
            command + ["swarm", "create", "cli-run", "--swarm-dir", self.temp.name,
                       "--swarm-tasks", str(task_file),
                       "--swarm-project-root-sha256", self.project,
                       "--swarm-roster-sha256", self.roster, "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(create.returncode, 0, create.stderr)
        self.assertEqual(json.loads(create.stdout)["status"], "prepared")
        status = subprocess.run(
            command + ["swarm", "status", "cli-run", "--swarm-dir", self.temp.name, "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["run_id"], "cli-run")


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()

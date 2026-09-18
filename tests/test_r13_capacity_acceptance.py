"""Real coordinator byte-boundary acceptance; no provider access.

Windows-host execution is evidence only for that host. These tests do not certify
POSIX or filesystem power-loss behavior. All roots and journal bodies are synthetic.
The one two-process fixture uses shared hidden-launch flags and bounded cleanup.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "summon" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _rundir as rd
import _swarm_coordinator as swarm
from _context_target import _final_open_path
from _swarm_protocol import make_frame
from _spawn import popen_flags


STAMP = 2_000_000_000.25
REQUEST = "d" * 64
WORST_REASON = "\x01" * 512


class Clock:
    def __init__(self):
        self.value = STAMP

    def __call__(self):
        return self.value


class CapacityAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="summon-r13-acceptance-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.clock = Clock()
        # Freeze journal timestamps AND owner wall clock. Claim expiry uses the
        # separate injected clock, so an expired claim need not expire its owner.
        timer = mock.patch.object(swarm.time, "time", return_value=STAMP)
        timer.start()
        self.addCleanup(timer.stop)
        self.serial = itertools.count(1)

    def fixture(self, label, *, maximal=False):
        run_id = "r" * 64 if maximal else "run-1"
        task = "t" * 128 if maximal else "task-1"
        a = "a" * 128 if maximal else "worker-a"
        b = "b" * 128 if maximal else "worker-b"
        coordinator = swarm.SwarmCoordinator.create(
            self.root / label, run_id, project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64,
            tasks=[{"task_id": task, "request_sha256": REQUEST}],
            max_attempts=2, clock=self.clock,
        )
        coordinator.register_worker(a, worker_instance_id="i" * (128 if maximal else 8))
        coordinator.register_worker(b, worker_instance_id="j" * (128 if maximal else 8))
        return coordinator, task, a, b

    def clone(self, original, label):
        destination = self.root / label
        shutil.copytree(original.runs_root, destination)
        return swarm.SwarmCoordinator(destination, original.run_id, clock=self.clock)

    def journals(self, coordinator):
        return {p.name: p.read_bytes() for p in Path(coordinator.run_dir).glob("journal-g*.jsonl")}

    def used(self, coordinator):
        return sum(map(len, self.journals(coordinator).values()))

    def records(self, coordinator):
        return [json.loads(line) for raw in self.journals(coordinator).values()
                for line in raw.splitlines()]

    def event_count(self, coordinator, message_id):
        return sum(r.get("message_id") == message_id for r in self.records(coordinator))

    def reserve(self, coordinator):
        state, _ = coordinator._load()
        return coordinator._reserve_after(state, {"event": "message_posted"})

    def claim(self, coordinator, task, worker, *, maximal=False):
        claim_id = "c" * 128 if maximal else "claim-fixed"
        frame = make_frame(
            "claim", run_id=coordinator.run_id,
            message_id="q" * 128 if maximal else "claim-frame",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"task_id": task, "claim_id": claim_id, "attempt": 1,
                     "lease_generation": 1,
                     "lease_expires_at_ms": int(self.clock.value * 1000) + 30_000,
                     "request_sha256": REQUEST},
        )
        return coordinator.apply_frame(frame, worker_id=worker)

    def message_frame(self, coordinator, a, b, message_id="one-message"):
        return make_frame(
            "send_message", run_id=coordinator.run_id, message_id=message_id,
            sent_at_ms=int(self.clock.value * 1000),
            payload={"from": a, "to": b, "content_sha256": hashlib.sha256(b"body").hexdigest(),
                     "content_chars": 4, "preview": "body"},
        )

    def ack_and_outcome(self, coordinator, worker, claim, outcome="cancelled", *, maximal=False):
        ack = make_frame(
            "ack_cancel", run_id=coordinator.run_id,
            message_id="k" * 128 if maximal else "cancel-ack",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"claim_id": claim["claim_id"], "lease_generation": claim["lease_generation"]},
        )
        self.assertEqual(coordinator.apply_frame(ack, worker_id=worker)["status"], "cancel_acknowledged")
        frame = make_frame(
            outcome, run_id=coordinator.run_id,
            message_id="o" * 128 if maximal else "cancel-outcome",
            sent_at_ms=int(self.clock.value * 1000),
            payload={"claim_id": claim["claim_id"], "lease_generation": claim["lease_generation"],
                     "reason": WORST_REASON},
        )
        return coordinator.apply_frame(frame, worker_id=worker)

    def assert_bounds(self, coordinator):
        observed = set()
        for raw in self.journals(coordinator).values():
            self.assertNotIn(b"\r\n", raw)
            for line in raw.splitlines(keepends=True):
                data = json.loads(line)
                event = data["event"]
                data.pop("sha256")
                self.assertEqual(rd.encode_journal_record(data, timestamp=data["ts"]), line)
                self.assertLessEqual(len(line), swarm._EVENT_BOUND_BYTES[event], event)
                observed.add(event)
        return observed

    def test_actual_encoded_admission_at_h_minus_one_h_and_h_plus_one(self):
        baseline, _task, a, b = self.fixture("baseline")
        frame = self.message_frame(baseline, a, b)
        calibration = self.clone(baseline, "calibration")
        before = self.used(calibration)
        self.assertEqual(calibration.apply_frame(frame, worker_id=a)["status"], "posted")
        actual_encoded_bytes = self.used(calibration) - before
        self.assertGreater(actual_encoded_bytes, 0)
        required = before + actual_encoded_bytes + self.reserve(calibration)
        for delta in (-1, 0, 1):
            with self.subTest(cap_delta=delta):
                candidate = self.clone(baseline, f"cap-{delta}")
                unchanged = self.journals(candidate)
                with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", required + delta):
                    if delta < 0:
                        with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                            candidate.apply_frame(frame, worker_id=a)
                        self.assertEqual(self.journals(candidate), unchanged)
                    else:
                        candidate.apply_frame(frame, worker_id=a)
                        self.assertEqual(self.journals(candidate), self.journals(calibration))
                        self.assertEqual(self.used(candidate) + self.reserve(candidate), required)
                        self.assertEqual(self.event_count(candidate, frame["message_id"]), 1)

    def test_ordinary_soft_boundary_preserves_complete_cancellation_path_at_same_cap(self):
        coordinator, task, a, b = self.fixture("cancel")
        claim = self.claim(coordinator, task, a)
        frame = self.message_frame(coordinator, a, b)
        calibration = self.clone(coordinator, "cancel-calibration")
        calibration.apply_frame(frame, worker_id=a)
        cap = self.used(calibration) + self.reserve(calibration)
        with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
            coordinator.apply_frame(frame, worker_id=a)
            self.assertEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            before_refusal = self.journals(coordinator)
            with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                coordinator.send_message(a, b, "another", message_id="ordinary-refused")
            self.assertEqual(self.journals(coordinator), before_refusal)
            coordinator.cancel(task, reason=WORST_REASON, message_id="cancel-request")
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            result = self.ack_and_outcome(coordinator, a, claim)
            self.assertEqual(result["status"], "cancelled")
            self.assertEqual(coordinator.close()["status"], "closed")
            self.assertLessEqual(self.used(coordinator), cap)
        events = [r["event"] for r in self.records(coordinator)]
        for event in ("cancel_requested", "cancel_acknowledged", "task_cancelled", "run_closed"):
            self.assertEqual(events.count(event), 1)
        self.assertEqual(self.event_count(coordinator, "ordinary-refused"), 0)
        reopened = swarm.SwarmCoordinator(coordinator.runs_root, coordinator.run_id, clock=self.clock)
        self.assertEqual(reopened.status()["tasks"][0]["status"], "cancelled")
        self.assert_bounds(coordinator)

    def test_expired_active_claim_recovers_at_its_reserved_boundary(self):
        for allow_retry in (False, True):
            with self.subTest(allow_retry=allow_retry):
                self.clock.value = STAMP
                coordinator, task, a, b = self.fixture(f"recovery-{allow_retry}")
                self.claim(coordinator, task, a)
                cap = self.used(coordinator) + self.reserve(coordinator)
                self.clock.value += 31
                before = self.journals(coordinator)
                with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
                    with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                        coordinator.send_message(a, b, "blocked", message_id="ordinary-refused")
                    self.assertEqual(self.journals(coordinator), before)
                    result = coordinator.acknowledge_indeterminate(
                        task, allow_retry=allow_retry, human_confirmed=True, reason=WORST_REASON)
                    self.assertEqual(result["status"], "retry_authorized" if allow_retry else "blocked")
                    self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
                    if not allow_retry:
                        coordinator.close()
                events = [r["event"] for r in self.records(coordinator)]
                self.assertEqual(events.count("claim_indeterminate"), 1)
                self.assertEqual(events.count("retry_authorized" if allow_retry else "task_blocked"), 1)
                self.assertEqual(events.count("claim_granted"), 1)
                self.assertEqual(self.event_count(coordinator, "ordinary-refused"), 0)
                self.assertLessEqual(self.used(coordinator), cap)
                self.assert_bounds(coordinator)

    def test_real_maximum_identifier_and_escaped_reason_constructors_fit_bounds(self):
        def maximal_message_id(prefix):
            suffix = str(next(self.serial))
            return (prefix + "x" * 128)[:128 - len(suffix)] + suffix

        coverage = set()
        with mock.patch.object(swarm, "_message_id", side_effect=maximal_message_id):
            for branch in ("cancelled", "indeterminate-retry", "indeterminate-block", "failed", "completed", "released"):
                with self.subTest(branch=branch):
                    coordinator, task, a, b = self.fixture("max-" + branch, maximal=True)
                    claim = self.claim(coordinator, task, a, maximal=True)
                    coordinator.renew(a, claim["claim_id"], 1, lease_ms=swarm.MAX_LEASE_MS)
                    coordinator.send_message(a, b, WORST_REASON, reply_to="p" * 128)
                    coordinator.publish_artifact(
                        a, task_id=task, claim_id=claim["claim_id"], attempt=1,
                        lease_generation=1, request_sha256=REQUEST, artifact_id="z" * 128,
                        sha256="f" * 64, bytes_count=swarm.MAX_ARTIFACT_BYTES,
                        media_type=WORST_REASON, relative_path=WORST_REASON)
                    if branch.startswith("indeterminate") or branch == "cancelled":
                        coordinator.cancel(task, reason=WORST_REASON)
                        self.ack_and_outcome(coordinator, a, claim,
                                             "cancelled" if branch == "cancelled" else "indeterminate",
                                             maximal=True)
                        if branch.startswith("indeterminate"):
                            coordinator.acknowledge_indeterminate(
                                task, allow_retry=branch.endswith("retry"),
                                reason=WORST_REASON, human_confirmed=True)
                    elif branch in ("failed", "completed"):
                        operation = coordinator.fail if branch == "failed" else coordinator.complete
                        extra = {"reason": WORST_REASON} if branch == "failed" else {}
                        operation(a, task_id=task, claim_id=claim["claim_id"], attempt=1,
                                  lease_generation=1, request_sha256=REQUEST,
                                  envelope_sha256="e" * 64, **extra)
                    else:
                        # The actual wire contract names this claim_released
                        # and permits no freeform reason on that frame.
                        frame = make_frame("claim_released", run_id=coordinator.run_id,
                                           message_id="l" * 128, sent_at_ms=int(self.clock.value * 1000),
                                           payload={"task_id": task, "claim_id": claim["claim_id"],
                                                    "attempt": 1, "lease_generation": 1,
                                                    "request_sha256": REQUEST})
                        coordinator.apply_frame(frame, worker_id=a)
                    coverage |= self.assert_bounds(coordinator)
                    self.assertTrue(any(b"\\u0001" * 512 in raw for raw in self.journals(coordinator).values()))
        required = {"worker_registered", "claim_granted", "claim_renewed", "message_posted",
                    "artifact_published", "cancel_requested", "cancel_acknowledged",
                    "task_cancelled", "claim_indeterminate", "retry_authorized",
                    "task_blocked", "task_failed", "task_completed", "claim_released"}
        self.assertTrue(required <= coverage, required - coverage)

    def test_complete_line_fsync_uncertainty_reconciles_without_duplicate_event(self):
        coordinator, _task, a, b = self.fixture("uncertain")
        frame = self.message_frame(coordinator, a, b, "uncertain-message")
        original = os.fsync
        sync_paths = []

        def target_file(fd):
            name = _final_open_path(fd)
            if name is None or not Path(name).name.startswith("journal-g"):
                return False
            return b'"message_id":"uncertain-message"' in Path(name).read_bytes()

        def fail_target(fd):
            if target_file(fd):
                raise OSError("synthetic journal fsync failure")
            return original(fd)

        with mock.patch.object(swarm.os, "fsync", side_effect=fail_target):
            with self.assertRaises(rd.JournalWriteError) as caught:
                coordinator.apply_frame(frame, worker_id=a)
        self.assertEqual(caught.exception.durability, "unknown")
        self.assertEqual(caught.exception.phase, "fsync")
        self.assertEqual(self.event_count(coordinator, "uncertain-message"), 1)
        complete_line = self.journals(coordinator)
        reopened = swarm.SwarmCoordinator(coordinator.runs_root, coordinator.run_id, clock=self.clock)
        with mock.patch.object(swarm.os, "fsync", side_effect=fail_target):
            with self.assertRaises(swarm.SwarmCoordinatorError):
                reopened.apply_frame(frame, worker_id=a)
        self.assertEqual(self.journals(reopened), complete_line)

        def successful_sync(fd):
            matches = target_file(fd)
            result = original(fd)
            if matches:
                sync_paths.append(True)
            return result

        with mock.patch.object(swarm.os, "fsync", side_effect=successful_sync):
            self.assertEqual(reopened.apply_frame(frame, worker_id=a)["status"], "posted")
        self.assertTrue(sync_paths)
        self.assertEqual(self.journals(reopened), complete_line)
        self.assertEqual(self.event_count(reopened, "uncertain-message"), 1)

    def test_two_process_last_claim_slot_preserves_winner_settlement(self):
        coordinator = swarm.SwarmCoordinator.create(
            self.root / "race", "race-1", project_root_sha256="a" * 64,
            roster_definition_sha256="b" * 64,
            tasks=[{"task_id": task, "request_sha256": REQUEST}
                   for task in ("task-a", "task-b")], clock=self.clock)
        frames = []
        for suffix in ("a", "b"):
            coordinator.register_worker("worker-" + suffix, worker_instance_id="instance-" + suffix)
            frames.append(make_frame(
                "claim", run_id=coordinator.run_id, message_id="race-claim-" + suffix,
                sent_at_ms=int(STAMP * 1000),
                payload={"task_id": "task-" + suffix, "claim_id": "claim-" + suffix,
                         "attempt": 1, "lease_generation": 1,
                         "lease_expires_at_ms": int(STAMP * 1000) + 30_000,
                         "request_sha256": REQUEST}))
        # Measure both alternatives through real public commands. Symmetric
        # identifiers make either winner consume precisely the same budget.
        capacities = []
        for index, frame in enumerate(frames):
            calibration = self.clone(coordinator, "race-calibration-" + str(index))
            calibration.apply_frame(frame, worker_id="worker-" + "ab"[index])
            capacities.append(self.used(calibration) + self.reserve(calibration))
        self.assertEqual(capacities[0], capacities[1])
        cap = capacities[0]
        gate = self.root / "race-go"
        ready = [self.root / ("race-ready-" + str(index)) for index in range(2)]
        code = '''import json, pathlib, sys, time
import _rundir as rd
import _swarm_coordinator as swarm
root, frame_json, cap, gate, ready, worker, stamp = sys.argv[1:]
stamp = float(stamp)
time.time = lambda: stamp
swarm.MAX_JOURNAL_BYTES = int(cap)
frame = json.loads(frame_json)
coordinator = swarm.SwarmCoordinator(root, frame["run_id"], clock=lambda: stamp)
pathlib.Path(ready).write_text("ready", encoding="ascii")
deadline = time.monotonic() + 10
while not pathlib.Path(gate).exists():
    if time.monotonic() >= deadline: raise SystemExit(3)
    time.sleep(0.005)
try:
    response = coordinator.apply_frame(frame, worker_id=worker)
    result = {"outcome": "claimed", "response": response}
except rd.OwnerHeldError:
    result = {"outcome": "owner_held"}
except swarm.SwarmCoordinatorError as exc:
    result = {"outcome": "capacity" if "capacity" in str(exc) else "error",
              "error_type": type(exc).__name__}
except Exception as exc:
    result = {"outcome": "error", "error_type": type(exc).__name__}
print(json.dumps(result))
'''
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", SUMMON_TELEMETRY="0",
                   PYTHONIOENCODING="utf-8", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
        processes = []
        try:
            for index, frame in enumerate(frames):
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", code, coordinator.runs_root,
                     json.dumps(frame), str(cap), str(gate), str(ready[index]),
                     "worker-" + "ab"[index], str(STAMP)],
                    cwd=str(SCRIPTS), env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, **popen_flags()))
            deadline = time.monotonic() + 10
            while not all(path.exists() for path in ready):
                self.assertTrue(all(process.poll() is None for process in processes),
                                "race child exited before the common barrier")
                self.assertLess(time.monotonic(), deadline, "race readiness timed out")
                time.sleep(0.01)
            self.assertTrue(all(process.poll() is None for process in processes))
            gate.write_text("go", encoding="ascii")
            results = []
            for process in processes:
                output, error = process.communicate(timeout=20)
                self.assertEqual(process.returncode, 0, "race child failed")
                self.assertEqual(error, "", "race child emitted a private diagnostic")
                results.append(json.loads(output))
            outcomes = [result["outcome"] for result in results]
            self.assertEqual(outcomes.count("claimed"), 1, outcomes)
            winner = outcomes.index("claimed")
            loser = 1 - winner
            self.assertIn(outcomes[loser], {"owner_held", "capacity"})
            self.assertEqual(sum(r["event"] == "claim_granted"
                                 for r in self.records(coordinator)), 1)
            self.assertLessEqual(self.used(coordinator) + self.reserve(coordinator), cap)
            before_retry = self.journals(coordinator)
            reopened = swarm.SwarmCoordinator(coordinator.runs_root, coordinator.run_id, clock=self.clock)
            with mock.patch.object(swarm, "MAX_JOURNAL_BYTES", cap):
                # Both processes have exited and released ownership. This is
                # an explicit operation retry, not a hidden provider retry.
                with self.assertRaisesRegex(swarm.SwarmCoordinatorError, "capacity"):
                    reopened.apply_frame(frames[loser], worker_id="worker-" + "ab"[loser])
                self.assertEqual(self.journals(reopened), before_retry)
                claim = results[winner]["response"]
                reopened.complete(
                    "worker-" + "ab"[winner], task_id="task-" + "ab"[winner],
                    claim_id=claim["claim_id"], attempt=claim["attempt"],
                    lease_generation=claim["lease_generation"], request_sha256=REQUEST,
                    envelope_sha256="e" * 64, message_id="winner-complete")
                self.assertLessEqual(self.used(reopened) + self.reserve(reopened), cap)
            replayed = swarm.SwarmCoordinator(coordinator.runs_root, coordinator.run_id, clock=self.clock)
            state, torn = replayed._load()
            self.assertFalse(torn)
            self.assertEqual(state["tasks"]["task-" + "ab"[winner]]["terminal"], "completed")
            self.assertEqual(state["tasks"]["task-" + "ab"[loser]]["attempts"], [])
            self.assertEqual(len(state["claims"]), 1)
            self.assert_bounds(replayed)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

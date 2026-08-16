from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _conversation import ConversationJournal
from _conversation_runtime import (ConversationRuntime, ConversationRuntimeError,
                                    _pid_alive, _process_birth_token,
                                    _record_process_live, _record_process_state,
                                    _kill_pid_tree)


class ConversationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project = self.base / "project"
        self.project.mkdir()
        self.root = self.base / "rooms"
        self.roster = self.base / "agents"
        self.roster.mkdir()
        (self.roster / "worker.md").write_text(
            "---\nrun-agent: codex\nmodel: fake-model\npermission: read-only\n---\n\n"
            "A deterministic test participant.\n", encoding="utf-8")
        (self.roster / "worker2.md").write_text(
            "---\nrun-agent: codex\nmodel: fake-model-2\npermission: read-only\n---\n\n"
            "A second deterministic test participant.\n", encoding="utf-8")
        self.marker = self.base / "calls.jsonl"
        self.dispatcher = self.base / "fake_dispatcher.py"
        self.dispatcher.write_text(
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "marker = os.environ.get('FAKE_MARKER')\n"
            "if marker:\n"
            "    with open(marker, 'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps(args) + '\\n')\n"
            "resumed = '--resume' in args\n"
            "result = os.environ.get('FAKE_RESULT') or ('resumed answer' if resumed else 'first answer')\n"
            "print(json.dumps({'status':'success','result':result,"
            "'model':{'served':'fake-model'},'resume':{'session_id':'provider-session-1',"
            "'profile':'fake-profile'}}))\n",
            encoding="utf-8",
        )
        self.room = ConversationJournal.create(
            self.root, session_id="room-1", project_id="demo", project_root=self.project,
            initiator_host="codex", initiator_agent="human", mode="chat",
            participants=[{"agent": "worker", "role": "researcher", "name": "Worker", "version": "1"},
                          {"agent": "worker2", "role": "reviewer", "name": "Worker 2", "version": "1"}],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _runtime(self):
        return ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=self.dispatcher, timeout_ms=10_000, strict_agents_dir=True,
        )

    def _calls(self):
        if not self.marker.exists():
            return []
        return [json.loads(line) for line in self.marker.read_text(encoding="utf-8").splitlines()]

    def test_pid_probe_rejects_a_child_after_it_exits(self):
        """Recovery must not treat a naturally exited provider as live.

        This is deliberately cross-platform: Windows needs the explicit
        GetExitCodeProcess(STILL_ACTIVE) check, while POSIX uses kill(pid, 0).
        """
        child = subprocess.Popen([sys.executable, "-c", "pass"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        pid = child.pid
        self.assertTrue(_pid_alive(pid))
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertFalse(_pid_alive(pid))

    def test_process_record_birth_token_rejects_pid_reuse_metadata(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        try:
            token = _process_birth_token(child.pid)
            self.assertIsInstance(token, str)
            self.assertTrue(_record_process_live({"pid": child.pid,
                                                  "pid_start_token": token}))
            self.assertFalse(_record_process_live({"pid": child.pid,
                                                   "pid_start_token": "wrong-start"}))
            self.assertEqual(_record_process_state({"pid": child.pid,
                                                    "pid_start_token": None}), "unknown")
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_cross_process_kill_refuses_a_birth_token_mismatch(self):
        """A stale owner cannot taskkill a replacement PID."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            with patch("_conversation_runtime.subprocess.run") as taskkill:
                self.assertFalse(_kill_pid_tree(child.pid, expected_start_token="wrong-start"))
                taskkill.assert_not_called()
            self.assertTrue(_pid_alive(child.pid))
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_process_group_or_job_reaps_descendant_after_leader_exit(self):
        dispatcher = self.base / "leader_exits_dispatcher.py"
        marker = self.base / "descendant-pid.txt"
        dispatcher.write_text(
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
            f"open({str(marker)!r}, 'w', encoding='ascii').write(str(child.pid))\n"
            "time.sleep(0.2)\n",
            encoding="utf-8",
        )
        runtime = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=dispatcher, timeout_ms=8_000, strict_agents_dir=True,
        )
        try:
            runtime.start_turn("room-1", "worker", "reap descendants", wait=False)
            deadline = __import__("time").monotonic() + 5
            child_pid = None
            while __import__("time").monotonic() < deadline:
                if marker.exists():
                    child_pid = int(marker.read_text(encoding="ascii"))
                    break
                __import__("time").sleep(0.05)
            self.assertIsNotNone(child_pid)
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline and _pid_alive(child_pid):
                __import__("time").sleep(0.05)
            self.assertFalse(_pid_alive(child_pid))
        finally:
            runtime.close(timeout=5)

    def test_runtime_rejects_symlinked_conversation_root(self):
        link = self.base / "rooms-link"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ConversationRuntimeError):
            ConversationRuntime(link, cwd=self.project, agents_dir=self.roster,
                                dispatcher=self.dispatcher, strict_agents_dir=True)

    def test_runtime_rejects_symlinked_chat_runtime_parent(self):
        outside = self.base / "runtime-outside"
        outside.mkdir()
        link = self.root / ".chat-runtime"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        runtime = self._runtime()
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "worker", "must not follow parent link", wait=True)
        self.assertEqual(list(outside.rglob("*")), [])
        self.assertFalse(self.marker.exists())

    def test_runtime_rejects_symlinked_session_and_participant_dirs(self):
        runtime = self._runtime()
        runtime_dir = self.root / ".chat-runtime"
        runtime_dir.mkdir()
        outside_session = self.base / "session-outside"
        outside_session.mkdir()
        session_link = runtime_dir / "room-1"
        try:
            session_link.symlink_to(outside_session, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "worker", "must not follow session link", wait=True)
        self.assertEqual(list(outside_session.rglob("*")), [])
        session_link.unlink()
        session_link.mkdir()
        outside_participant = self.base / "participant-outside"
        outside_participant.mkdir()
        participant_link = session_link / "worker"
        try:
            participant_link.symlink_to(outside_participant, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "worker", "must not follow participant link", wait=True)
        self.assertEqual(list(outside_participant.rglob("*")), [])
        self.assertFalse(self.marker.exists())

    def test_durable_turn_and_same_provider_session_resume(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            first = runtime.start_turn("room-1", "worker", "first question", wait=True)
            second = runtime.start_turn("room-1", "worker", "follow up", wait=True)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        calls = self._calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--resume", calls[0])
        self.assertIn("--transport", calls[0])
        self.assertEqual(calls[0][calls[0].index("--transport") + 1], "subprocess")
        self.assertIn("--max-permission", calls[0])
        self.assertEqual(calls[0][calls[0].index("--max-permission") + 1], "read-only")
        self.assertIn("--resume", calls[1])
        self.assertIn("provider-session-1", calls[1])
        process_records = list((self.root / ".chat-runtime" / "room-1").glob("*/turn-*.json"))
        self.assertEqual(len(process_records), 2)
        self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["state"] == "terminal"
                             for path in process_records))
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        self.assertEqual([event["event"] for event in events], [
            "session_created", "turn_started", "message_posted", "turn_finished",
            "turn_started", "message_posted", "turn_finished",
        ])
        public = ConversationJournal.open(self.root, "room-1").as_dict()
        self.assertIn("first answer", json.dumps(public))
        self.assertNotIn("provider-session-1", json.dumps(public))

    def test_cross_runtime_turn_claim_is_compare_and_swap(self):
        """Two runtimes may observe the same cursor, but only one may launch."""
        left = self._runtime()
        right = self._runtime()
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(runtime.start_turn, "room-1", "worker", prompt, wait=True)
                    for runtime, prompt in ((left, "left"), (right, "right"))
                ]
                outcomes = []
                for future in futures:
                    try:
                        outcomes.append(future.result())
                    except ConversationRuntimeError as exc:
                        outcomes.append({"status": "conflict", "error": str(exc)})

        self.assertEqual(sum(outcome.get("status") == "success" for outcome in outcomes), 1)
        self.assertEqual(sum(outcome.get("status") == "conflict" for outcome in outcomes), 1)
        self.assertEqual(len(self._calls()), 1)

    def test_different_participants_can_run_in_parallel(self):
        slow = self.base / "parallel_dispatcher.py"
        slow.write_text(
            "import json, time\n"
            "time.sleep(0.6)\n"
            "print(json.dumps({'status':'success','result':'parallel',"
            "'model':{'served':'fake-model'},'resume':{}}))\n",
            encoding="utf-8")
        left = ConversationRuntime(self.root, cwd=self.project, agents_dir=self.roster,
                                   dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True)
        right = ConversationRuntime(self.root, cwd=self.project, agents_dir=self.roster,
                                    dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True)
        first = left.start_turn("room-1", "worker", "parallel one", wait=False)
        second = right.start_turn("room-1", "worker2", "parallel two", wait=False)
        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "started")
        deadline = __import__("time").monotonic() + 8
        while __import__("time").monotonic() < deadline:
            events = ConversationJournal.open(self.root, "room-1").events(native=True)
            finished = [event for event in events if event["event"] == "turn_finished"]
            if len(finished) == 2:
                break
            __import__("time").sleep(0.05)
        self.assertEqual(len(finished), 2)

    def test_definition_drift_creates_explicit_fork_without_provider_call(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(runtime.start_turn("room-1", "worker", "first", wait=True)["status"], "success")
            (self.roster / "worker.md").write_text(
                "---\nrun-agent: codex\nmodel: changed-model\npermission: read-only\n---\n\nChanged.\n",
                encoding="utf-8",
            )
            fork = runtime.start_turn("room-1", "worker", "continue", wait=True)
        self.assertEqual(fork["status"], "forked")
        self.assertTrue(fork["prompt_preserved"])
        self.assertEqual(len(self._calls()), 1)
        self.assertIn("room-1-fork-", fork["session_id"])
        parent = ConversationJournal.open(self.root, "room-1").as_dict()
        self.assertIn("fork_created", {event["event"] for event in parent["events"]})
        child = ConversationJournal.open(self.root, fork["session_id"])
        child_native = json.dumps(child.events(native=True))
        self.assertIn("continue", child_native)

    def test_resume_handle_without_identity_forks_instead_of_starting_fresh(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(runtime.start_turn("room-1", "worker", "first", wait=True)["status"], "success")
            path = self.root / "room-1.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                record = json.loads(line)
                if record.get("event") == "turn_finished":
                    record["payload"].pop("identity", None)
                    record["payload_sha256"] = __import__("_conversation")._sha256(record["payload"])
                    lines[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    break
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            fork = runtime.start_turn("room-1", "worker", "continue", wait=True)
        self.assertEqual(fork["status"], "forked")
        self.assertIn("missing continuation identity", fork["reason"])
        self.assertEqual(len(self._calls()), 1)

    def test_unmatched_turn_is_refused_before_second_provider_call(self):
        runtime = self._runtime()
        journal = ConversationJournal.open(self.root, "room-1")
        journal.append("turn_started", "system", "summon", {
            "turn_id": "turn-stuck", "participant": "worker", "prompt_sha256": "a" * 64,
            "prompt_chars": 4, "provider": "codex", "model_target": "fake-model",
            "permission": "read-only", "transport": "subprocess", "resumed": False,
        })
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "worker", "retry", wait=True)
        self.assertFalse(self.marker.exists())

    def test_turn_requires_explicit_room_membership_before_provider_work(self):
        runtime = self._runtime()
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "not-a-member", "do not run", wait=True)
        self.assertFalse(self.marker.exists())

    def test_chat_permission_ceiling_clamps_a_yolo_room_member(self):
        ConversationJournal.create(
            self.root, session_id="room-yolo", project_id="demo",
            project_root=self.project, initiator_host="codex",
            initiator_agent="human", mode="chat",
            participants=[{"agent": "worker", "role": "researcher",
                            "name": "Worker", "version": "1",
                            "permission": "yolo"}],
        )
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            result = self._runtime().start_turn("room-yolo", "worker", "bounded", wait=True)
        self.assertEqual(result["status"], "success")
        calls = self._calls()
        self.assertEqual(calls[0][calls[0].index("--max-permission") + 1], "read-only")
        finish = next(event for event in ConversationJournal.open(
            self.root, "room-yolo").events(native=True)
                       if event["event"] == "turn_finished")
        self.assertEqual(finish["payload"]["identity"]["permission"], "read-only")

    def test_definition_drift_after_durable_start_is_fenced_before_provider(self):
        runtime = self._runtime()
        original_identity = runtime._identity
        calls = 0

        def drift_on_final_read(journal, participant):
            nonlocal calls
            calls += 1
            if calls == 2:
                (self.roster / "worker.md").write_text(
                    "---\nrun-agent: codex\nmodel: changed-after-start\n"
                    "permission: read-only\n---\n\nChanged after start.\n",
                    encoding="utf-8")
            return original_identity(journal, participant)

        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}), \
                patch.object(runtime, "_identity", side_effect=drift_on_final_read):
            result = runtime.start_turn("room-1", "worker", "fence me", wait=True)
        self.assertEqual(result["status"], "error")
        self.assertFalse(self.marker.exists())
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        finish = next(event for event in events if event["event"] == "turn_finished")
        self.assertEqual(finish["payload"]["status"], "error")
        self.assertEqual(len([event for event in events if event["event"] == "message_posted"]), 0)

    def test_recovery_requires_attestation_and_allows_a_fresh_non_resumed_turn(self):
        runtime = self._runtime()
        journal = ConversationJournal.open(self.root, "room-1")
        journal.append("turn_started", "system", "summon", {
            "turn_id": "turn-stuck", "participant": "worker", "prompt_sha256": "a" * 64,
            "prompt_chars": 4, "provider": "codex", "model_target": "fake-model",
            "permission": "read-only", "transport": "subprocess", "resumed": False,
        })
        with self.assertRaises(ConversationRuntimeError):
            runtime.recover_turn("room-1", "worker")
        recovered = runtime.recover_turn("room-1", "worker", confirm=True)
        self.assertEqual(recovered["status"], "recovered")
        self.assertEqual(recovered["recovery_kind"], "human_attested_indeterminate")
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            fresh = runtime.start_turn("room-1", "worker", "fresh after review", wait=True)
        self.assertEqual(fresh["status"], "success")
        self.assertEqual(len(self._calls()), 1)
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        finishes = [event for event in events if event["event"] == "turn_finished"]
        self.assertEqual(finishes[0]["payload"]["status"], "blocked")
        self.assertEqual(finishes[-1]["payload"]["status"], "success")

    def test_cancel_kills_dispatcher_tree_and_journals_cancelled(self):
        slow = self.base / "slow_dispatcher.py"
        slow.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        runtime = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        started = runtime.start_turn("room-1", "worker", "cancel me", wait=False)
        self.assertEqual(started["status"], "started")
        runtime.cancel_turn("room-1", "worker")
        deadline = __import__("time").monotonic() + 5
        finished = None
        while __import__("time").monotonic() < deadline:
            events = ConversationJournal.open(self.root, "room-1").events(native=True)
            finished = next((event for event in events
                             if event["event"] == "turn_finished"), None)
            if finished:
                break
            __import__("time").sleep(0.05)
        self.assertIsNotNone(finished)
        self.assertEqual(finished["payload"]["status"], "cancelled")

    def test_runtime_close_cancels_and_joins_active_turn(self):
        slow = self.base / "surface_close_dispatcher.py"
        slow.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        runtime = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        runtime.start_turn("room-1", "worker", "close the surface", wait=False)
        # Cold Windows Python process creation can exceed three seconds when
        # the full suite is running beside browser/ACP tests.  The lifecycle
        # contract is bounded, but the assertion must not turn scheduler
        # contention into a false reliability failure.
        deadline = __import__("time").monotonic() + 8
        while __import__("time").monotonic() < deadline:
            if runtime._active:
                break
            __import__("time").sleep(0.05)
        runtime.close(timeout=5)
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        finished = [event for event in events if event["event"] == "turn_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "cancelled")
        with self.assertRaises(ConversationRuntimeError):
            runtime.start_turn("room-1", "worker", "must remain closed", wait=True)

    def test_cancel_is_durable_across_runtime_instances(self):
        slow = self.base / "durable_cancel_dispatcher.py"
        slow.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        left = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        right = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        started = left.start_turn("room-1", "worker", "cancel from another runtime", wait=False)
        self.assertEqual(started["status"], "started")
        requested = right.cancel_turn("room-1", "worker")
        self.assertTrue(requested["durable"])
        self.assertFalse(requested["local_worker"])
        deadline = __import__("time").monotonic() + 5
        finished = None
        while __import__("time").monotonic() < deadline:
            events = ConversationJournal.open(self.root, "room-1").events(native=True)
            finished = next((event for event in events
                             if event["event"] == "turn_finished"), None)
            if finished:
                break
            __import__("time").sleep(0.05)
        self.assertIsNotNone(finished)
        self.assertEqual(finished["payload"]["status"], "cancelled")
        self.assertTrue(any(event["event"] == "turn_cancel_requested"
                             for event in ConversationJournal.open(self.root, "room-1").events(native=True)))

    def test_recovery_refuses_a_live_provider_process(self):
        slow = self.base / "live_recovery_dispatcher.py"
        slow.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        left = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        right = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        try:
            left.start_turn("room-1", "worker", "do not recover while live", wait=False)
            with self.assertRaises(ConversationRuntimeError) as refused:
                right.recover_turn("room-1", "worker", confirm=True)
            self.assertIn("still", str(refused.exception))
            right.cancel_turn("room-1", "worker")
            deadline = __import__("time").monotonic() + 5
            while __import__("time").monotonic() < deadline:
                events = ConversationJournal.open(self.root, "room-1").events(native=True)
                if any(event["event"] == "turn_finished" for event in events):
                    break
                __import__("time").sleep(0.05)
            else:
                self.fail("live provider did not finish after durable cancellation")
        finally:
            # The worker is intentionally asynchronous in this test.  Close
            # both runtimes before TemporaryDirectory teardown so Windows does
            # not retain an open journal handle.
            left.close(timeout=5)
            right.close(timeout=5)

    def test_expired_owner_cancel_kills_recorded_orphan_tree(self):
        slow = self.base / "orphan_dispatcher.py"
        slow.write_text("import time; time.sleep(20)\n", encoding="utf-8")
        left = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        right = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=slow, timeout_ms=8_000, strict_agents_dir=True,
        )
        left.start_turn("room-1", "worker", "orphan me", wait=False)
        runtime_dir = self.root / ".chat-runtime" / "room-1" / "worker"
        deadline = __import__("time").monotonic() + 3
        record = None
        while __import__("time").monotonic() < deadline:
            records = list(runtime_dir.glob("turn-*.json"))
            if records:
                candidate = json.loads(records[0].read_text(encoding="utf-8"))
                if candidate.get("state") == "running":
                    record = candidate
                    break
            __import__("time").sleep(0.05)
        self.assertIsNotNone(record)
        lock = runtime_dir / "owner.lock"
        owner_data = json.loads(lock.read_text(encoding="utf-8"))
        owner_data["lease_expires"] = 0
        lock.write_text(json.dumps(owner_data), encoding="utf-8")
        for sidecar in runtime_dir.glob("lease-*.json"):
            side = json.loads(sidecar.read_text(encoding="utf-8"))
            side["lease_expires"] = 0
            sidecar.write_text(json.dumps(side), encoding="utf-8")
        requested = right.cancel_turn("room-1", "worker")
        self.assertTrue(requested["process_killed"])
        deadline = __import__("time").monotonic() + 5
        while __import__("time").monotonic() < deadline:
            events = ConversationJournal.open(self.root, "room-1").events(native=True)
            if any(event["event"] == "turn_finished" for event in events):
                return
            __import__("time").sleep(0.05)
        self.fail("orphan provider did not finish after process-tree kill")

    def test_provider_output_is_redacted_in_public_projection(self):
        secret = ("SECRET_TOKEN=top-secret C:\\private folder\\repo "
                  "sk-1234567890abcdef ghp_1234567890abcdef AKIA1234567890ABCDEF "
                  "eyJaaaaaaaaaaaaaaaaaaaa.bbbbbbbb.cccccccc")
        with patch.dict(os.environ, {"FAKE_RESULT": secret}):
            result = self._runtime().start_turn("room-1", "worker", "show result", wait=True)
        self.assertEqual(result["status"], "success")
        public = json.dumps(ConversationJournal.open(self.root, "room-1").as_dict())
        self.assertNotIn("top-secret", public)
        self.assertNotIn("private folder", public)
        self.assertNotIn("1234567890abcdef", public)
        self.assertNotIn("AKIA1234567890ABCDEF", public)
        self.assertNotIn("eyJaaaaaaaaaaaaaaaaaaaa", public)
        self.assertIn("[redacted]", public)

    def test_hostile_served_model_still_closes_turn_with_bounded_error(self):
        hostile = self.base / "hostile_served.py"
        hostile.write_text(
            "import json\n"
            "print(json.dumps({'status':'success','result':'bounded answer',"
            "'model':{'served':'C:\\\\private\\\\provider'},"
            "'resume':{'session_id':'session-safe','profile':'profile-safe'}}))\n",
            encoding="utf-8",
        )
        result = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=hostile, timeout_ms=10_000, strict_agents_dir=True,
        ).start_turn("room-1", "worker", "hostile identity", wait=True)
        self.assertEqual(result["status"], "error")
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        finished = [event for event in events if event["event"] == "turn_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["payload"]["status"], "error")
        public = json.dumps(ConversationJournal.open(self.root, "room-1").as_dict())
        self.assertNotIn("private", public)
        self.assertIn("provider model identity refused", public)


if __name__ == "__main__":
    unittest.main()

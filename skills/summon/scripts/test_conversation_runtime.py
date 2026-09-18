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
from _chat_resume import build as build_refusal
from _resume_capabilities import resume_capability
from _spawn import popen_flags
from _conversation_runtime import (ConversationRuntime, ConversationRuntimeError,
                                    _pid_alive, _process_birth_token,
                                    _read_process_record,
                                    _record_process_live, _record_process_state,
                                    _kill_pid_tree)
import _chat_launch_qualification
import _chat_source_family
import _launch_qualification


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
            "---\nrun-agent: claude\nmodel: fake-model\npermission: read-only\n---\n\n"
            "A deterministic test participant.\n", encoding="utf-8")
        (self.roster / "worker2.md").write_text(
            "---\nrun-agent: codex\nmodel: fake-model-2\npermission: read-only\n---\n\n"
            "A second deterministic test participant.\n", encoding="utf-8")
        (self.roster / "candidate.md").write_text(
            "---\nrun-agent: codex\nmodel: candidate-model\npermission: read-only\n---\n\n"
            "A candidate continuation participant.\n", encoding="utf-8")
        (self.roster / "agy.md").write_text(
            "---\nrun-agent: agy\nmodel: agy-model\npermission: read-only\n---\n\n"
            "An unsupported continuation participant.\n", encoding="utf-8")
        (self.roster / "acp-candidate.md").write_text(
            "---\nrun-agent: codex\nmodel: acp-model\ntransport: acp\n"
            "permission: read-only\n---\n\nAn unsupported transport participant.\n",
            encoding="utf-8")
        self.marker = self.base / "calls.jsonl"
        self.dispatcher = self.base / "fake_dispatcher.py"
        self.dispatcher.write_text(
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "marker = os.environ.get('FAKE_MARKER')\n"
            "if marker:\n"
            "    with open(marker, 'a', encoding='utf-8') as fh:\n"
            "        fh.write(json.dumps(args) + '\\n')\n"
            "guard = os.environ.get('SUMMON_CHAT_LAUNCH_GUARD_PATH')\n"
            "token = os.environ.get('SUMMON_CHAT_LAUNCH_GUARD_TOKEN')\n"
            "if guard and token:\n"
            "    sys.path.insert(0, " + repr(str(HERE)) + ")\n"
            "    from _launch_binding import observation\n"
            "    import _chat_launch_guard\n"
            "    _obs = observation(sys.executable, [__file__], os.getcwd(), os.environ, backend='claude', transport='subprocess')\n"
            "    _backend = os.environ.get('SUMMON_CHAT_LAUNCH_BACKEND', 'claude')\n"
            "    _transport = os.environ.get('SUMMON_CHAT_LAUNCH_TRANSPORT', 'subprocess')\n"
            "    _obs = observation(sys.executable, [__file__], os.getcwd(), os.environ, backend=_backend, transport=_transport, external_cli_version='fake-cli/1.0')\n"
            "    _evidence = {'schema':'summon.fleet-launch-evidence/v1', 'backend':_backend, 'transport':_transport, 'launch_observation':_obs}\n"
            "    _prompt = args[args.index('--prompt') + 1] if '--prompt' in args and args.index('--prompt') + 1 < len(args) else ''\n"
            "    import hashlib\n"
            "    _evidence['dispatch_payload_sha256'] = hashlib.sha256(_prompt.encode('utf-8')).hexdigest()\n"
            "    _attempt = os.environ.get('SUMMON_CHAT_LAUNCH_ATTEMPT_ID', '')\n"
            "    _evidence['attempt_id_sha256'] = hashlib.sha256(_attempt.encode('utf-8')).hexdigest()\n"
            "    _qualification = os.environ.get('SUMMON_CHAT_LAUNCH_QUALIFICATION')\n"
            "    if _qualification:\n"
            "        _evidence['launch_qualification'] = json.loads(_qualification)\n"
             "    _before = (_chat_launch_guard.before_launch_v2 if os.environ.get('SUMMON_CHAT_LAUNCH_GUARD_VERSION') == '2' else _chat_launch_guard.before_launch)\n"
             "    _before(guard, token, _evidence, backend=_backend, transport=_transport, attempt_id=os.environ.get('SUMMON_CHAT_LAUNCH_ATTEMPT_ID', ''), qualification_token=os.environ.get('SUMMON_CHAT_QUALIFICATION_TOKEN'), source_family_path=os.environ.get('SUMMON_CHAT_SOURCE_FAMILY_PATH'))\n"
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
        runtime = ConversationRuntime(
            self.root, cwd=self.project, agents_dir=self.roster,
            dispatcher=self.dispatcher, timeout_ms=10_000, strict_agents_dir=True,
        )
        # Existing chat-runtime compatibility tests use a synthetic trusted
        # adapter.  Explicitly perform the same non-launching qualification
        # operation that a real revalidator would perform after each successful
        # synthetic turn; production ConversationRuntime never auto-signs this.
        original_start = runtime.start_turn
        def start_with_trusted_revalidation(*args, **kwargs):
            result = original_start(*args, **kwargs)
            if kwargs.get("wait", False) and isinstance(result, dict) \
                    and result.get("status") == "success":
                self._trust_last_chat_turn(runtime, args[0], args[1])
            return result
        runtime.start_turn = start_with_trusted_revalidation
        return runtime

    def _trust_last_chat_turn(self, runtime, session_id, participant):
        journal = ConversationJournal.open(self.root, session_id)
        start, finish = runtime._last_turn(journal.events(native=True), participant)
        if not start or not finish:
            return
        observation = finish.get("payload", {}).get("launch_observation")
        turn_id = start.get("payload", {}).get("turn_id")
        if not isinstance(observation, dict) or not isinstance(turn_id, str):
            return
        owner_dir = runtime._runtime_owner_dir(session_id, participant, create=False)
        family_path = _chat_source_family.family_path(owner_dir)
        family = _chat_source_family.read(family_path)
        common = {
            "backend": observation["backend"], "transport": observation["transport"],
            "registry_generation": observation["registry_generation"],
            "registry_digest": observation["registry_digest"],
            "adapter": observation["adapter"],
            "adapter_version": observation["adapter_version"],
            "external_cli_version": observation["external_cli_version"],
            "material_contract": _launch_qualification.material_contract_for(
                backend=observation["backend"], transport=observation["transport"],
                adapter=observation["adapter"], adapter_version=observation["adapter_version"]),
            "executable_sha256": observation["executable_sha256"],
            "launch_material_sha256": observation["launch_material_sha256"],
        }
        # The compatibility fixture intentionally covers candidate/unsupported
        # routes too. Those routes must retain their capability refusal and do
        # not have a continuation qualification to consume.
        if common["material_contract"] is None:
            return
        common["operation"] = "chat_resume"
        common["revocation_id"] = _launch_qualification.revocation_id_for(common)
        qualification = _chat_launch_qualification.issue(
            source_family_id=family["source_family_id"], turn_id=turn_id,
            expires_at=__import__("time").time() + 1800,
            observation=observation, token=family["nonce"],
            **{key: value for key, value in common.items() if key != "operation"})
        runtime.revalidate_chat_source(
            session_id, participant, turn_id=turn_id,
            observation=observation, qualification=qualification)

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
                                 stderr=subprocess.DEVNULL, **popen_flags())
        pid = child.pid
        self.assertTrue(_pid_alive(pid))
        self.assertEqual(child.wait(timeout=10), 0)
        self.assertFalse(_pid_alive(pid))

    def test_process_record_birth_token_rejects_pid_reuse_metadata(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **popen_flags())
        try:
            token = _process_birth_token(child.pid)
            self.assertIsInstance(token, str)
            self.assertTrue(_record_process_live({"pid": child.pid,
                                                  "pid_start_token": token}))
            self.assertFalse(_record_process_live({"pid": child.pid,
                                                   "pid_start_token": "wrong-start"}))
            self.assertEqual(_record_process_state({"pid": child.pid,
                                                    "pid_start_token": None}), "unknown")
            self.assertEqual(_record_process_state({"pid": child.pid}), "unknown")
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_cross_process_kill_refuses_a_birth_token_mismatch(self):
        """A stale owner cannot taskkill a replacement PID."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 **popen_flags())
        try:
            with patch("_conversation_runtime.subprocess.run") as taskkill:
                self.assertFalse(_kill_pid_tree(child.pid, expected_start_token="wrong-start"))
                taskkill.assert_not_called()
            self.assertTrue(_pid_alive(child.pid))
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_legacy_process_record_lookup_is_read_only_and_does_not_duplicate_scope(self):
        runtime = self._runtime()
        legacy_dir = Path(runtime._runtime_owner_dir("room-1"))
        record = {
            "schema_version": 1, "turn_id": "turn-legacy",
            "session_id": "room-1", "participant": "worker",
            "state": "terminal", "pid": None,
        }
        (legacy_dir / "turn-turn-legacy.json").write_text(
            json.dumps(record), encoding="utf-8")

        found, owner_dir = runtime._turn_process_record_with_owner(
            "room-1", "turn-legacy", "worker")
        self.assertEqual(found, record)
        self.assertEqual(Path(owner_dir), legacy_dir)
        # Inspecting an old session-wide record must not create the newer
        # participant-scoped directory or silently establish a second owner.
        self.assertFalse((legacy_dir / "worker").exists())
        self.assertEqual(list((self.root / ".chat-runtime").glob("room-1/*")),
                         [legacy_dir / "turn-turn-legacy.json"])

    def test_process_record_version_must_be_exact_integer_before_recovery_probe(self):
        path = self.base / "process-record.json"
        for value in (None, True, 1.0, 99):
            record = {"schema_version": value, "state": "running", "pid": 1}
            path.write_text(json.dumps(record), encoding="utf-8")
            with self.subTest(schema_version=value), self.assertRaises(ConversationRuntimeError):
                _read_process_record(str(path))
        path.write_text(json.dumps({"schema_version": 1, "state": "terminal", "pid": None}),
                        encoding="utf-8")
        self.assertEqual(_read_process_record(str(path))["schema_version"], 1)

    def test_process_group_or_job_reaps_descendant_after_leader_exit(self):
        dispatcher = self.base / "leader_exits_dispatcher.py"
        marker = self.base / "descendant-pid.txt"
        dispatcher.write_text(
            "import subprocess, sys, time\n"
            f"sys.path.insert(0, {str(HERE)!r})\n"
            "from _spawn import popen_flags\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'], **popen_flags(join_parent_group=True))\n"
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
                    marker_text = marker.read_text(encoding="ascii").strip()
                    if marker_text:
                        child_pid = int(marker_text)
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
            third = runtime.start_turn("room-1", "worker", "one more follow up", wait=True)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertEqual(third["status"], "success")
        calls = self._calls()
        self.assertEqual(len(calls), 3)
        self.assertNotIn("--resume", calls[0])
        self.assertIn("--transport", calls[0])
        self.assertEqual(calls[0][calls[0].index("--transport") + 1], "subprocess")
        self.assertIn("--max-permission", calls[0])
        self.assertEqual(calls[0][calls[0].index("--max-permission") + 1], "read-only")
        self.assertIn("--resume", calls[1])
        self.assertIn("provider-session-1", calls[1])
        self.assertIn("--resume", calls[2])
        self.assertIn("provider-session-1", calls[2])
        process_records = list((self.root / ".chat-runtime" / "room-1").glob("*/turn-*.json"))
        self.assertEqual(len(process_records), 3)
        self.assertTrue(all(json.loads(path.read_text(encoding="utf-8"))["state"] == "terminal"
                             for path in process_records))
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        self.assertEqual([event["event"] for event in events], [
            "session_created", "turn_started", "message_posted", "turn_finished",
            "turn_started", "message_posted", "turn_finished",
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
        # The journal boundary can be durable a few instructions before the
        # worker's final owner/process-record cleanup.  Explicitly close both
        # runtimes before TemporaryDirectory teardown so Windows cannot race a
        # late cleanup/read handle with shutil.rmtree (the product surface does
        # the same on browser shutdown).
        left.close(timeout=5)
        right.close(timeout=5)

    def test_participant_leases_do_not_regress_global_journal_generation(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(runtime.start_turn("room-1", "worker", "a1", wait=True)["status"], "success")
            self.assertEqual(runtime.start_turn("room-1", "worker", "a2", wait=True)["status"], "success")
            self.assertEqual(runtime.start_turn("room-1", "worker2", "b1", wait=True)["status"], "success")
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        generations = [event["generation"] for event in events]
        self.assertEqual(generations, sorted(generations))
        starts = [event for event in events if event["event"] == "turn_started"]
        self.assertEqual([event["payload"]["participant"] for event in starts],
                         ["worker", "worker", "worker2"])

    def test_definition_drift_returns_typed_refusal_without_provider_call(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(runtime.start_turn("room-1", "worker", "first", wait=True)["status"], "success")
            (self.roster / "worker.md").write_text(
                "---\nrun-agent: claude\nmodel: changed-model\npermission: read-only\n---\n\nChanged.\n",
                encoding="utf-8",
            )
            refusal = runtime.start_turn("room-1", "worker", "continue", wait=True)
        self.assertEqual(refusal["status"], "blocked")
        self.assertEqual(refusal["error_kind"], "chat_resume_refused")
        self.assertEqual(refusal["refusal"]["reason_code"], "continuation_identity_incompatible")
        self.assertEqual(refusal["refusal"]["attempts"], 0)
        self.assertFalse(refusal["refusal"]["provider_contacted"])
        self.assertEqual(len(self._calls()), 1)
        parent = ConversationJournal.open(self.root, "room-1").as_dict()
        self.assertNotIn("fork_created", {event["event"] for event in parent["events"]})

    def test_historical_v1_fork_creates_v2_child_without_parent_mutation(self):
        room = ConversationJournal.create(
            self.root, session_id="legacy-room", project_id="demo",
            project_root=self.project, initiator_host="codex",
            initiator_agent="human", mode="chat",
            participants=[{"agent": "worker", "role": "researcher"}],
        )
        path = self.root / "legacy-room.jsonl"
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(raw)
            record["schema_version"] = 1
            lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        legacy = ConversationJournal.open(self.root, "legacy-room")
        before = path.read_bytes()
        runtime = self._runtime()
        try:
            result = runtime.fork_turn(
                "legacy-room", "worker", "continue in a new room",
                reason="historical room requires a new mutable lineage")
            assert result["status"] == "forked"
            assert result["historical_parent"] is True
            assert result["parent_unchanged"] is True
            assert path.read_bytes() == before
            child = ConversationJournal.open(self.root, result["session_id"])
            assert child.schema_version == 2
            fork = next(event for event in child.events(native=True)
                        if event["event"] == "fork_created")
            assert fork["payload"]["parent_schema_version"] == 1
            assert fork["payload"]["lineage"] == "historical-read-only"
            assert any(event["event"] == "human_message"
                       and event["payload"]["text"] == "continue in a new room"
                       for event in child.events(native=True))
        finally:
            runtime.close(timeout=1)

    def test_post_claim_refuses_lower_actual_generation_without_dispatch_or_lock_leak(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(
                runtime.start_turn("room-1", "worker", "first", wait=True)["status"],
                "success",
            )
            path = self.root / "room-1.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                record = json.loads(line)
                if record.get("event") == "turn_finished":
                    record["payload"]["identity"]["owner_generation"] = 99
                    record["payload_sha256"] = __import__("_conversation")._sha256(
                        record["payload"])
                    lines[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    break
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            refused = runtime.start_turn("room-1", "worker", "stale owner", wait=True)
        self.assertEqual(refused["status"], "blocked")
        self.assertEqual(refused["error_kind"], "chat_resume_refused")
        self.assertEqual(refused["refusal"]["reason_code"],
                         "continuation_identity_incompatible")
        self.assertEqual(len(self._calls()), 1)
        owner_dir = Path(runtime._runtime_owner_dir("room-1", "worker"))
        self.assertFalse((owner_dir / "owner.lock").exists())

    def test_resume_handle_without_identity_returns_typed_refusal(self):
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
            refusal = runtime.start_turn("room-1", "worker", "continue", wait=True)
        self.assertEqual(refusal["status"], "blocked")
        self.assertEqual(refusal["error_kind"], "chat_resume_refused")
        self.assertEqual(refusal["refusal"]["reason_code"], "continuation_identity_missing")
        self.assertEqual(len(self._calls()), 1)

    def test_candidate_resume_refuses_before_popen(self):
        room = ConversationJournal.create(
            self.root, session_id="room-candidate", project_id="demo",
            project_root=self.project, initiator_host="codex", initiator_agent="human",
            mode="chat", participants=[{"agent": "candidate", "role": "candidate"}],
        )
        runtime = self._runtime()
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            self.assertEqual(runtime.start_turn("room-candidate", "candidate", "first", wait=True)["status"], "success")
            result = runtime.start_turn("room-candidate", "candidate", "continue", wait=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "chat_resume_refused")
        self.assertEqual(result["refusal"]["reason_code"], "resume_candidate")
        self.assertEqual(result["refusal"]["attempts"], 0)
        self.assertEqual(len(self._calls()), 1)
        self.assertEqual(ConversationJournal.open(self.root, "room-candidate").room.cursor, 4)

    def test_unsupported_agy_and_transport_resume_refuse_without_fresh_dispatch(self):
        cases = (("room-agy", "agy", "resume_unsupported"),
                 ("room-acp", "acp-candidate", "resume_unsupported"))
        runtime = self._runtime()
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            for session_id, participant, reason in cases:
                ConversationJournal.create(
                    self.root, session_id=session_id, project_id="demo",
                    project_root=self.project, initiator_host="codex",
                    initiator_agent="human", mode="chat",
                    participants=[{"agent": participant, "role": "reviewer"}],
                )
                first = runtime.start_turn(session_id, participant, "first", wait=True)
                refused = runtime.start_turn(session_id, participant, "continue", wait=True)
                self.assertEqual(first["status"], "success")
                self.assertEqual(refused["status"], "blocked")
                self.assertEqual(refused["error_kind"], "chat_resume_refused")
                self.assertEqual(refused["refusal"]["reason_code"], reason)
        self.assertEqual(len(self._calls()), 2)

    def test_cancel_race_preserves_pre_dispatch_refusal_and_cancel_event(self):
        runtime = self._runtime()
        entered = threading.Event()
        release = threading.Event()
        original_identity = runtime._identity
        reads = 0

        def gated_identity(journal, participant):
            nonlocal reads
            reads += 1
            if reads == 3:
                entered.set()
                self.assertTrue(release.wait(timeout=5))
                (self.roster / "worker.md").write_text(
                    "---\nrun-agent: codex\nmodel: candidate-model\n"
                    "permission: read-only\n---\n\nCandidate drift.\n",
                    encoding="utf-8",
                )
            return original_identity(journal, participant)

        try:
            with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
                first = runtime.start_turn("room-1", "worker", "first", wait=True)
                self.assertEqual(first["status"], "success")
                reads = 0
                with patch.object(runtime, "_identity", side_effect=gated_identity):
                    started = runtime.start_turn("room-1", "worker", "race", wait=False)
                    self.assertEqual(started["status"], "started")
                    self.assertTrue(entered.wait(timeout=5))
                    cancelling = runtime.cancel_turn("room-1", "worker")
                    self.assertTrue(cancelling["durable"])
                    release.set()
                    deadline = __import__("time").monotonic() + 5
                    finish = None
                    while __import__("time").monotonic() < deadline:
                        events = ConversationJournal.open(self.root, "room-1").events(native=True)
                        finish = next((event for event in events
                                       if event["event"] == "turn_finished"
                                       and event["payload"].get("turn_id") == started["turn_id"]),
                                      None)
                        if finish:
                            break
                        __import__("time").sleep(0.05)
                    self.assertIsNotNone(finish)
        finally:
            release.set()
            runtime.close(timeout=5)
        self.assertEqual(finish["payload"]["status"], "blocked")
        self.assertEqual(finish["payload"]["error_kind"], "chat_resume_refused")
        self.assertEqual(finish["payload"]["refusal_reason"], "resume_capability_changed")
        self.assertTrue(any(event["event"] == "turn_cancel_requested"
                             and event["payload"].get("turn_id") == started["turn_id"]
                             for event in ConversationJournal.open(self.root, "room-1").events(native=True)))
        self.assertEqual(len(self._calls()), 1)

    def test_malformed_native_refusal_cannot_fall_through_to_fresh_dispatch(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(runtime.start_turn("room-1", "worker", "first", wait=True)["status"], "success")
            path = self.root / "room-1.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                record = json.loads(line)
                if record.get("event") == "turn_finished":
                    record["payload"]["status"] = "blocked"
                    record["payload"]["error_kind"] = "chat_resume_refused"
                    record["payload"]["refusal"] = {"attempts": False}
                    record["payload_sha256"] = __import__("_conversation")._sha256(record["payload"])
                    lines[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    break
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(ConversationRuntimeError):
                runtime.start_turn("room-1", "worker", "must not start fresh", wait=True)
        self.assertEqual(len(self._calls()), 1)

    def test_restored_original_scope_resumes_same_handle(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            first = runtime.start_turn("room-1", "worker", "first", wait=True)
            journal = ConversationJournal.open(self.root, "room-1")
            events = journal.events(native=True)
            finish = next(event for event in events if event["event"] == "turn_finished")
            identity, execution = runtime._identity(journal, "worker")
            finish["payload"].update({
                "status": "blocked", "error_kind": "chat_resume_refused",
                "refusal_reason": "continuation_identity_incompatible",
                "refusal": build_refusal("continuation_identity_incompatible",
                                           resume_capability("claude", "subprocess")),
                "resume_capability_original": execution["resume_capability"],
                "resume_registry_scope": execution["resume_registry_scope"],
                "resume_session_id": "provider-session-1", "resume_profile": "fake-profile",
                "identity": identity,
            })
            path = self.root / "room-1.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                record = json.loads(line)
                if record.get("event") == "turn_finished":
                    record["payload"] = finish["payload"]
                    record["payload_sha256"] = __import__("_conversation")._sha256(record["payload"])
                    lines[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                    break
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            resumed = runtime.start_turn("room-1", "worker", "restored", wait=True)
            follow_up = runtime.start_turn("room-1", "worker", "after restore", wait=True)
        self.assertEqual(resumed["status"], "success")
        self.assertEqual(follow_up["status"], "success")
        self.assertIn("--resume", self._calls()[-1])

    def _exercise_late_resume_refusal(self, *, prior_refusal):
        """A pre-Popen drift closes the real turn and never falls back to fresh."""
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            self.assertEqual(
                runtime.start_turn("room-1", "worker", "first", wait=True)["status"],
                "success",
            )
            journal = ConversationJournal.open(self.root, "room-1")
            old_identity, old_execution = runtime._identity(journal, "worker")
            if prior_refusal:
                path = self.root / "room-1.jsonl"
                lines = path.read_text(encoding="utf-8").splitlines()
                for index, line in enumerate(lines):
                    record = json.loads(line)
                    if record.get("event") == "turn_finished":
                        record["payload"].update({
                            "status": "blocked",
                            "error_kind": "chat_resume_refused",
                            "refusal_reason": "continuation_identity_incompatible",
                            "refusal": build_refusal(
                                "continuation_identity_incompatible",
                                old_execution["resume_capability"],
                            ),
                            "resume_capability_original": old_execution["resume_capability"],
                            "resume_registry_scope": old_execution["resume_registry_scope"],
                            "resume_session_id": "provider-session-1",
                            "resume_profile": "fake-profile",
                            "identity": old_identity,
                        })
                        record["payload_sha256"] = __import__("_conversation")._sha256(
                            record["payload"])
                        lines[index] = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                        break
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            original_identity = runtime._identity
            reads = 0

            def drift_before_worker(journal_arg, participant):
                nonlocal reads
                reads += 1
                if reads == 3:
                    (self.roster / "worker.md").write_text(
                        "---\nrun-agent: codex\nmodel: candidate-model\n"
                        "permission: read-only\n---\n\nCandidate drift.\n",
                        encoding="utf-8",
                    )
                return original_identity(journal_arg, participant)

            with patch.object(runtime, "_identity", side_effect=drift_before_worker):
                late = runtime.start_turn("room-1", "worker", "late resume", wait=True)
            self.assertEqual(late["status"], "blocked")
            self.assertEqual(late["error_kind"], "chat_resume_refused")
            self.assertEqual(late["refusal"]["reason_code"], "resume_capability_changed")
            self.assertEqual(len(self._calls()), 1)

            # The unresolved lineage refuses again without creating a fresh
            # turn.  It is not discarded merely because the current roster is
            # still ineligible.
            repeated = runtime.start_turn("room-1", "worker", "try again", wait=True)
            self.assertEqual(repeated["status"], "blocked")
            self.assertEqual(repeated["error_kind"], "chat_resume_refused")
            self.assertEqual(len(self._calls()), 1)

            (self.roster / "worker.md").write_text(
                "---\nrun-agent: claude\nmodel: fake-model\n"
                "permission: read-only\n---\n\nA deterministic test participant.\n",
                encoding="utf-8",
            )
            restored = runtime.start_turn("room-1", "worker", "restore", wait=True)
            follow_up = runtime.start_turn("room-1", "worker", "after restore", wait=True)

        self.assertEqual(restored["status"], "success")
        self.assertEqual(follow_up["status"], "success")
        calls = self._calls()
        self.assertEqual(len(calls), 3)
        self.assertIn("--resume", calls[-1])
        self.assertIn("provider-session-1", calls[-1])
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        refusal_finish = [event for event in events if event["event"] == "turn_finished"][1]
        self.assertEqual(refusal_finish["payload"]["resume_session_id"], "provider-session-1")
        self.assertEqual(refusal_finish["payload"]["resume_capability_original"],
                         old_execution["resume_capability"])
        self.assertEqual(refusal_finish["payload"]["resume_registry_scope"],
                         old_execution["resume_registry_scope"])

    def test_late_resume_refusal_retains_lineage_from_existing_refusal(self):
        self._exercise_late_resume_refusal(prior_refusal=True)

    def test_late_resume_refusal_retains_admission_lineage(self):
        self._exercise_late_resume_refusal(prior_refusal=False)

    def test_v2_launch_scope_refusal_happens_before_provider_contact(self):
        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}):
            runtime = self._runtime()
            first = runtime.start_turn("room-1", "worker", "first", wait=True)
            self.assertEqual(first["status"], "success")
            with patch(
                "_conversation_runtime.compare_launch_scope",
                return_value={"status": "refused", "reason": "adapter_version_mismatch"},
            ) as compare:
                result = runtime.start_turn("room-1", "worker", "resume", wait=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_kind"], "chat_resume_refused")
        self.assertEqual(result["refusal"]["reason_code"], "resume_capability_changed")
        compare.assert_called_once()
        self.assertEqual(len(self._calls()), 1)

    def test_popen_exception_after_attempt_is_not_a_refusal(self):
        runtime = self._runtime()
        with patch("_conversation_runtime.subprocess.Popen", side_effect=OSError("synthetic")):
            result = runtime.start_turn("room-1", "worker", "spawn failure", wait=True)
        self.assertEqual(result["status"], "error")
        self.assertNotEqual(result.get("error_kind"), "chat_resume_refused")
        finish = next(event for event in ConversationJournal.open(
            self.root, "room-1").events(native=True) if event["event"] == "turn_finished")
        self.assertNotIn("refusal", finish["payload"])

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
        self.addCleanup(runtime.close)
        self.room.append_human_message("Earlier immutable history.", actor_id="human")
        earlier_history = self.room.path.read_bytes()
        definition = (self.roster / "worker.md").read_text(encoding="utf-8")
        reads = []
        durable = []

        def drift_on_final_read(journal, participant):
            persisted = ConversationJournal.open(self.root, "room-1")
            events = persisted.events(native=True)
            starts = [event for event in events if event["event"] == "turn_started"]
            if starts:
                # Admission and post-owner identity reads precede this durable
                # start. Only the worker's final read may observe the mutation.
                self.assertEqual(len(reads), 2)
                self.assertEqual(len(starts), 1)
                self.assertFalse(any(event["event"] == "turn_finished" for event in events))
                self.assertTrue(persisted.path.read_bytes().startswith(earlier_history))
                durable.append((starts[0], persisted.path.read_bytes()))
                (self.roster / "worker.md").write_text(
                    definition.replace("A deterministic test participant.",
                                       "A changed after-start test participant."),
                    encoding="utf-8")
            identity, execution = original_identity(journal, participant)
            reads.append((dict(identity), dict(execution)))
            return identity, execution

        with patch.dict(os.environ, {"FAKE_MARKER": str(self.marker)}), \
                patch.object(runtime, "_identity", side_effect=drift_on_final_read), \
                patch("_conversation_runtime._chat_launch_guard.create_v2",
                      side_effect=AssertionError("unexpected launch guard")) as guard, \
                patch("_conversation_runtime.subprocess.Popen",
                      side_effect=AssertionError("unexpected child launch")) as popen:
            result = runtime.start_turn("room-1", "worker", "fence me", wait=True)
        guard.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(len(reads), 3)
        self.assertEqual(len(durable), 1)
        self.assertEqual(reads[0][1]["agent_definition_sha256"], reads[1][1]["agent_definition_sha256"])
        self.assertNotEqual(reads[1][1]["agent_definition_sha256"], reads[2][1]["agent_definition_sha256"])
        for field in ("cli", "model", "permission", "transport"):
            self.assertEqual(reads[1][1][field], reads[2][1][field])
        start, started_history = durable[0]
        self.assertEqual(start["payload"]["identity"]["prompt_contract_sha256"],
                         reads[1][0]["prompt_contract_sha256"])
        self.assertEqual(result["status"], "error")
        self.assertNotIn("error_kind", result)
        self.assertFalse(self.marker.exists())
        events = ConversationJournal.open(self.root, "room-1").events(native=True)
        finishes = [event for event in events if event["event"] == "turn_finished"]
        self.assertEqual(len(finishes), 1)
        finish = finishes[0]
        self.assertEqual(finish["payload"]["turn_id"], start["payload"]["turn_id"])
        self.assertEqual(finish["payload"]["status"], "error")
        self.assertEqual(finish["payload"]["summary"], "ConversationRuntimeError")
        self.assertIsNone(finish["payload"]["model_served"])
        self.assertNotIn("launch_observation", finish["payload"])
        self.assertTrue(self.room.path.read_bytes().startswith(started_history))
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
        try:
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
        finally:
            # The durable finish event is written before the worker removes its
            # process record and releases the owner. Close the runtime before
            # TemporaryDirectory cleanup so Windows cannot race that final
            # bookkeeping and leave the test directory non-empty.
            runtime.close(timeout=5)

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
        try:
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
        finally:
            # The worker is intentionally asynchronous in this test.  Close
            # both runtimes before TemporaryDirectory teardown so Windows does
            # not retain an open journal handle.
            left.close(timeout=5)
            right.close(timeout=5)

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

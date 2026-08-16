from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in os.sys.path:
    os.sys.path.insert(0, str(HERE))

from _conversation import (ConversationError, ConversationJournal,
                           MAX_JOURNAL_BYTES, council_recommendation,
                           continuation_decision, group_rooms, promote_recommendation)
from _conversation import run_command


class ConversationJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "project"
        self.project.mkdir()
        self.root = Path(self.tmp.name) / "rooms"

    def tearDown(self):
        self.tmp.cleanup()

    def _room(self, *, mode="chat"):
        return ConversationJournal.create(
            self.root, session_id="session-1", project_id="summon",
            project_root=self.project, initiator_host="codex",
            initiator_agent="sol", mode=mode,
            participants=[{"agent": "sol", "role": "architect", "name": "Sol", "version": "5.6"}],
        )

    def test_room_and_human_message_are_durable_and_redacted(self):
        room = self._room()
        event = room.append_human_message("do not leak SECRET_TOKEN", actor_id="human")
        self.assertEqual(event["event"], "human_message")
        self.assertNotIn("SECRET_TOKEN", json.dumps(event))
        self.assertIn("do not leak", event["payload"]["preview"])
        self.assertEqual(event["payload"]["preview"], event["payload"]["summary"])
        self.assertEqual(room.room.cursor, 2)
        reopened = ConversationJournal.open(self.root, "session-1")
        self.assertEqual(reopened.room.cursor, 2)
        self.assertNotIn("SECRET_TOKEN", json.dumps(reopened.as_dict()))
        self.assertIn("SECRET_TOKEN", json.dumps(reopened.as_dict(native=True)))

    def test_open_reader_refreshes_after_another_writer(self):
        room = self._room()
        reader = ConversationJournal.open(self.root, "session-1")
        room.append("message_posted", "agent", "sol",
                    {"message_id": "m1", "summary": "new event"})
        self.assertEqual(reader.room.cursor, 2)
        self.assertEqual(len(reader.events()), 2)

    def test_cursor_conflict_and_event_id_are_idempotent(self):
        room = self._room()
        first = room.append("message_posted", "agent", "sol",
                            {"message_id": "m1", "summary": "first"},
                            event_id="evt-1", expected_cursor=1)
        again = room.append("message_posted", "agent", "sol",
                            {"message_id": "m1", "summary": "first"},
                            event_id="evt-1", expected_cursor=1)
        self.assertEqual(first, again)
        with self.assertRaises(ConversationError):
            room.append("message_posted", "agent", "sol",
                        {"message_id": "m2", "summary": "second"},
                        expected_cursor=1)
        with self.assertRaises(ConversationError):
            room.append("message_posted", "agent", "sol",
                        {"message_id": "m2", "summary": "changed"}, event_id="evt-1")

    def test_modes_keep_control_authority_separate(self):
        room = self._room(mode="council")
        with self.assertRaises(ConversationError):
            room.append("ballot_accepted", "agent", "sol", {"option_id": "yes"})
        deliberation = ConversationJournal.create(
            self.root, session_id="decision-1", project_id="summon",
            project_root=self.project, initiator_host="claude-code",
            initiator_agent="fable", mode="deliberate")
        with self.assertRaises(ConversationError):
            deliberation.append("ballot_accepted", "agent", "fable",
                                {"option_id": "yes"})

    def test_duplicate_participant_ids_are_rejected_at_admission(self):
        with self.assertRaises(ConversationError):
            ConversationJournal.create(
                self.root, session_id="duplicate-1", project_id="summon",
                project_root=self.project, initiator_host="codex",
                initiator_agent="sol", mode="chat",
                participants=[
                    {"agent": "worker", "role": "researcher", "name": "A", "version": "1"},
                    {"agent": "worker", "role": "reviewer", "name": "B", "version": "1"},
                ],
            )

    def test_council_round_and_human_chime_are_context_only(self):
        room = self._room(mode="council")
        events = room.append_council_round(
            1,
            [{"participant": "sol", "role": "architect", "summary": "choose yes"},
             {"participant": "fable", "role": "reviewer", "summary": "choose no"}],
            cross_exams=[{"asker": "sol", "target": "fable", "summary": "show evidence"}],
            synthesis={"summary": "split recommendation", "recommended_option": "yes"},
        )
        self.assertEqual([event["event"] for event in events],
                         ["council_round_started", "position_submitted", "position_submitted",
                          "cross_exam", "chair_synthesis"])
        human = room.append_human_message("I want to ask a follow-up")
        self.assertEqual(human["event"], "human_message")
        with self.assertRaises(ConversationError):
            room.append("state_transition", "system", "summon", {"reason": "consensus"})

    def test_council_recommendation_requires_human_promotion_boundary(self):
        artifact = council_recommendation(
            session_id="session-1", source_cursor=4,
            option_ids=["yes", "no"], recommended_option="yes",
            tradeoffs=["faster"], dissent=["reviewer disagrees"],
        )
        with self.assertRaises(ConversationError):
            promote_recommendation(artifact, human_confirmed=False,
                                   decision_id="decision-1", seat_ids=["sol", "fable"],
                                   option_ids=["yes", "no"], quorum="all", rounds=1,
                                   max_attempts=4, deadline_unix_ms=4_000_000_000_000,
                                   require_human_approval=True)
        setup = promote_recommendation(artifact, human_confirmed=True,
                                       decision_id="decision-1", seat_ids=["sol", "fable"],
                                       option_ids=["yes", "no"], quorum="all", rounds=1,
                                       max_attempts=4, deadline_unix_ms=4_000_000_000_000,
                                       require_human_approval=True)
        self.assertEqual(setup["mode"], "deliberate")
        self.assertEqual(setup["provider_calls"], 0)

    def test_private_payload_fields_are_not_projected(self):
        room = self._room()
        event = room.append("position_submitted", "agent", "sol", {
            "turn_id": "turn-1", "summary": "safe summary",
            "cwd": r"C:\private\repo", "argv": ["--token", "SECRET"],
            "prompt": "TOP_SECRET", "transport": "private",
        })
        serialized = json.dumps(event)
        self.assertIn("safe summary", serialized)
        for secret in ("TOP_SECRET", "SECRET", "C:\\private\\repo", "argv", "prompt", "transport"):
            self.assertNotIn(secret, serialized)
        path_event = room.append("message_posted", "agent", "sol",
                                 {"message_id": "m-path", "summary": r"see C:\private\repo"})
        self.assertNotIn(r"C:\private\repo", json.dumps(path_event))
        spaced = room.append("message_posted", "agent", "sol",
                             {"message_id": "m-spaced", "summary": r"open C:\private folder\repo now"})
        self.assertNotIn("private folder", json.dumps(spaced))
        artifact = council_recommendation(
            session_id="session-1", source_cursor=2, option_ids=["yes", "no"],
            recommended_option="yes", tradeoffs=[r"C:\private folder\repo"],
            dissent=[r"/tmp/secret folder"],
        )
        self.assertNotIn("private folder", json.dumps(artifact))
        self.assertNotIn("secret folder", json.dumps(artifact))

    def test_generic_http_credentials_are_not_projected(self):
        room = self._room()
        message = (
            "Authorization: Bearer abcdefghijklmnopQRSTUV0123456789 "
            "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ== "
            "Cookie: session=private-cookie-value"
        )
        event = room.append_human_message(message)
        public = json.dumps(event)
        self.assertNotIn("abcdefghijklmnopQRSTUV0123456789", public)
        self.assertNotIn("QWxhZGRpbjpvcGVuIHNlc2FtZQ==", public)
        self.assertNotIn("private-cookie-value", public)
        self.assertIn("[redacted]", public)

    def test_after_cursor_is_bounded(self):
        room = self._room()
        room.append_human_message("hello")
        self.assertEqual(room.events(after_cursor=2), [])
        with self.assertRaises(ConversationError):
            room.events(after_cursor=-1)
        with self.assertRaises(ConversationError):
            room.events(after_cursor=True)

    def test_concurrent_writers_keep_one_cursor_sequence(self):
        room = self._room()
        left = ConversationJournal.open(self.root, "session-1")
        right = ConversationJournal.open(self.root, "session-1")
        def write(journal, index):
            return journal.append("message_posted", "agent", "sol",
                                  {"message_id": f"m-{index}", "summary": f"s-{index}"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda pair: write(*pair), [(left, 1), (right, 2)]))
        reopened = ConversationJournal.open(self.root, "session-1")
        self.assertEqual(reopened.room.cursor, 3)
        self.assertEqual([event["cursor"] for event in reopened.events()], [1, 2, 3])

    def test_crashed_writer_stale_append_lock_is_reclaimed(self):
        room = self._room()
        lock = room.path.with_name(room.path.name + ".lock")
        lock.write_text(json.dumps({"pid": 2_147_483_647, "token": "a" * 32,
                                    "started_at": 1}), encoding="ascii")
        old = 1.0
        os.utime(lock, (old, old))
        event = room.append("message_posted", "agent", "sol",
                            {"message_id": "after-crash", "summary": "reclaimed"})
        self.assertEqual(event["cursor"], 2)
        self.assertFalse(lock.exists())

    def test_tampered_filename_payload_empty_and_oversized_rooms_fail_closed(self):
        room = self._room()
        path = self.root / "session-1.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0])
        header["session_id"] = "other-room"
        path.write_text(json.dumps(header) + "\n" + "\n".join(lines[1:]) + "\n", encoding="utf-8")
        with self.assertRaises(ConversationError):
            ConversationJournal.open(self.root, "session-1")

        empty = self.root / "empty.jsonl"
        empty.write_text(json.dumps({"record": "conversation_room"}) + "\n", encoding="utf-8")
        with self.assertRaises(ConversationError):
            ConversationJournal.open(self.root, "empty")

        oversized = self.root / "oversized.jsonl"
        oversized.write_bytes(b"x" * (MAX_JOURNAL_BYTES + 1))
        with self.assertRaises(ConversationError):
            ConversationJournal.open(self.root, "oversized")

    def test_non_string_ids_and_invalid_public_session_metadata_fail(self):
        with self.assertRaises(ConversationError):
            group_rooms([{"session_id": 1, "project_id": "one",
                          "project_root_sha256": "a" * 64,
                          "initiator_host": "codex", "initiator_agent": "sol",
                          "mode": "chat", "cursor": 0}])
        room = self._room()
        path = self.root / "session-1.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[1])
        record["payload"]["project_id"] = "TOP_SECRET"
        # A forged digest must still hit the strict event-specific allowlist.
        record["payload_sha256"] = __import__("_conversation")._sha256(record["payload"])
        path.write_text(lines[0] + "\n" + json.dumps(record) + "\n", encoding="utf-8")
        with self.assertRaises(ConversationError):
            ConversationJournal.open(self.root, "session-1")

    def test_participant_generator_is_bounded_before_materialization(self):
        consumed = []
        def participants():
            for index in range(1000):
                consumed.append(index)
                yield {"agent": f"agent-{index}"}
        with self.assertRaises(ConversationError):
            ConversationJournal.create(
                self.root, session_id="bounded", project_id="summon",
                project_root=self.project, initiator_host="codex",
                initiator_agent="sol", participants=participants())
        self.assertEqual(len(consumed), 17)

    def test_chat_open_browser_handoff_is_reused_without_provider_contact(self):
        args = SimpleNamespace(
            chat_action="open", chat_session="browser-room",
            chat_project_id="summon", chat_project_root=str(self.project),
            chat_initiator_host="codex", chat_initiator_agent="sol",
            chat_mode="chat", conversation_dir=str(self.root), cwd=str(self.project),
            chat_browser="link",
        )
        with patch("_conversation_browser.ensure_surface",
                   return_value={"url": "http://127.0.0.1:43123/#token=" + "a" * 48,
                                 "reused": True, "pid": 123}), \
             patch("_conversation_browser.open_url",
                   return_value={"url": "http://127.0.0.1:43123/#token=" + "a" * 48,
                                 "target": "link", "opened": False, "reused": False}):
            from io import StringIO
            with patch("sys.stdout", new_callable=StringIO) as output:
                self.assertEqual(run_command(args), 0)
            payload = json.loads(output.getvalue())
        self.assertEqual(payload["browser"]["target"], "link")
        self.assertFalse(payload["browser"]["opened"])

        # Existing-room browser handoff with the parser's default cwd must be
        # a bounded result, not the old UnboundLocalError path.
        args.cwd = None
        with patch("_conversation_browser.ensure_surface",
                   return_value={"url": "http://127.0.0.1:43123/#token=" + "a" * 48,
                                 "reused": True, "pid": 123}), \
             patch("_conversation_browser.open_url",
                   return_value={"url": "http://127.0.0.1:43123/#token=" + "a" * 48,
                                 "target": "link", "opened": False, "reused": True}):
            from io import StringIO
            with patch("sys.stdout", new_callable=StringIO) as output:
                self.assertEqual(run_command(args), 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "opened")


class ConversationPolicyTests(unittest.TestCase):
    def test_continuation_is_explicit_and_forks_on_identity_drift(self):
        common = {"provider": "codex", "profile_digest": "p", "model_target": "m",
                  "profile_revision_sha256": "c" * 64, "account_evidence_sha256": "d" * 64,
                  "model_served_sha256": "a" * 64, "prompt_contract_sha256": "b" * 64,
                  "permission": "read-only", "owner_generation": 1}
        common["profile_digest"] = "e" * 64
        decision = continuation_decision("session-1", common, dict(common))
        self.assertEqual(decision.action, "continue")
        changed = dict(common)
        changed["model_served_sha256"] = "f" * 64
        decision = continuation_decision("session-1", common, changed)
        self.assertEqual(decision.action, "fork")
        self.assertIn("model_served_sha256", decision.reason)
        newer_owner = dict(common)
        newer_owner["owner_generation"] = 2
        self.assertEqual(continuation_decision("session-1", common, newer_owner).action, "continue")
        stale_owner = dict(common)
        stale_owner["owner_generation"] = 0
        self.assertEqual(continuation_decision("session-1", common, stale_owner).action, "fork")
        incomplete = dict(common)
        incomplete["account_evidence_sha256"] = None
        self.assertEqual(continuation_decision("session-1", common, incomplete).action, "fork")
        huge_key = "account_" + ("x" * 1000000)
        oversized = dict(common)
        oversized[huge_key] = "secret"
        reason = continuation_decision("session-1", common, oversized).reason
        self.assertLessEqual(len(reason), 512)
        self.assertNotIn("x" * 1000, reason)

    def test_grouping_is_project_then_initiator(self):
        grouped = group_rooms([
            {"session_id": "a", "project_id": "one", "initiator_host": "codex",
             "initiator_agent": "sol", "mode": "chat", "cursor": 2,
             "project_root_sha256": "a" * 64},
            {"session_id": "b", "project_id": "one", "initiator_host": "cursor",
             "initiator_agent": "reviewer", "mode": "council", "cursor": 4,
             "project_root_sha256": "a" * 64},
            {"session_id": "c", "project_id": "two", "initiator_host": "codex",
             "initiator_agent": "sol", "mode": "deliberate", "cursor": 1,
             "project_root_sha256": "b" * 64},
        ])
        self.assertEqual(set(grouped), {"one@" + "a" * 64, "two@" + "b" * 64})
        self.assertEqual(set(grouped["one@" + "a" * 64]), {"codex/sol", "cursor/reviewer"})
        self.assertEqual(grouped["two@" + "b" * 64]["codex/sol"][0]["mode"], "deliberate")

    def test_no_provider_or_process_imports(self):
        source = Path(__file__).with_name("_conversation.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess", "socket", "requests", "Popen", "execute_agent"):
            self.assertNotIn(forbidden, source)

    def test_cli_subcommand_is_provider_inert_and_grouped(self):
        script = HERE / "run_subagent.py"
        with tempfile.TemporaryDirectory() as cli_tmp:
            root = Path(cli_tmp) / "cli-rooms"
            project = Path(cli_tmp) / "project"
            project.mkdir()
            commands = [
                ["chat", "open", "cli-room", "--project-id", "demo", "--project-root", str(project),
                 "--initiator-host", "claude-code", "--initiator-agent", "opus", "--conversation-dir", str(root), "--json"],
                ["chat", "post", "cli-room", "--message", "human context", "--conversation-dir", str(root), "--json"],
                ["chat", "list", "--conversation-dir", str(root), "--json"],
            ]
            results = []
            for command in commands:
                completed = subprocess.run([sys.executable, str(script), *command],
                                           cwd=str(HERE.parent.parent.parent),
                                           capture_output=True, text=True, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
                results.append(json.loads(completed.stdout))
            project_key = next(key for key in results[-1]["rooms"] if key.startswith("demo@"))
            self.assertEqual(results[-1]["rooms"][project_key]["claude-code/opus"][0]["mode"], "chat")


if __name__ == "__main__":
    unittest.main()

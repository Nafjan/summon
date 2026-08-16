#!/usr/bin/env python3
"""Security and contract tests for the public provider-inert loopback surface."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_ui as ui
import _deliberation_store as store
import _rundir


def _real_receipt(run_id: str) -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "decision_id": "decision-1", "question_sha256": "a" * 64,
        "seat_ids": ["a", "b"], "option_ids": ["yes", "no"],
        "quorum_rule": "all", "max_attempts": 4,
        "require_human_approval": False, "rounds": 1,
        "deadline_unix_ms": 4_000_000_000_000, "created_at": 1.0,
        "plan_identity_by_seat": {
            "a": {"cli": "codex", "model_sha256": hashlib.sha256(
                b"gpt-5.6-sol").hexdigest()},
            "b": {"cli": "agy", "model_sha256": hashlib.sha256(
                b"gemini-3.7-flash-high").hexdigest()},
        },
        "model_served_sha256_by_seat": {
            "a": hashlib.sha256(b"gpt-5.6-sol").hexdigest(),
        },
        "model_display_by_seat": {
            "a": {"role": "architecture", "name": "Sol", "version": "5.6",
                  "label": "frontier", "lane": "architecture",
                  "availability": "served_exact", "served_exact": True},
            "b": {"role": "evidence", "name": "Gemini Flash", "version": "3.7",
                  "label": "near-frontier", "lane": "evidence-research",
                  "availability": "catalog_listed", "served_exact": False},
        },
    }


class DeliberationUITests(unittest.TestCase):
    def setUp(self):
        self.surface = ui.DeliberationSurface("C:\\private\\runs", "run-1",
                                             token="t" * 48)
        self.surface.start()
        self.addCleanup(self.surface.close)
        self.snapshot = {
            "mode": "deliberation-status", "status": "success", "run_id": "run-1",
            "projection": {"state": "WAITING_HUMAN"}, "journal_records": 2,
            "receipt": {"decision_id": "decision", "seat_ids": ["a", "b"]},
            "records": [{"event": "state_transition", "to": "WAITING_HUMAN"}],
        }

    def request(self, path, *, method="GET", body=None, origin=None, token=True):
        url = f"http://127.0.0.1:{self.surface.address[1]}{path}"
        headers = {"Authorization": "Bearer " + self.surface.token} if token else {}
        if origin is not None:
            headers["Origin"] = origin
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        return urlopen(Request(url, data=data, headers=headers, method=method), timeout=3)

    def test_bootstrap_is_fragment_only_and_page_has_csp_contract(self):
        self.assertIn("#token=", self.surface.url)
        self.assertNotIn("Authorization", self.surface.url)
        response = urlopen(self.surface.url.split("#")[0], timeout=3)
        body = response.read().decode("utf-8")
        self.assertIn("Content-Security-Policy", " ".join(response.headers.keys()))
        self.assertIn("governed decision ledger", body)
        self.assertIn("THESIS:", body)
        self.assertIn("/replay", body)
        self.assertIn("/events", body)
        self.assertIn("Governed decision", body)
        self.assertIn("metric-candidate", body)
        self.assertIn("metric-rounds", body)
        self.assertIn("humanGate.hidden", body)
        self.assertIn("window.confirm", body)
        self.assertIn("evidence-list", body)
        self.assertIn("modelTooltip", body)
        self.assertIn("model_display_by_seat", body)

    def test_page_uses_canonical_replay_and_compact_event_details(self):
        body = ui._page(nonce="testnonce").decode("utf-8")
        self.assertIn("canonical replay", body)
        self.assertIn("eventSummary", body)
        self.assertIn("createElement('details')", body)
        self.assertIn("Live updates paused; retrying safely.", body)

    def test_generated_page_script_is_javascript_syntax_valid(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        body = ui._page(nonce="syntax-test").decode("utf-8")
        scripts = re.findall(r"<script[^>]*>(.*?)</script>", body, flags=re.S)
        self.assertTrue(scripts)
        checked = subprocess.run(
            [node, "--check", "-"], input=scripts[-1], text=True,
            capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_flight_recorder_truth_and_responsive_polish_contract(self):
        body = ui._page(nonce="testnonce").decode("utf-8")
        for marker in (
            "global-alert", "seat-strip", "round-divider", "event-technical",
            "refreshCountdown", "Owner unavailable", "Verified clean",
            "The governed decision is complete.", "recovery_required",
        ):
            self.assertIn(marker, body)

    def test_snapshot_requires_bearer_and_exact_host(self):
        with mock.patch.object(ui._store, "inspect_run", return_value=self.snapshot):
            with self.assertRaises(HTTPError) as missing:
                self.request("/api/v1/runs/run-1/snapshot", token=False)
            self.assertEqual(missing.exception.code, 401)
            response = self.request("/api/v1/runs/run-1/snapshot")
            self.assertEqual(json.loads(response.read()), self.snapshot)

    def test_snapshot_forwards_the_store_public_projection_without_reconstruction(self):
        public = dict(self.snapshot, projection={"state": "RUNNING"})
        with mock.patch.object(ui._store, "inspect_run", return_value=public):
            data = json.loads(self.request("/api/v1/runs/run-1/snapshot").read())
        self.assertEqual(data, public)

    def test_cancel_requires_same_origin_and_queues_only_typed_cancel(self):
        queued = {"status": "success", "command_status": "queued"}
        with mock.patch.object(ui._store, "queue_cancel", return_value=queued) as queue:
            with self.assertRaises(HTTPError) as missing:
                self.request("/api/v1/runs/run-1/commands", method="POST",
                             body={"action": "cancel"})
            self.assertEqual(missing.exception.code, 401)
            origin = f"http://127.0.0.1:{self.surface.address[1]}"
            response = self.request("/api/v1/runs/run-1/commands", method="POST",
                                    body={"action": "cancel", "command_id": "ui-1"},
                                    origin=origin)
            self.assertEqual(json.loads(response.read()), queued)
            queue.assert_called_once_with("C:\\private\\runs", "run-1", "ui-1")

    def test_non_cancel_commands_are_refused_before_store(self):
        with mock.patch.object(ui._store, "queue_cancel") as queue:
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/v1/runs/run-1/commands", method="POST",
                             body={"action": "approve"},
                             origin=f"http://127.0.0.1:{self.surface.address[1]}")
            self.assertEqual(caught.exception.code, 400)
            queue.assert_not_called()

    def test_extra_route_segments_are_refused(self):
        with mock.patch.object(ui._store, "inspect_run") as inspect:
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/v1/runs/run-1/extra/snapshot")
            self.assertEqual(caught.exception.code, 400)
            inspect.assert_not_called()

    def test_sse_uses_event_stream_and_does_not_require_eventsource(self):
        with mock.patch.object(ui._store, "replay_run", return_value=self.snapshot), \
             mock.patch.object(ui, "SSE_MAX_SECONDS", 0.01), \
             mock.patch.object(ui, "SSE_POLL_SECONDS", 0.001):
            response = self.request("/api/v1/runs/run-1/events")
            self.assertTrue(response.headers["Content-Type"].startswith("text/event-stream"))
            self.assertIn(b"event: snapshot", response.read())

    def test_close_signals_inflight_sse_pollers(self):
        self.assertFalse(self.surface.closed.is_set())
        self.surface.close()
        self.assertTrue(self.surface.closed.is_set())
        self.surface = ui.DeliberationSurface("C:\\private\\runs", "run-1",
                                             token="t" * 48)
        self.surface.start()

    def test_real_journal_snapshot_replay_sse_cancel_and_torn_tail(self):
        """Exercise the loopback surface against a real durable run, not mocks."""
        with tempfile.TemporaryDirectory(prefix="summon-ui-real-") as root:
            path, owner = store.initialize_run(root, _real_receipt("run-1"))
            _rundir.release_owner(owner)
            surface = ui.DeliberationSurface(root, "run-1", token="r" * 48)
            surface.start()
            self.addCleanup(surface.close)

            def request(path_suffix, *, method="GET", body=None, origin=None):
                headers = {"Authorization": "Bearer " + surface.token}
                if origin is not None:
                    headers["Origin"] = origin
                data = None if body is None else json.dumps(body).encode("utf-8")
                if data is not None:
                    headers["Content-Type"] = "application/json"
                url = f"http://127.0.0.1:{surface.address[1]}{path_suffix}"
                return json.loads(urlopen(
                    Request(url, data=data, headers=headers, method=method), timeout=3
                ).read())

            snapshot = request("/api/v1/runs/run-1/snapshot")
            self.assertEqual(snapshot["status"], "success")
            self.assertEqual(snapshot["projection"]["state"], "prepared")
            self.assertIsNone(snapshot["owner"])
            self.assertEqual(snapshot["receipt"]["model_display_by_seat"]["a"]["name"], "Sol")
            self.assertNotIn("profile", json.dumps(snapshot["receipt"]))
            replay = request("/api/v1/runs/run-1/replay")
            self.assertEqual(replay["status"], "success")
            self.assertEqual(replay["records"][0]["event"], "run_prepared")

            # SSE is not JSON; read the stream body directly.
            stream_request = Request(
                f"http://127.0.0.1:{surface.address[1]}/api/v1/runs/run-1/events",
                headers={"Authorization": "Bearer " + surface.token},
            )
            with mock.patch.object(ui, "SSE_MAX_SECONDS", 0.02), \
                 mock.patch.object(ui, "SSE_POLL_SECONDS", 0.001):
                stream_response = urlopen(stream_request, timeout=3)
                stream_body = stream_response.read()
            self.assertIn(b"event: snapshot", stream_body)

            origin = f"http://127.0.0.1:{surface.address[1]}"
            queued = request(
                "/api/v1/runs/run-1/commands", method="POST",
                body={"action": "cancel", "command_id": "ui-real-1"}, origin=origin,
            )
            self.assertEqual(queued["command_status"], "queued")
            self.assertEqual(request("/api/v1/runs/run-1/snapshot")["pending_commands"], 1)

            # A hostile extra journal field must not cross the public replay boundary.
            journal = _rundir._journal_path(path, 1)
            line = Path(journal).read_text(encoding="utf-8").strip()
            record = json.loads(line)
            record.pop("sha256")
            record["secret"] = "TOP_SECRET_SHOULD_NOT_ESCAPE"
            Path(journal).write_text(
                _rundir._journal_line(record) + "\n", encoding="utf-8"
            )
            replay = request("/api/v1/runs/run-1/replay")
            self.assertNotIn("TOP_SECRET_SHOULD_NOT_ESCAPE", json.dumps(replay))

            # A torn newest line blocks the public view and never gets presented as healthy.
            with open(journal, "ab") as fh:
                fh.write(b"{torn-tail")
            blocked = request("/api/v1/runs/run-1/snapshot")
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["error_kind"], "torn_tail")
            self.assertFalse(blocked["consistent"])
            # Close the live server before TemporaryDirectory removes the
            # project tree.  unittest cleanups run after the context manager;
            # relying on addCleanup alone leaves a Windows listener/thread
            # holding the directory and makes the release registry flaky.
            surface.close()

    def test_model_display_projection_is_bounded_and_redacts_hostile_values(self):
        receipt = {
            "decision_id": "decision", "seat_ids": ["a"],
            "option_ids": ["yes", "no"], "quorum_rule": "all",
            "max_attempts": 1, "require_human_approval": False,
            "model_display_by_seat": {
                "a": {"role": "research", "name": "Gemini", "version": "3.7",
                      "label": "near-frontier", "lane": "evidence",
                      "availability": "unverified", "served_exact": False,
                      "prompt": "TOP_SECRET", "cwd": "C:\\private"},
                "b": {"role": "C:\\private", "name": "secret\nname", "version": "3.7"},
            },
        }
        projected = store._public_receipt(receipt)
        # Receipt-authored display rows are not evidence and are ignored when
        # there is no exact plan hash from which to derive catalog identity.
        self.assertNotIn("model_display_by_seat", projected)
        self.assertNotIn("prompt", json.dumps(projected))
        self.assertNotIn("private", json.dumps(projected))

    def test_catalog_display_wins_and_served_evidence_is_hash_bound(self):
        receipt = {
            "decision_id": "decision", "seat_ids": ["a"],
            "option_ids": ["yes", "no"], "quorum_rule": "all",
            "max_attempts": 1, "require_human_approval": False,
            "plan_identity_by_seat": {
                "a": {"cli": "codex", "model_sha256": hashlib.sha256(
                    b"gpt-5.6-sol").hexdigest()},
            },
            # This forged row must not override the catalog role/name/version.
            "model_display_by_seat": {
                "a": {"role": "secret", "name": "Forged", "version": "0",
                      "label": "frontier", "served_exact": True},
            },
            "model_served_sha256_by_seat": {
                "a": hashlib.sha256(b"some-other-model").hexdigest(),
            },
        }
        projected = store._public_receipt(receipt)
        display = projected["model_display_by_seat"]["a"]
        self.assertEqual(display["name"], "Sol")
        self.assertEqual(display["version"], "5.6")
        self.assertEqual(display["availability"], "served_mismatch")
        self.assertFalse(display["served_exact"])
        self.assertNotIn("Forged", json.dumps(projected))


if __name__ == "__main__":
    unittest.main(verbosity=2)

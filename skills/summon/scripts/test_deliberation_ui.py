#!/usr/bin/env python3
"""Security and contract tests for the optional loopback deliberation surface."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_ui as ui


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

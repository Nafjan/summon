from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _conversation import ConversationJournal
import _conversation_browser
from _conversation_browser import ConversationBrowserError, ensure_surface, open_url
from _conversation_ui import (ConversationSurface, MAX_ACTIVE_CLIENTS,
                               REQUEST_TIMEOUT_SECONDS, _surface_root_digest,
                               read_surface_record)


class ConversationUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "rooms"
        project = Path(self.tmp.name) / "project"
        project.mkdir()
        self.room = ConversationJournal.create(
            self.root, session_id="session-1", project_id="summon",
            project_root=project, initiator_host="codex", initiator_agent="sol",
            mode="chat", participants=[{"agent": "sol", "role": "architect",
                                         "name": "Sol", "version": "5.6",
                                         "provider": "claude", "model": "claude-opus-5"}],
        )
        self.surface = ConversationSurface(str(self.root), token="a" * 48)
        self.base = self.surface.start()
        self.token = self.surface.token

    def tearDown(self):
        self.surface.close()
        self.tmp.cleanup()

    def _request(self, path, *, method="GET", body=None, token=True, origin=False):
        headers = {"Host": f"127.0.0.1:{self.surface.address[1]}"}
        if token:
            headers["Authorization"] = "Bearer " + self.token
        if origin:
            headers["Origin"] = f"http://{headers['Host']}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base.split("#", 1)[0].rstrip("/") + path,
                          method=method, headers=headers, data=data)
        return urlopen(request, timeout=3)

    def test_page_is_local_redacted_and_token_is_fragment_only(self):
        with urlopen(self.base.split("#", 1)[0], timeout=3) as response:
            page = response.read().decode()
        self.assertIn("Conversation atlas", page)
        self.assertIn("Ask an agent", page)
        self.assertIn("Stop this turn", page)
        self.assertIn("Context and agent turns", page)
        self.assertIn("Agents and models", page)
        self.assertIn("MODEL ·", page)
        self.assertIn("Context ·", page)
        self.assertIn("Built with a visual adaptation of", page)
        self.assertIn("agent-picker-button", page)
        self.assertIn("agent-option", page)
        self.assertIn("/stream?after=", page)
        self.assertIn("Live updates paused; retrying from recorded event", page)
        self.assertIn("setInterval(refresh,10000)", page)
        self.assertNotIn("setInterval(refresh,2000)", page)
        # Embedded browser harnesses may omit URI decoding globals; the
        # URL-safe fragment token must bootstrap auth without that optional
        # dependency.
        self.assertNotIn("decodeURIComponent", page)
        self.assertNotIn(self.token, page)

    def test_page_uses_messenger_regions_and_incremental_cursor_reducer(self):
        source = (HERE / "_conversation_page.py").read_text(encoding="utf-8")
        for marker in ("Conversation atlas", "room-search", "timeline", "Room details",
                       "Post context", "Ask an agent", "Context and agent turns",
                       "appendRecord", "cursor !== state.cursor + 1",
                       "fragment.append(renderEvent(record))", "event === 'agent_message'",
                       "Agents and models", "Model not verified", "identityTooltip",
                       "Role · name/version · model · provider", "Review agent request",
                       "WAITING FOR MODEL RECEIPT", "MODEL MATCH", "target missing", "setConnection('Live', 'connected'",
                       ".room-bar { display: block; }", "min-width: 44px; min-height: 44px",
                       "$('drawer-close').focus();", "height: 100dvh", "overflow: hidden",
                       "grid-template-rows: 96px 48px minmax(0, 1fr)", "captureFeedAnchor",
                       "state.feedPinned", "position: fixed; left: 0"):
            self.assertIn(marker, source)
        self.assertIn("stops automatically after", source)
        self.assertIn("Reconnect to the local owner before starting an agent turn", source)
        self.assertIn("action-dialog", source)
        self.assertNotIn("window.confirm", source)
        self.assertNotIn("window.prompt", source)
        self.assertNotIn("timeline.replaceChildren(); renderEvent", source)

    def test_rooms_and_room_events_are_authenticated_and_redacted(self):
        self.room.append_human_message(r"private context C:\secret\repo SECRET_TOKEN")
        with self._request("/api/v1/rooms") as response:
            rooms = json.loads(response.read())
        key = next(iter(rooms["rooms"]))
        self.assertIn("codex/sol", rooms["rooms"][key])
        self.assertEqual(rooms["rooms"][key]["codex/sol"][0]["participants"][0]["model"],
                         "claude-opus-5")
        self.assertIn("subject", rooms["rooms"][key]["codex/sol"][0])
        with self._request("/api/v1/rooms/session-1") as response:
            data = json.loads(response.read())
        self.assertEqual(data["room"]["session_id"], "session-1")
        self.assertEqual(data["room"]["participants"][0]["provider"], "claude")
        self.assertEqual(data["room"]["participants"][0]["model"], "claude-opus-5")
        public = json.dumps(data)
        self.assertIn("private context", public)
        self.assertNotIn(r"C:\secret\repo", public)
        self.assertNotIn("SECRET_TOKEN", public)
        self.assertIn("subject", data["room"])
        self.assertEqual(data["events"][-1]["payload"]["text_chars"],
                         len(r"private context C:\secret\repo SECRET_TOKEN"))

    def test_authenticated_cursor_stream_emits_redacted_events(self):
        self.room.append_human_message(r"stream secret C:\private\stream-token")
        request = Request(
            self.base.split("#", 1)[0].rstrip("/") + "/api/v1/rooms/session-1/stream?after=1",
            headers={"Host": f"127.0.0.1:{self.surface.address[1]}",
                     "Authorization": "Bearer " + self.token},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.headers.get("Content-Type"),
                             "text/event-stream; charset=utf-8")
            lines = [response.readline().decode("utf-8") for _ in range(3)]
        frame = "".join(lines)
        self.assertIn("id: 2", frame)
        self.assertIn("event: conversation", frame)
        self.assertNotIn(r"C:\private\stream-token", frame)

    def test_cursor_stream_requires_authentication_and_bounded_cursor(self):
        for query, authenticated in (("after=-1", False), ("after=999999999", True)):
            headers = {"Host": f"127.0.0.1:{self.surface.address[1]}"}
            if authenticated:
                headers["Authorization"] = "Bearer " + self.token
            request = Request(
                self.base.split("#", 1)[0].rstrip("/") + "/api/v1/rooms/session-1/stream?" + query,
                headers=headers,
            )
            with self.assertRaises(HTTPError) as refused:
                urlopen(request, timeout=3)
            self.assertEqual(refused.exception.code, 401 if not authenticated else 400)

    def test_post_requires_origin_and_only_appends_human_context(self):
        # Repeatedly exercise the rejected-body path: on Windows an unread
        # request body can otherwise race the response and surface as a socket
        # reset instead of the intended structured 401.
        for _ in range(3):
            with self.assertRaises(HTTPError) as refused:
                self._request("/api/v1/rooms/session-1/messages", method="POST",
                              body={"message": "no origin"})
            self.assertEqual(refused.exception.code, 401)
        with self._request("/api/v1/rooms/session-1/messages", method="POST",
                           body={"message": "hello from browser"}, origin=True) as response:
            posted = json.loads(response.read())
        self.assertEqual(posted["status"], "posted")
        with self._request("/api/v1/rooms/session-1") as response:
            data = json.loads(response.read())
        self.assertEqual(data["room"]["cursor"], 2)

    def test_agent_turn_endpoint_uses_persistent_runtime_and_is_not_a_control_command(self):
        calls = []

        class FakeRuntime:
            def start_turn(self, session, participant, prompt, *, wait=False):
                calls.append(("turn", session, participant, prompt, wait))
                return {"status": "started", "turn_id": "turn-1", "session_id": session,
                        "participant": participant}

            def cancel_turn(self, session, participant):
                calls.append(("cancel", session, participant))
                return {"status": "cancelling", "turn_id": "turn-1", "session_id": session,
                        "participant": participant}

        self.surface.runtime = FakeRuntime()
        with self.assertRaises(HTTPError) as unreviewed:
            self._request("/api/v1/rooms/session-1/turns", method="POST",
                          body={"participant": "sol", "message": "inspect this"}, origin=True)
        self.assertEqual(unreviewed.exception.code, 400)
        with self._request("/api/v1/rooms/session-1/turns", method="POST",
                           body={"participant": "sol", "message": "inspect this", "reviewed": True}, origin=True) as response:
            started = json.loads(response.read())
        self.assertEqual(response.status, 202)
        self.assertEqual(started["status"], "started")
        self.assertEqual(calls, [("turn", "session-1", "sol", "inspect this", False)])
        with self._request("/api/v1/rooms/session-1/cancel", method="POST",
                           body={"participant": "sol"}, origin=True) as response:
            cancelling = json.loads(response.read())
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(calls[-1], ("cancel", "session-1", "sol"))

    def test_bad_token_and_extra_route_are_refused(self):
        with self.assertRaises(HTTPError) as bad:
            self._request("/api/v1/rooms", token=False)
        self.assertEqual(bad.exception.code, 401)
        with self.assertRaises(HTTPError) as route:
            self._request("/api/v1/rooms/session-1/extra")
        self.assertEqual(route.exception.code, 400)

    def test_surface_record_reuses_one_atlas_and_link_mode_is_nonlaunching(self):
        with patch("_conversation_browser._launch_guard",
                   wraps=_conversation_browser._launch_guard) as guard:
            reused = ensure_surface(str(self.root))
        self.assertTrue(guard.called)
        self.assertTrue(reused["reused"])
        handoff = open_url(self.base, mode="link")
        self.assertFalse(handoff["opened"])
        with self.assertRaises(ConversationBrowserError):
            open_url(self.base.replace("127.0.0.1", "example.com"), mode="link")

    def test_surface_reuse_is_bound_to_project_and_explicit_roster(self):
        root = Path(self.tmp.name) / "bound-root"
        project_a = Path(self.tmp.name) / "project-a"
        project_b = Path(self.tmp.name) / "project-b"
        roster_a = Path(self.tmp.name) / "roster-a"
        roster_b = Path(self.tmp.name) / "roster-b"
        for path in (project_a, project_b, roster_a, roster_b):
            path.mkdir()
        surface = ConversationSurface(str(root), cwd=project_a, agents_dir=roster_a)
        surface.start()
        self.addCleanup(surface.close)
        record = read_surface_record(root)
        self.assertIsNotNone(record)
        self.assertIn("cwd_sha256", record)
        self.assertIn("agents_dir_sha256", record)
        self.assertIsNotNone(read_surface_record(root, cwd=project_a,
                                                 agents_dir=roster_a))
        self.assertIsNone(read_surface_record(root, cwd=project_b,
                                              agents_dir=roster_a))
        self.assertTrue(ensure_surface(str(root), cwd=str(project_a),
                                       agents_dir=str(roster_a))["reused"])
        with self.assertRaisesRegex(ConversationBrowserError, "different project"):
            ensure_surface(str(root), cwd=str(project_b), agents_dir=str(roster_a),
                           timeout=0.2)
        with self.assertRaisesRegex(ConversationBrowserError, "different agent roster"):
            ensure_surface(str(root), cwd=str(project_a), agents_dir=str(roster_b),
                           timeout=0.2)

    def test_legacy_surface_record_is_not_reused_for_bound_project(self):
        root = Path(self.tmp.name) / "legacy-bound-root"
        project = Path(self.tmp.name) / "legacy-project"
        project.mkdir()
        surface = ConversationSurface(str(root), cwd=project)
        surface.start()
        self.addCleanup(surface.close)
        record_path = root / ".summon-conversation-ui.json"
        original = record_path.read_text(encoding="utf-8")
        try:
            legacy = json.loads(original)
            legacy.pop("cwd_sha256", None)
            legacy.pop("agents_dir_sha256", None)
            record_path.write_text(json.dumps(legacy), encoding="utf-8")
            with self.assertRaisesRegex(ConversationBrowserError, "different project"):
                ensure_surface(str(root), cwd=str(project), timeout=0.2)
        finally:
            record_path.write_text(original, encoding="utf-8")

    def test_parent_symlink_is_rejected_before_launch_lock_write(self):
        real_parent = Path(self.tmp.name) / "real-parent"
        real_parent.mkdir()
        (real_parent / "rooms").mkdir()
        alias_parent = Path(self.tmp.name) / "alias-parent"
        try:
            alias_parent.symlink_to(real_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        with self.assertRaises(ConversationBrowserError):
            ensure_surface(str(alias_parent / "rooms"), timeout=0.2)
        self.assertFalse((real_parent / "rooms" / ".summon-conversation-ui.launch.lock").exists())

    def test_surface_record_is_bound_and_browser_rejects_userinfo(self):
        record_path = self.root / ".summon-conversation-ui.json"
        original = record_path.read_text(encoding="utf-8")
        try:
            forged = json.loads(original)
            forged["schema_version"] = 999
            record_path.write_text(json.dumps(forged), encoding="utf-8")
            self.assertIsNone(read_surface_record(self.root))
            record_path.write_text("x" * (16 * 1024 + 1), encoding="utf-8")
            self.assertIsNone(read_surface_record(self.root))
        finally:
            record_path.write_text(original, encoding="utf-8")
        with self.assertRaises(ConversationBrowserError):
            open_url("http://user:pass@127.0.0.1:43123/#token=" + "a" * 48, mode="link")

    def test_ensure_surface_does_not_accept_a_shaped_dead_endpoint(self):
        root = Path(self.tmp.name) / "stale-root"
        root.mkdir()
        token = "b" * 48
        (root / ".summon-conversation-ui.json").write_text(json.dumps({
            "schema_version": 1, "root_sha256": _surface_root_digest(root),
            "pid": 999999, "url": "http://127.0.0.1:54321/#token=" + token,
            "token": token,
        }), encoding="utf-8")
        # Full release runs start this child under a cold, hermetic Python
        # environment; use the production startup budget rather than making
        # readiness correctness depend on scheduler noise.
        started = ensure_surface(str(root), timeout=5.0)
        self.assertFalse(started["reused"])
        self.assertNotIn(":54321/", str(started["url"]))
        pid = int(started["pid"])
        ready = read_surface_record(root)
        self.assertIsNotNone(ready)
        self.assertEqual(ready["pid"], pid)
        self.assertEqual(ready["url"], started["url"])
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)

    def test_new_surface_is_detached_from_short_lived_handoff_process(self):
        root = Path(self.tmp.name) / "detached-root"
        root.mkdir()
        with patch.object(_conversation_browser, "popen_flags",
                          wraps=_conversation_browser.popen_flags) as flags:
            started = ensure_surface(str(root), timeout=5.0)
        self.assertTrue(any(call.kwargs.get("detached") is True
                             for call in flags.call_args_list))
        pid = int(started["pid"])
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)

    def test_live_unreachable_record_is_not_unlinked_or_replaced(self):
        root = Path(self.tmp.name) / "live-stale-root"
        root.mkdir()
        token = "c" * 48
        record_path = root / ".summon-conversation-ui.json"
        record_path.write_text(json.dumps({
            "schema_version": 1, "root_sha256": _surface_root_digest(root),
            "pid": os.getpid(), "url": "http://127.0.0.1:54321/#token=" + token,
            "token": token,
        }), encoding="utf-8")
        with self.assertRaises(ConversationBrowserError):
            ensure_surface(str(root), timeout=0.2)
        self.assertTrue(record_path.exists())

    def test_concurrent_surface_starts_share_one_server(self):
        root = Path(self.tmp.name) / "concurrent-root"
        root.mkdir()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ensure_surface, str(root), timeout=5.0)
                       for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual({item["pid"] for item in results}, {results[0]["pid"]})
        self.assertEqual({item["url"] for item in results}, {results[0]["url"]})
        pid = int(results[0]["pid"])
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)

    def test_direct_surface_starts_are_serialized_by_the_launch_guard(self):
        """Direct callers cannot publish two sidecars for one atlas root."""
        root = Path(self.tmp.name) / "direct-concurrent-root"
        root.mkdir()
        surfaces = [ConversationSurface(str(root), token=letter * 48)
                    for letter in ("d", "e")]

        def start(surface):
            try:
                return ("ok", surface.start())
            except Exception as exc:  # noqa: BLE001 - assert the public failure
                return ("error", exc)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [future.result(timeout=5)
                           for future in (pool.submit(start, surface) for surface in surfaces)]
            self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
            self.assertEqual(sum(kind == "error" for kind, _ in results), 1)
            error = next(value for kind, value in results if kind == "error")
            self.assertIsInstance(error, Exception)
            self.assertIn("already running", str(error))
        finally:
            for surface in surfaces:
                try:
                    surface.close()
                except Exception:
                    pass

    def test_surface_probe_never_follows_redirect_with_bearer(self):
        received = []

        class Sink(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                return

        sink = HTTPServer(("127.0.0.1", 0), Sink)

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib handler API
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/api/v1/rooms")
                self.end_headers()

            def log_message(self, *_args):
                return

        redirect = HTTPServer(("127.0.0.1", 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (sink, redirect)]
        for thread in threads:
            thread.start()
        token = "c" * 48
        record = {
            "url": f"http://127.0.0.1:{redirect.server_port}/#token={token}",
            "token": token,
        }
        try:
            self.assertFalse(_conversation_browser._surface_reachable(record))
            self.assertEqual(received, [])
        finally:
            redirect.shutdown()
            sink.shutdown()
            redirect.server_close()
            sink.server_close()
            for thread in threads:
                thread.join(timeout=1)

    def test_no_provider_import_surface(self):
        sources = [(HERE / "_conversation_ui.py").read_text(encoding="utf-8"),
                   (HERE / "_conversation_page.py").read_text(encoding="utf-8")]
        for source in sources:
            for forbidden in ("subprocess", "socket", "requests", "Popen", "execute_agent"):
                self.assertNotIn(forbidden, source)
        page_source = sources[1]
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "document.cookie",
                           "eval(", "WebSocket"):
            self.assertNotIn(forbidden, page_source)
        self.assertGreaterEqual(REQUEST_TIMEOUT_SECONDS, 1.0)
        self.assertGreaterEqual(MAX_ACTIVE_CLIENTS, 8)


if __name__ == "__main__":
    unittest.main()

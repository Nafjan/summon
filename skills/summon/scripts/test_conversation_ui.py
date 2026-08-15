from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _conversation import ConversationJournal
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
                                         "name": "Sol", "version": "5.6"}],
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
        self.assertIn("provider-inert", page)
        self.assertNotIn(self.token, page)

    def test_rooms_and_room_events_are_authenticated_and_redacted(self):
        self.room.append_human_message("private context")
        with self._request("/api/v1/rooms") as response:
            rooms = json.loads(response.read())
        key = next(iter(rooms["rooms"]))
        self.assertIn("codex/sol", rooms["rooms"][key])
        with self._request("/api/v1/rooms/session-1") as response:
            data = json.loads(response.read())
        self.assertEqual(data["room"]["session_id"], "session-1")
        self.assertNotIn("private context", json.dumps(data))
        self.assertEqual(data["events"][-1]["payload"]["text_chars"], len("private context"))

    def test_post_requires_origin_and_only_appends_human_context(self):
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

    def test_bad_token_and_extra_route_are_refused(self):
        with self.assertRaises(HTTPError) as bad:
            self._request("/api/v1/rooms", token=False)
        self.assertEqual(bad.exception.code, 401)
        with self.assertRaises(HTTPError) as route:
            self._request("/api/v1/rooms/session-1/extra")
        self.assertEqual(route.exception.code, 400)

    def test_surface_record_reuses_one_atlas_and_link_mode_is_nonlaunching(self):
        reused = ensure_surface(str(self.root))
        self.assertTrue(reused["reused"])
        handoff = open_url(self.base, mode="link")
        self.assertFalse(handoff["opened"])
        with self.assertRaises(ConversationBrowserError):
            open_url(self.base.replace("127.0.0.1", "example.com"), mode="link")

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
            "pid": os.getpid(), "url": "http://127.0.0.1:54321/#token=" + token,
            "token": token,
        }), encoding="utf-8")
        started = ensure_surface(str(root), timeout=1.5)
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

    def test_no_provider_import_surface(self):
        source = (HERE / "_conversation_ui.py").read_text(encoding="utf-8")
        for forbidden in ("subprocess", "socket", "requests", "Popen", "execute_agent"):
            self.assertNotIn(forbidden, source)
        self.assertGreaterEqual(REQUEST_TIMEOUT_SECONDS, 1.0)
        self.assertGreaterEqual(MAX_ACTIVE_CLIENTS, 8)


if __name__ == "__main__":
    unittest.main()

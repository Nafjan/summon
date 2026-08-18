"""Browser-handoff tests: local URL validation, bridge safety, and reuse."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import _deliberation_browser as browser
import _deliberation_store as store
import _deliberation_ui as ui
import _rundir


def _receipt(run_id: str) -> dict:
    return {
        "mode": "deliberation", "schema_version": 1, "run_id": run_id,
        "decision_id": "decision-1", "question_sha256": "a" * 64,
        "seat_ids": ["a", "b"], "option_ids": ["yes", "no"],
        "quorum_rule": "all", "max_attempts": 4,
        "require_human_approval": False, "rounds": 1,
        "deadline_unix_ms": 4_000_000_000_000, "created_at": 1.0,
    }


class DeliberationBrowserTests(unittest.TestCase):
    def setUp(self):
        self.url = "http://127.0.0.1:12345/runs/run-1/#token=" + "t" * 48

    def test_link_mode_is_non_launching_and_validates_loopback_token(self):
        with mock.patch.object(browser.webbrowser, "open") as opened, \
             mock.patch.object(browser.subprocess, "Popen") as process:
            result = browser.open_url(self.url, mode="link")
        self.assertFalse(result["opened"])
        opened.assert_not_called()
        process.assert_not_called()
        for invalid in (
            self.url.replace("127.0.0.1", "example.com"),
            self.url.replace("#token=", "?token=") ,
            "file:///C:/private/run.html",
        ):
            with self.assertRaises(browser.BrowserOpenError):
                browser.open_url(invalid, mode="link")
        with self.assertRaises(browser.BrowserOpenError):
            browser.open_url(self.url.replace("http://", "http://user:pass@"), mode="link")

    def test_system_browser_reuses_existing_window(self):
        with mock.patch.object(browser.webbrowser, "open", return_value=True) as opened:
            result = browser.open_url(self.url, mode="system")
        self.assertEqual(result["target"], "system")
        self.assertTrue(result["reused"])
        opened.assert_called_once_with(self.url, new=0, autoraise=True)

    def test_ide_bridge_receives_only_executable_and_url(self):
        with mock.patch.dict(os.environ, {"SUMMON_BROWSER_BRIDGE": sys.executable}, clear=True), \
             mock.patch.object(browser.subprocess, "Popen") as process:
            result = browser.open_url(self.url, mode="ide")
        self.assertEqual(result["target"], "ide")
        args, kwargs = process.call_args
        self.assertEqual(args[0], [sys.executable, self.url])
        self.assertFalse(kwargs["shell"])
        self.assertNotIn("&&", " ".join(args[0]))

    def test_auto_without_bridge_uses_system_and_ide_without_bridge_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(browser.webbrowser, "open", return_value=True) as opened:
            browser.open_url(self.url, mode="auto")
        opened.assert_called_once()
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(browser.BrowserOpenError):
                browser.open_url(self.url, mode="ide")

    def test_builtin_browser_reuses_tab_script_without_shell(self):
        process = mock.Mock(returncode=0)
        with mock.patch.dict(os.environ, {"BROWSER_USE_AVAILABLE_BACKENDS": "iab"}, clear=True), \
             mock.patch.object(browser.shutil, "which", return_value="browser-harness"), \
             mock.patch.object(browser.subprocess, "Popen", return_value=process) as popen:
            result = browser.open_url(self.url, mode="builtin")
        self.assertEqual(result["target"], "builtin")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["browser-harness"])
        self.assertFalse(kwargs["shell"])
        self.assertIn(self.url.encode("utf-8"), process.communicate.call_args.args[0])
        process.communicate.assert_called_once()

    def test_unreachable_surface_record_is_not_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-1"))
            _rundir.release_owner(owner)
            record_url = "http://127.0.0.1:23456/runs/run-1/#token=" + "u" * 48
            _rundir.atomic_write_json(os.path.join(path, browser._ui.SURFACE_RECORD), {
                "schema_version": 1, "run_id": "run-1", "pid": 999999,
                "url": record_url, "token": "u" * 48,
            })
            with mock.patch.object(browser.subprocess, "Popen") as process:
                with self.assertRaises(browser.BrowserOpenError):
                    browser.ensure_surface(root, "run-1")
            process.assert_called_once()

    def test_reachable_surface_record_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-1"))
            _rundir.release_owner(owner)
            surface = ui.DeliberationSurface(root, "run-1")
            surface.start()
            try:
                with mock.patch.object(browser, "_claim_open_lock",
                                       wraps=browser._claim_open_lock) as claim:
                    result = browser.ensure_surface(root, "run-1")
                self.assertTrue(claim.called)
                self.assertTrue(result["reused"])
                self.assertEqual(result["pid"], os.getpid())
            finally:
                surface.close()

    def test_surface_record_root_binding_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = os.path.join(temp, "deliberations")
            path, owner = store.initialize_run(root, _receipt("run-1"))
            _rundir.release_owner(owner)
            surface = ui.DeliberationSurface(root, "run-1")
            surface.start()
            try:
                record_path = os.path.join(path, ui.SURFACE_RECORD)
                record = _rundir.read_json(record_path)
                record["root_sha256"] = "0" * 64
                _rundir.atomic_write_json(record_path, record)
                self.assertIsNone(ui.read_surface_record(root, "run-1"))
            finally:
                surface.close()

    def test_open_lock_release_cannot_delete_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            lock = os.path.join(temp, "open.lock")
            token = browser._claim_open_lock(lock)
            self.assertIsInstance(token, str)
            _rundir.atomic_write_json(lock, {
                "pid": os.getpid(), "token": "replacement", "started_at": 1,
            })
            browser._release_open_lock(lock, token)
            self.assertTrue(os.path.isfile(lock))

    def test_run_path_rejects_symlinked_root_and_run_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real_root = base / "real"
            real_root.mkdir()
            root_link = base / "root-link"
            run_target = base / "run-target"
            run_target.mkdir()
            try:
                root_link.symlink_to(real_root, target_is_directory=True)
                with self.assertRaises(ValueError):
                    _rundir.run_path(str(root_link), "run-1")
                child = real_root / "run-1"
                child.symlink_to(run_target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlinks unavailable: {exc}")
            with self.assertRaises(ValueError):
                _rundir.run_path(str(real_root), "run-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)

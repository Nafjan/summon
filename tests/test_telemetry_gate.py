from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("summon_telemetry_gate", ROOT / "skills" / "summon" / "scripts" / "_telemetry.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class TelemetryGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.config = base / "config.json"
        self.events = base / "events.jsonl"
        self.reports = base / "reports"
        self.env = {
            "SUMMON_TELEMETRY_CONFIG": str(self.config),
            "SUMMON_TELEMETRY_FILE": str(self.events),
            "SUMMON_REPORTS_DIR": str(self.reports),
        }
        self.old = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        os.environ.pop("SUMMON_TELEMETRY", None)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_clean_home_is_disabled_and_env_override_wins(self):
        self.assertFalse(MODULE.enabled())
        os.environ["SUMMON_TELEMETRY"] = "1"
        self.assertTrue(MODULE.enabled())
        os.environ["SUMMON_TELEMETRY"] = "0"
        self.assertFalse(MODULE.enabled())

    def test_record_is_bounded_and_redacts_secrets_paths_and_prompt(self):
        os.environ["SUMMON_TELEMETRY"] = "1"
        envelope = {
            "status": "error", "cli": "claude", "permission": "safe-edit",
            "error": "Bearer sk-super-secret /tmp/private/repo prompt=do not publish",
            "model": {"requested": "claude-opus-5", "served": "claude-opus-5"},
            "prompt_sha256": "a" * 64,
            "summon": {"version": "3.0.0", "scripts_sha256": "b" * 64},
            "workspace_evidence": {"coverage": "full"},
        }
        event = MODULE.record(envelope)
        self.assertIsInstance(event, dict)
        raw = self.events.read_text(encoding="utf-8")
        self.assertLessEqual(self.events.stat().st_size, MODULE._MAX_EVENT_FILE_BYTES)
        self.assertNotIn("sk-super-secret", raw)
        self.assertNotIn("/tmp/private/repo", raw)
        self.assertNotIn("do not publish", raw)
        json.loads(raw)

    def test_clear_does_not_change_opt_in(self):
        MODULE.set_enabled(True)
        self.assertTrue(MODULE.enabled())
        MODULE.record({"status": "success", "cli": "claude"})
        MODULE.clear_events()
        self.assertTrue(MODULE.enabled())


if __name__ == "__main__":
    unittest.main()

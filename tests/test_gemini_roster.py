"""Provider-inert tests for current Gemini defaults and historical identity."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "summon" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import _model_catalog as catalog
import _executor
from _builder import AgentInvocation
from _loader import load_agent
from _executor import model_exact_policy
from run_subagent import _apply_gemini_thinking


class GeminiRosterTests(unittest.TestCase):
    def test_all_bundled_gemini_defaults_are_pinned(self):
        agents = ROOT / "skills" / "summon" / "agents"
        for name in ("flash-reviewer", "researcher", "frontend", "docs-writer", "antigravity"):
            with self.subTest(name=name):
                definition = load_agent(str(agents), name)
                self.assertEqual(definition[0], "agy")
                self.assertEqual(definition[5], "gemini-3.8-flash-high")

    def test_advisory_seat_does_not_weaken_researcher_exact_contract(self):
        self.assertEqual(model_exact_policy("researcher"), (True, "named-seat"))
        for name in ("flash-reviewer", "frontend", "docs-writer", "antigravity"):
            with self.subTest(name=name):
                self.assertEqual(model_exact_policy(name), (False, None))
                self.assertEqual(model_exact_policy(name, explicit=True), (True, "cli"))

    def test_completed_agy_report_is_advisory_or_exact_blocked(self):
        terminal = {"event": "result", "result": {"status": "SUCCESS",
                    "response": "STATUS: DONE\nSUMMARY: inspected fixture\nFOLLOW-UP: none\nHANDOFF: none",
                    "usage": {"output_tokens": 10}}}
        code = "print(" + repr(json.dumps(terminal)) + ")"
        for seat, expected in (("flash-reviewer", "success"), ("researcher", "blocked")):
            with self.subTest(seat=seat):
                exact, source = model_exact_policy(seat)
                inv = AgentInvocation(cli="agy", model="gemini-3.8-flash-high",
                    prompt="fixture", cwd=tempfile.gettempdir(), permission="yolo",
                    model_exact_required=exact, model_exact_source=source)
                with mock.patch.object(_executor, "build_invocation_args",
                        return_value=(sys.executable, ("-c", code), None)), \
                     mock.patch.object(_executor, "_agy_stream_wrapper", return_value=True), \
                     mock.patch("_receipt.workspace_snapshot", return_value={"coverage": "none"}), \
                     mock.patch("_receipt.workspace_evidence", return_value={}):
                    result = _executor.execute_agent(inv, timeout_ms=5000)
                self.assertEqual(result["status"], expected)
                self.assertTrue(result["report_ok"], repr((result.get("result"), result.get("report"))))
                self.assertNotEqual(result["served_model_evidence"], "reported")
                self.assertFalse(result["named_model_verified"])
                if exact:
                    self.assertIsNone(result["model"]["served"])
                    self.assertEqual(result["served_model_evidence"], "absent")
                    self.assertFalse(result["result_usable"])
                    self.assertEqual(result["error_kind"], "served_model_unverified")

    def test_effort_override_keeps_generation(self):
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort):
                self.assertEqual(
                    _apply_gemini_thinking("gemini-3.8-flash-high", effort),
                    "gemini-3.8-flash-" + effort)
        self.assertEqual(_apply_gemini_thinking("gemini-3.7-flash-high", "low"),
                         "gemini-3.7-flash-low")
        self.assertEqual(_apply_gemini_thinking("gemini-3.8-flash-medium", "max"),
                         "gemini-3.8-flash-high")
        self.assertEqual(_apply_gemini_thinking("Gemini 3.8 Flash (High)", "low"),
                         "Gemini 3.8 Flash (Low)")
        self.assertEqual(_apply_gemini_thinking("gemini-3.8-flash-high", "none"),
                         "gemini-3.8-flash-high")

    def test_current_and_historical_identity_stay_separate(self):
        for version in ("3.8", "3.7"):
            with self.subTest(version=version):
                model = "gemini-" + version + "-flash-high"
                display = catalog.display_for("agy", model)
                self.assertEqual(display["version"], version)
                self.assertFalse(display["served_exact"])
                self.assertEqual(display["availability"], "catalog_listed")
                hashed = catalog.display_for_hash("agy", hashlib.sha256(model.encode()).hexdigest())
                self.assertEqual(hashed["version"], version)
        self.assertEqual(catalog.display_for("agy", "gemini-3.8-flash-high")["label"], "frontier")
        self.assertIsNone(catalog.display_for("agy", "gemini-3.9-flash-high"))


if __name__ == "__main__":
    unittest.main()

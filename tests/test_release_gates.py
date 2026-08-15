from __future__ import annotations

import importlib.util
import inspect
from unittest import mock
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_gates", ROOT / "tools" / "release_gates.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ReleaseGateRunnerTests(unittest.TestCase):
    def test_registry_matches_manifest_required_tests(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "release_manifest", ROOT / "tools" / "release_manifest.py"
        )
        manifest = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(manifest)
        self.assertEqual(set(MODULE.COMMANDS), manifest.REQUIRED_TESTS)
        self.assertEqual(MODULE.COMMANDS, manifest.REQUIRED_COMMANDS)
        self.assertEqual(set(MODULE.GATE_COMMANDS), manifest.REQUIRED_GATES)
        self.assertEqual(MODULE.GATE_COMMANDS, manifest.REQUIRED_GATE_COMMANDS)

    def test_runner_has_no_free_form_gate_override_and_is_hermetic(self):
        self.assertNotIn('add_argument("--gate"', inspect.getsource(MODULE.main))
        env = MODULE._hermetic_environment()
        self.assertEqual(env["SUMMON_ACP_FALLBACK"], "0")
        self.assertEqual(env["SUMMON_TELEMETRY"], "0")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("OPENROUTER_BASE_URL", env)

    def test_explicit_redacted_live_receipt_path_is_forwarded_only(self):
        with mock.patch.dict(MODULE.os.environ, {
            "SUMMON_LIVE_PROVIDER_RECEIPT": "C:/temp/redacted-live-provider-receipt.json",
            "OPENAI_API_KEY": "SECRET",
        }, clear=False):
            env = MODULE._hermetic_environment()
        self.assertEqual(env["SUMMON_LIVE_PROVIDER_RECEIPT"],
                         "C:/temp/redacted-live-provider-receipt.json")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_machine_result_digest_shape(self):
        digest = "a" * 64
        self.assertTrue(MODULE.re.fullmatch(r"[0-9a-f]{64}", digest))

    def test_custom_and_unittest_counts_are_strict(self):
        self.assertEqual(MODULE._parse_count("x", "\n36/36 passed\n"), "36/36")
        self.assertEqual(MODULE._parse_count("x", "Ran 6 tests\n\nOK\n"), "6/6")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "0/0 passed\n")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "Ran 0 tests\n\nOK\n")


if __name__ == "__main__":
    unittest.main()

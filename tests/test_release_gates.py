from __future__ import annotations

import importlib.util
import inspect
import hashlib
import io
import json
import contextlib
import subprocess
import tempfile
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
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

    def test_git_metadata_timeout_is_bounded_but_windows_tolerant(self):
        self.assertGreaterEqual(MODULE.GIT_METADATA_TIMEOUT_SECONDS, 30.0)
        self.assertLessEqual(MODULE.GIT_METADATA_TIMEOUT_SECONDS, 120.0)

    def test_git_metadata_calls_use_the_bounded_timeout(self):
        completed = [
            SimpleNamespace(stdout=str(ROOT)),
            SimpleNamespace(stdout="a" * 40),
        ]
        with mock.patch.object(MODULE.subprocess, "run", side_effect=completed) as run:
            self.assertEqual(MODULE._git_head(), "a" * 40)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(
            call.kwargs["timeout"] == MODULE.GIT_METADATA_TIMEOUT_SECONDS
            for call in run.call_args_list
        ))

        status = SimpleNamespace(stdout="")
        with (
            mock.patch.object(MODULE, "COMMANDS", {}),
            mock.patch.object(MODULE, "GATE_COMMANDS", {}),
            mock.patch.object(MODULE, "_source_hash", side_effect=["b" * 64, "b" * 64]),
            mock.patch.object(MODULE, "_git_head", side_effect=["c" * 40, "c" * 40]),
            mock.patch.object(MODULE.platform, "platform", return_value="Windows-test"),
            mock.patch.object(MODULE.subprocess, "run", side_effect=[status, status]) as run,
        ):
            MODULE.build_evidence(1.0)
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(
            call.kwargs["timeout"] == MODULE.GIT_METADATA_TIMEOUT_SECONDS
            for call in run.call_args_list
        ))

    def test_git_timeout_fails_closed_without_evidence(self):
        errors = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "evidence.json"
            with (
                mock.patch.object(
                    MODULE,
                    "build_evidence",
                    side_effect=subprocess.TimeoutExpired(["git", "status"], 60),
                ),
                contextlib.redirect_stderr(errors),
            ):
                result = MODULE.main(["--output", str(output)])
            self.assertEqual(result, 2)
            self.assertFalse(output.exists())
        self.assertIn("timed out", errors.getvalue())

    def test_outer_artifact_hash_is_recomputable_with_gate_marker_hash(self):
        artifact = MODULE._artifact(
            "live_provider", status="pass", command="python tools/live_provider_gate.py",
            source_hash="a" * 64, git_head="b" * 40, output="gate output\n",
            marker={"status": "pass", "artifact_sha256": "c" * 64,
                    "evidence_file": "redacted-live-provider-receipt.json"},
        )
        payload = dict(artifact)
        stored = payload.pop("artifact_sha256")
        self.assertEqual(artifact["evidence_sha256"], "c" * 64)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        self.assertEqual(stored, hashlib.sha256(encoded).hexdigest())

    def test_custom_and_unittest_counts_are_strict(self):
        self.assertEqual(MODULE._parse_count("x", "\n36/36 passed\n"), "36/36")
        self.assertEqual(MODULE._parse_count("x", "Ran 6 tests\n\nOK\n"), "6/6")
        self.assertEqual(MODULE._parse_count(
            "x", "================ 61 passed in 3.25s ================\n"), "61/61")
        self.assertEqual(MODULE._parse_count(
            "x", "============ 59 passed, 2 skipped in 4.10s ============\n"), "59/61")
        self.assertEqual(MODULE._parse_count(
            "x", "831 passed, 1 skipped, 26 subtests passed in 100.33s\n"),
            "831/832")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "10 passed, 1 xfailed in 1.00s\n")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "0/0 passed\n")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "Ran 0 tests\n\nOK\n")
        with self.assertRaises(RuntimeError):
            MODULE._parse_count("x", "2 failed, 59 passed in 4.10s\n")

    def test_cli_rejects_in_tree_evidence_output(self):
        output = ROOT / "tools" / ".release-gates-test-output.json"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = MODULE.main(["--output", str(output)])
        self.assertEqual(result, 2)
        self.assertIn("release evidence output must be outside", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

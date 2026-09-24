from __future__ import annotations

import importlib.util
import inspect
import hashlib
import io
import json
import contextlib
import shlex
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
    def test_ce_native_zoom_registry_and_finite_ci_retention(self):
        native = {"native-zoom-measurements.json"} | {
            f"native-zoom-{factor}-{surface}.png"
            for factor in (1, 2, 4) for surface in ("workspace", "artifact")}
        delivery = {
            "u04-worker-state-card-" + state + ".png"
            for state in ("accepted", "queued", "included-in-attempt",
                          "submission-started", "submitted", "acknowledged",
                          "not-submitted")
        }
        manifest = MODULE._MANIFEST
        fixture = "skills/summon/scripts/test_workspace_zoom_rendered.py"
        self.assertIn(fixture, MODULE.COMMANDS["workspace_ui"])
        self.assertIn(fixture, MODULE.GATE_COMMANDS["accessibility"])
        self.assertTrue(native <= manifest.REQUIRED_RENDERED_FILES)
        self.assertTrue(delivery <= manifest.REQUIRED_RENDERED_FILES)
        for name in native:
            self.assertTrue(MODULE._rendered_file_allowed(name))
            self.assertIsNotNone(manifest.RENDERED_NAME.fullmatch(name))
        for name in delivery:
            self.assertTrue(MODULE._rendered_file_allowed(name))
            self.assertIsNotNone(manifest.RENDERED_NAME.fullmatch(name))
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        jobs = workflow.split("  release-evidence-windows:", 1)[1].split("  release-evidence-info:", 1)
        for job in jobs:
            self.assertIn("p.chromium.executable_path", job)
            self.assertIn("--chromium-executable", job)
            uploaded = {line.strip().split("release-evidence.gates/", 1)[1]
                        for line in job.splitlines() if "runner.temp" in line and "release-evidence.gates/" in line}
            expected = {f"gate.{name}.json" for name in manifest.REQUIRED_GATES}
            expected |= {"rendered/" + name for name in manifest.REQUIRED_RENDERED_FILES}
            self.assertEqual(uploaded, expected)
            self.assertFalse(any("*" in name for name in uploaded))
            self.assertNotIn("gate.unrelated.json", uploaded)
        self.assertIn("validate_incomplete_diagnostics", jobs[1])
        self.assertNotIn("python tools/release_manifest.py", jobs[1])
        self.assertNotIn("release-manifest.json", jobs[1])
        self.assertIn("if: success()", jobs[1])

    def test_ce_numeric_schema_inventory_rows(self):
        spec = importlib.util.spec_from_file_location("ce_inventory", ROOT / "tools/format_inventory.py")
        inventory = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inventory)
        rows = {row["id"]: row for row in inventory.validate_inventory(ROOT)["formats"]}
        for identifier in ("release-evidence-platform-qualification", "release-evidence-rendered-bundle"):
            row = rows[identifier]
            self.assertEqual(row["identity"]["kind"], "numeric")
            self.assertEqual(row["identity"]["version"], 1)
            self.assertEqual(row["identity"]["discriminator"], "schema")
            self.assertEqual(row["authority"], "private_evidence")
            self.assertTrue(row["fixtures"] and row["evidence_gaps"])


    def test_runtime_regressions_remain_in_related_fixed_commands_and_ci(self):
        scripts = "skills/summon/scripts/"
        expected = {
            "test_executor_observation_regressions.py": ("phase0_phase1",),
            "test_attempt_and_wait_regressions.py": ("phase0_phase1",),
            "test_fork_and_claim_regressions.py": ("phase0_phase1", "swarm_coordinator"),
            "test_workspace_capability_snapshot.py": ("workspace_ui",),
        }
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for filename, suites in expected.items():
            token = scripts + filename
            self.assertTrue((ROOT / token).is_file())
            for suite in suites:
                argv = shlex.split(MODULE.COMMANDS[suite])
                self.assertEqual(argv.count(token), 1)
                self.assertEqual(argv[:3], ["python", "-m", "pytest"])
            self.assertIn(token, workflow)
        capability = scripts + "test_workspace_capability_snapshot.py"
        self.assertEqual(shlex.split(MODULE.GATE_COMMANDS["browser_security"]).count(capability), 1)
        phase_ci = workflow.split("          skills/summon/scripts/test_phase0_contracts.py", 1)[1].split("      - name:", 1)[0]
        for filename in tuple(expected)[:3]:
            self.assertIn(scripts + filename, phase_ci)
        workspace_ci = workflow.split("      - name: Unit tests (workspace UI loopback)", 1)[1].split("      - name:", 1)[0]
        self.assertIn(capability, workspace_ci)

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

    def test_workspace_demo_has_a_separate_bounded_release_partition(self):
        self.assertIn("workspace_demo", MODULE.COMMANDS)
        self.assertNotIn("test_workspace_demo.py", MODULE.COMMANDS["workspace_core"])
        self.assertEqual(
            MODULE.COMMANDS["workspace_demo"],
            "python -m pytest -q skills/summon/scripts/test_workspace_demo.py",
        )

    def test_runner_has_no_free_form_gate_override_and_is_hermetic(self):
        self.assertNotIn('add_argument("--gate"', inspect.getsource(MODULE.main))
        env = MODULE._hermetic_environment()
        self.assertEqual(env["SUMMON_ACP_FALLBACK"], "0")
        self.assertEqual(env["SUMMON_TELEMETRY"], "0")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("AWS_ACCESS_KEY_ID", env)
        self.assertNotIn("OPENROUTER_BASE_URL", env)

    def test_ui_inputs_are_explicitly_bound_and_ambient_values_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            rendered = root / "rendered"
            browser = root / ("chrome.exe" if MODULE.os.name == "nt" else "chrome")
            browser.write_bytes(b"synthetic browser placeholder")
            with mock.patch.dict(MODULE.os.environ, {
                "SUMMON_UI_RENDERED_EVIDENCE_DIR": "C:/ambient/private",
                "SUMMON_UI_CHROMIUM_EXECUTABLE": "C:/ambient/private/chrome",
            }, clear=False):
                env = MODULE._hermetic_environment(
                    rendered_evidence_dir=rendered,
                    chromium_executable=browser,
                )
            self.assertEqual(Path(env["SUMMON_UI_RENDERED_EVIDENCE_DIR"]), rendered)
            self.assertEqual(Path(env["SUMMON_UI_CHROMIUM_EXECUTABLE"]), browser)
            self.assertTrue(rendered.is_dir())

    def test_ui_inputs_reject_missing_browser_and_in_tree_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ValueError):
                MODULE._hermetic_environment(
                    rendered_evidence_dir=ROOT / "tests" / "rendered")
            with self.assertRaises(ValueError):
                MODULE._hermetic_environment(
                    chromium_executable=Path(temp_dir) / "missing-browser")

    def test_non_windows_platform_qualification_never_claims_windows_proof(self):
        with mock.patch.object(MODULE.os, "name", "posix"):
            value = MODULE._platform_qualification({
                "migration_rollback": "pass",
                "browser_security": "pass",
                "accessibility": "pass",
            })
        self.assertEqual(value["host"], "non_windows")
        self.assertEqual(value["windows_evidence"], "unavailable")
        self.assertEqual(value["status"], "incomplete")
        self.assertEqual(set(value), {
            "schema", "host", "windows_evidence", "windows_scoped_gates", "status",
        })
        self.assertEqual(set(value["windows_scoped_gates"]), {
            "migration_rollback", "browser_security", "accessibility",
        })
        self.assertEqual(set(value["windows_scoped_gates"].values()), {"unavailable"})

    def test_ci_retains_explicit_browser_prerequisites_and_sanitized_artifacts(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('"playwright>=1.52,<2"', workflow)
        self.assertIn("python -m playwright install --with-deps chromium", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("release-evidence.gates/gate.accessibility.json", workflow)
        self.assertIn("release-evidence.gates/gate.live_provider.json", workflow)
        self.assertIn("release-evidence.gates/rendered/browser-toolchain.json", workflow)
        self.assertIn("release-evidence.gates/rendered/u08-observations.json", workflow)
        self.assertIn("release-evidence.gates/rendered/u04-worker-state-card-acknowledged.png", workflow)
        upload = workflow[workflow.index("actions/upload-artifact@v4"):]
        self.assertNotIn("/**/*.log", upload)
        self.assertNotIn("**/*", upload)
        self.assertIn('evidence["platform_qualification"]', workflow)

    def test_rendered_bundle_is_bounded_source_bound_and_rejects_stale_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "rendered"
            directory.mkdir()
            (directory / "browser-toolchain.json").write_text(
                '{"providers":"not invoked"}\n', encoding="utf-8")
            value = MODULE._collect_rendered_evidence(directory, "a" * 64)
            self.assertEqual(value["source_tree_sha256"], "a" * 64)
            self.assertEqual(value["files"][0]["name"], "browser-toolchain.json")
            (directory / "unexpected.log").write_text("private", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                MODULE._collect_rendered_evidence(directory, "a" * 64)

    def test_fixed_release_commands_reference_existing_python_files(self):
        commands = list(MODULE.COMMANDS.values()) + list(MODULE.GATE_COMMANDS.values())
        missing = []
        for command in commands:
            for token in shlex.split(command, posix=True):
                if token.endswith(".py") and not (ROOT / token).is_file():
                    missing.append(token)
        self.assertEqual(missing, [])

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

    def test_release_suite_timeout_is_bounded_for_windows_aggregate(self):
        self.assertEqual(MODULE.RELEASE_SUITE_TIMEOUT_SECONDS, 1800.0)
        self.assertGreater(MODULE.RELEASE_SUITE_TIMEOUT_SECONDS,
                           MODULE.GIT_METADATA_TIMEOUT_SECONDS)
        self.assertLessEqual(MODULE.RELEASE_SUITE_TIMEOUT_SECONDS, 3600.0)

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

    def test_artifact_redacts_untrusted_marker_strings(self):
        artifact = MODULE._artifact(
            "browser_security", status="blocked",
            command="python -m pytest -q", source_hash="a" * 64,
            git_head="b" * 40, output="private output",
            marker={
                "error_kind": "provider-path-C:/Users/private",
                "detail": "secret token /private/customer",
                "evidence_file": "C:/private/receipt.json",
            },
        )
        encoded = json.dumps(artifact)
        self.assertNotIn("private", encoded.casefold())
        self.assertNotIn("customer", encoded.casefold())
        self.assertEqual(artifact["error_kind"], "gate_diagnostic_redacted")
        self.assertEqual(artifact["detail"], "gate_diagnostic_redacted")
        self.assertNotIn("evidence_file", artifact)

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

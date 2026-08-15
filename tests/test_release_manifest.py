from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "release_manifest", ROOT / "tools" / "release_manifest.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class ReleaseManifestTests(unittest.TestCase):
    def test_source_hash_is_deterministic_and_excludes_python_cache(self):
        first = MODULE.source_tree_sha256(ROOT)
        second = MODULE.source_tree_sha256(ROOT)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)

    def test_manifest_contains_explicit_tests_and_gates_without_private_paths(self):
        manifest = MODULE.build_manifest(
            ROOT, tests={"deliberation": "334/334"},
            gates={"live_provider": "blocked"},
        )
        self.assertEqual(manifest["tests"], {"deliberation": "334/334"})
        self.assertEqual(manifest["known_gates"]["live_provider"], "blocked")
        encoded = json.dumps(manifest)
        self.assertNotIn(str(ROOT), encoded)

    def test_pairs_reject_malformed_and_duplicate_facts(self):
        with self.assertRaises(ValueError):
            MODULE._pairs(["missing-separator"], "test")
        with self.assertRaises(ValueError):
            MODULE._pairs(["suite=1", "suite=2"], "test")

    def test_pairs_reject_unknown_names_and_invalid_fact_shapes(self):
        with self.assertRaises(ValueError):
            MODULE._pairs(["invented=1/1"], "test",
                           allowed=MODULE.REQUIRED_TESTS)
        with self.assertRaises(ValueError):
            MODULE._pairs(["deliberation=passed"], "test",
                           allowed=MODULE.REQUIRED_TESTS)
        with self.assertRaises(ValueError):
            MODULE._pairs(["deliberation=0/1"], "test",
                           allowed=MODULE.REQUIRED_TESTS)
        with self.assertRaises(ValueError):
            MODULE._pairs(["deliberation=2/1"], "test",
                           allowed=MODULE.REQUIRED_TESTS)
        with self.assertRaises(ValueError):
            MODULE._pairs(["deliberation=0/0"], "test",
                           allowed=MODULE.REQUIRED_TESTS)
        with self.assertRaises(ValueError):
            MODULE._pairs(["live_provider=maybe"], "gate",
                           allowed=MODULE.REQUIRED_GATES)

    def test_check_requires_the_complete_named_contract(self):
        manifest = MODULE.build_manifest(
            ROOT,
            tests={"deliberation": "1/1"},
            gates={"live_provider": "blocked"},
        )
        failures = MODULE._check_facts(manifest)
        self.assertTrue(any("missing test evidence" in item for item in failures))
        self.assertTrue(any("missing gate evidence" in item for item in failures))

    def test_check_rejects_complete_but_failed_facts(self):
        tests = {name: "0/1" for name in MODULE.REQUIRED_TESTS}
        gates = {name: "not_run" for name in MODULE.REQUIRED_GATES}
        manifest = MODULE.build_manifest(ROOT, tests=tests, gates=gates)
        failures = MODULE._check_facts(manifest)
        self.assertTrue(any("not a passing count" in item for item in failures))
        self.assertTrue(any("gate is not passing" in item for item in failures))

    def test_terms_md_is_in_payload_when_present(self):
        included = {p.relative_to(ROOT).as_posix() for p in MODULE._included_files(ROOT)}
        self.assertIn("TERMS.md", included)

    def test_check_rejects_evidence_without_head_and_command_registry(self):
        manifest = MODULE.build_manifest(
            ROOT,
            tests={name: "1/1" for name in MODULE.REQUIRED_TESTS},
            gates={name: "pass" for name in MODULE.REQUIRED_GATES},
            evidence={
                "schema": 1,
                "source_tree_sha256": MODULE.source_tree_sha256(ROOT),
                "commands": {},
            },
        )
        failures = MODULE._check_facts(manifest)
        self.assertIn("evidence does not match the current Git HEAD", failures)

    def test_check_requires_machine_result_digests_and_exact_commands(self):
        evidence = {
            "schema": 1,
            "source_tree_sha256": MODULE.source_tree_sha256(ROOT),
            "git_head": MODULE._git_facts(ROOT)["head"],
            "producer": "tools/release_gates.py",
            "producer_sha256": "a" * 64,
            "commands": {name: "echo forged" for name in MODULE.REQUIRED_TESTS},
            "test_results": {
                name: {"count": "1/1", "output_sha256": "a" * 64}
                for name in MODULE.REQUIRED_TESTS
            },
        }
        manifest = MODULE.build_manifest(
            ROOT,
            tests={name: "1/1" for name in MODULE.REQUIRED_TESTS},
            gates={name: "pass" for name in MODULE.REQUIRED_GATES},
            evidence=evidence,
        )
        failures = MODULE._check_facts(manifest)
        self.assertIn("evidence command registry does not match required suites", failures)

    def test_evidence_reader_rejects_fabricated_passing_gates(self):
        evidence = {
            "schema": 1,
            "source_tree_sha256": MODULE.source_tree_sha256(ROOT),
            "git_head": MODULE._git_facts(ROOT)["head"],
            "producer": "tools/release_gates.py",
            "producer_sha256": MODULE._sha256_file(ROOT / "tools" / "release_gates.py"),
            "commands": dict(MODULE.REQUIRED_COMMANDS),
            "tests": {name: "1/1" for name in MODULE.REQUIRED_TESTS},
            "known_gates": {name: "pass" for name in MODULE.REQUIRED_GATES},
            "test_results": {
                name: {"count": "1/1", "output_sha256": "a" * 64}
                for name in MODULE.REQUIRED_TESTS
            },
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaises(ValueError):
                MODULE._read_evidence(path, ROOT, evidence["source_tree_sha256"])

    def test_git_status_failure_is_unknown_not_clean(self):
        top = SimpleNamespace(returncode=0, stdout=str(ROOT), stderr="")
        head = SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        failed_status = SimpleNamespace(returncode=1, stdout="", stderr="status failed")
        with patch.object(MODULE.subprocess, "run", side_effect=[top, head, failed_status]):
            facts = MODULE._git_facts(ROOT)
        self.assertIsNone(facts["dirty"])
        self.assertFalse(facts["status_available"])

    def test_cli_check_fails_for_missing_or_drifted_managed_install(self):
        with tempfile.TemporaryDirectory() as temp:
            result = MODULE.main(["--root", temp, "--check"])
        self.assertEqual(result, 2)

    def test_cli_expected_version_is_machine_checked(self):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = MODULE.main(["--root", str(ROOT), "--expected-version", "2.2.0"])
        self.assertEqual(result, 2)
        self.assertIn("--expected-version", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

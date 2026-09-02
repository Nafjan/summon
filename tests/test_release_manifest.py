from __future__ import annotations

import importlib.util
import contextlib
import io
import json
from pathlib import Path
import re
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
    def test_pytest_cache_directories_are_not_installed_or_fingerprinted(self):
        install_spec = importlib.util.spec_from_file_location(
            "summon_install_cache_test", ROOT / "install.py")
        install = importlib.util.module_from_spec(install_spec)
        install_spec.loader.exec_module(install)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill = root / "skills" / "summon"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            product = scripts / "runner.py"
            product.write_text("original\n", encoding="utf-8")
            cache = scripts / ".pytest_cache" / "v" / "cache" / "nodeids"
            cache.parent.mkdir(parents=True)
            cache.write_text('["synthetic-test"]', encoding="utf-8")
            dotfile = scripts / ".product-config"
            dotfile.write_text("bound\n", encoding="utf-8")

            source_before = MODULE.source_tree_sha256(root)
            fingerprint_before, files_before, error = MODULE._tree_fingerprint(skill)
            self.assertIsNone(error)
            self.assertEqual(files_before, {"scripts/runner.py", "scripts/.product-config"})
            for index, contents in enumerate(('[]', '["changed-test", "another-test"]')):
                cache.write_text(contents, encoding="utf-8")
                self.assertEqual(source_before, MODULE.source_tree_sha256(root))
                self.assertEqual((fingerprint_before, files_before, None), MODULE._tree_fingerprint(skill))
                target = root / f"installed-{index}"
                target.mkdir()
                with patch.object(install, "SKILL_SRC", str(skill)):
                    installed_files = install._build_tree(str(target))
                self.assertEqual({Path(name).as_posix() for name in installed_files}, files_before)
                self.assertFalse((target / "scripts" / ".pytest_cache").exists())
                self.assertEqual((fingerprint_before, files_before, None), MODULE._tree_fingerprint(target))

            # Existing manifests listing cache entries are NOT silently normalized.
            stale_manifest_files = files_before | {"scripts/.pytest_cache/v/cache/nodeids"}
            self.assertNotEqual(stale_manifest_files, MODULE._tree_fingerprint(skill)[1])
            product.write_text("changed product\n", encoding="utf-8")
            self.assertNotEqual(source_before, MODULE.source_tree_sha256(root))
            self.assertNotEqual(fingerprint_before, MODULE._tree_fingerprint(skill)[0])
            before_dot_change = MODULE.source_tree_sha256(root)
            dotfile.write_text("changed config\n", encoding="utf-8")
            self.assertNotEqual(before_dot_change, MODULE.source_tree_sha256(root))

    def test_pytest_cache_named_regular_file_remains_product_payload(self):
        install_spec = importlib.util.spec_from_file_location(
            "summon_install_cache_file_test", ROOT / "install.py")
        install = importlib.util.module_from_spec(install_spec)
        install_spec.loader.exec_module(install)
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill = root / "skills" / "summon"
            scripts = skill / "scripts"
            scripts.mkdir(parents=True)
            ordinary = scripts / ".pytest_cache"
            ordinary.write_text("real product\n", encoding="utf-8")
            before = MODULE.source_tree_sha256(root)
            digest, files, error = MODULE._tree_fingerprint(skill)
            self.assertIsNone(error)
            self.assertEqual(files, {"scripts/.pytest_cache"})
            target = root / "installed"
            target.mkdir()
            with patch.object(install, "SKILL_SRC", str(skill)):
                copied = install._build_tree(str(target))
            self.assertEqual({Path(name).as_posix() for name in copied}, files)
            self.assertEqual((target / "scripts" / ".pytest_cache").read_text(), "real product\n")
            ordinary.write_text("changed product\n", encoding="utf-8")
            self.assertNotEqual(before, MODULE.source_tree_sha256(root))
            self.assertNotEqual(digest, MODULE._tree_fingerprint(skill)[0])

    def test_stale_manifest_listing_pytest_cache_fails_install_validation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "source"
            scripts = root / "skills" / "summon" / "scripts"
            scripts.mkdir(parents=True)
            installed = Path(temp_dir) / "installed" / "summon"
            cache = installed / "scripts" / ".pytest_cache" / "v" / "cache" / "nodeids"
            cache.parent.mkdir(parents=True)
            cache.write_text("[]", encoding="utf-8")
            (installed / "scripts" / "runner.py").write_text("pass\n", encoding="utf-8")
            (root / "plugin.json").write_text('{"version":"3.4.0"}', encoding="utf-8")
            (installed / ".summon-install.json").write_text(json.dumps({
                "installed_by": "summon", "installed_at": 1, "version": "3.4.0",
                "files": ["scripts/runner.py", "scripts/.pytest_cache/v/cache/nodeids"],
            }), encoding="utf-8")
            record = {"label": "synthetic-host", "managed": True, "present": True,
                      "scripts_dir": str(installed / "scripts"), "sha256": "a" * 64}
            # Hermetic detector, not a scan of the operator's installation roots.
            (scripts / "_installs.py").write_text(
                f"RECORD = {record!r}\n"
                "def enumerate_installs(**kwargs): return [RECORD]\n"
                "def drift_report(records): return {'reference_sha': 'a'*64, 'hashed': records}\n",
                encoding="utf-8")
            facts = MODULE._install_facts(root)
            self.assertTrue(facts["available"])
            self.assertFalse(facts["managed_converged"])
            self.assertFalse(facts["managed"][0]["ownership_valid"])
            self.assertEqual(facts["managed"][0]["payload_error"],
                             "ownership manifest file list does not match tree")

    def test_cache_exclusion_does_not_hide_product_symlinks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scripts = root / "skills" / "summon" / "scripts"
            scripts.mkdir(parents=True)
            target = root / "outside.txt"
            target.write_text("synthetic\n", encoding="utf-8")
            link = scripts / "product-link"
            try:
                link.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaisesRegex(ValueError, "symlink"):
                MODULE.source_tree_sha256(root)
            self.assertIsNotNone(MODULE._tree_fingerprint(scripts.parent)[2])

    def test_managed_skill_fingerprint_matches_installer_payload(self):
        install_spec = importlib.util.spec_from_file_location(
            "summon_install_for_manifest_test", ROOT / "install.py"
        )
        install = importlib.util.module_from_spec(install_spec)
        assert install_spec and install_spec.loader
        install_spec.loader.exec_module(install)
        self.assertEqual(MODULE._MANAGED_SKILL_PAYLOAD, frozenset(install.SKILL_PAYLOAD))

        with tempfile.TemporaryDirectory() as temp_dir:
            skill = Path(temp_dir) / "summon"
            (skill / "scripts").mkdir(parents=True)
            (skill / "tests").mkdir()
            (skill / "SKILL.md").write_text("skill\n", encoding="utf-8")
            (skill / "scripts" / "runner.py").write_text("pass\n", encoding="utf-8")
            (skill / "tests" / "release_only.py").write_text("ignored\n", encoding="utf-8")
            first_hash, first_files, first_error = MODULE._tree_fingerprint(
                skill, include_top_level=MODULE._MANAGED_SKILL_PAYLOAD
            )
            self.assertIsNone(first_error)
            self.assertEqual(first_files, {"SKILL.md", "scripts/runner.py"})

            (skill / "tests" / "release_only.py").write_text("changed\n", encoding="utf-8")
            second_hash, second_files, second_error = MODULE._tree_fingerprint(
                skill, include_top_level=MODULE._MANAGED_SKILL_PAYLOAD
            )
            self.assertIsNone(second_error)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_files, second_files)

            (skill / "scripts" / "runner.py").write_text("changed\n", encoding="utf-8")
            third_hash, _, third_error = MODULE._tree_fingerprint(
                skill, include_top_level=MODULE._MANAGED_SKILL_PAYLOAD
            )
            self.assertIsNone(third_error)
            self.assertNotEqual(first_hash, third_hash)

    def test_phase0_phase1_release_command_matches_ci_partition(self):
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        start = ci.index("Unit tests (Phase 0 and Phase 1 provider-inert contracts)")
        end = ci.index("Unit tests (ACP transport", start)
        pattern = r"skills/summon/(?:scripts|tests)/test_[A-Za-z0-9_]+\.py"
        ci_files = set(re.findall(pattern, ci[start:end]))
        release_files = set(re.findall(
            pattern, MODULE.REQUIRED_COMMANDS["phase0_phase1"]))
        self.assertEqual(release_files, ci_files)

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
        self.assertEqual(
            MODULE._pairs(["deliberation=835/836"], "test",
                          allowed=MODULE.REQUIRED_TESTS),
            {"deliberation": "835/836"},
        )
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

    def test_managed_install_gate_ignores_unmanaged_drift_but_blocks_managed_drift(self):
        records = [
            {"label": "host-a", "managed": True, "present": True},
            {"label": "host-b", "managed": True, "present": True},
            {"label": "local-plugin", "managed": False, "present": True},
        ]
        facts = [
            {"label": "host-a", "ownership_valid": True, "payload_matches_source": True},
            {"label": "host-b", "ownership_valid": True, "payload_matches_source": True},
        ]
        report = {
            "drifted": [{"label": "local-plugin", "managed": False}],
            "unknown": [], "duplicates": [], "scan_truncated": [],
        }
        classified = MODULE._managed_install_classification(records, report, facts)
        self.assertTrue(classified["managed_converged"], classified)
        self.assertEqual(classified["managed_drift"], [])

        report["drifted"] = [{"label": "host-b", "managed": True}]
        classified = MODULE._managed_install_classification(records, report, facts)
        self.assertFalse(classified["managed_converged"], classified)
        self.assertEqual(classified["managed_drift"], ["host-b"])

    def test_managed_install_gate_blocks_missing_managed_host(self):
        records = [
            {"label": "host-a", "managed": True, "present": True},
            {"label": "host-b", "managed": True, "present": False},
        ]
        facts = [
            {"label": "host-a", "ownership_valid": True, "payload_matches_source": True},
        ]
        report = {"drifted": [], "unknown": [], "duplicates": [], "scan_truncated": []}
        classified = MODULE._managed_install_classification(records, report, facts)
        self.assertFalse(classified["managed_converged"], classified)
        self.assertEqual(classified["managed_missing"], ["host-b"])

    def test_cli_expected_version_is_machine_checked(self):
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = MODULE.main(["--root", str(ROOT), "--expected-version", "2.2.0"])
        self.assertEqual(result, 2)
        self.assertIn("--expected-version", errors.getvalue())

    def test_cli_rejects_in_tree_artifact_paths(self):
        output = ROOT / "tools" / ".release-manifest-test-output.json"
        evidence = ROOT / "tools" / ".release-evidence-test-input.json"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                MODULE.main(["--root", str(ROOT), "--output", str(output)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("manifest output must be outside", errors.getvalue())

        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                MODULE.main(["--root", str(ROOT), "--evidence-file", str(evidence)])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("evidence input must be outside", errors.getvalue())


if __name__ == "__main__":
    unittest.main()

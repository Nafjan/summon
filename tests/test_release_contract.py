from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools"))
import release_contract as contract  # noqa: E402


class ReleaseContractTests(unittest.TestCase):
    def test_current_companions_are_version_converged(self):
        facts = contract.version_facts(ROOT)
        self.assertTrue(facts["converged"], facts)
        self.assertEqual(facts["canonical"], "3.3.0")
        self.assertEqual(set(facts["versions"]),
                         {"dispatcher", "mcp_server", "plugin", "telemetry"})

    def test_migration_contract_is_present_and_complete(self):
        migration = contract.migration_contract(ROOT)
        self.assertTrue(migration["present"], migration)
        self.assertTrue(migration["complete"], migration)
        self.assertEqual(migration["path"], "docs/PHASE1_MIGRATION_ROLLBACK.md")

    def test_current_release_rejects_a_stale_phase1_version_marker(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "skills/summon/scripts").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "plugin.json").write_text(
                json.dumps({"version": "3.3.0"}), encoding="utf-8")
            for name, assignment in (
                ("run_subagent.py", "__version__"),
                ("_telemetry.py", "SUMMON_VERSION"),
                ("mcp_server.py", "SERVER_VERSION"),
            ):
                (root / "skills/summon/scripts" / name).write_text(
                    f'{assignment} = "3.3.0"\n', encoding="utf-8")
            current = (ROOT / "docs/PHASE1_MIGRATION_ROLLBACK.md").read_text(
                encoding="utf-8")
            (root / "docs/PHASE1_MIGRATION_ROLLBACK.md").write_text(
                current.replace("Current product version: 3.3.0",
                                "Current product version: 3.1.0"),
                encoding="utf-8")
            result = contract.release_contract(root)
            self.assertFalse(result["ready"])
            self.assertIn("unique current product version marker: 3.3.0",
                          result["migration"]["missing"])

    def test_stale_marker_cannot_be_hidden_by_a_correct_example(self):
        with tempfile.TemporaryDirectory() as raw:
            root = self._complete_release_root(Path(raw))
            path = root / "docs/PHASE1_MIGRATION_ROLLBACK.md"
            text = path.read_text(encoding="utf-8").replace(
                "Current product version: 3.3.0",
                "Current product version: 3.1.0\n\nExample: Current product version: 3.3.0",
                1,
            )
            path.write_text(text, encoding="utf-8")
            result = contract.release_contract(root)
            self.assertFalse(result["ready"])
            self.assertIn("unique current product version marker: 3.3.0",
                          result["migration"]["missing"])

    def test_duplicate_version_markers_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = self._complete_release_root(Path(raw))
            path = root / "docs/PHASE1_MIGRATION_ROLLBACK.md"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text + "\nCurrent product version: 3.3.0\n", encoding="utf-8")
            result = contract.release_contract(root)
            self.assertFalse(result["ready"])

    @staticmethod
    def _complete_release_root(root: Path) -> Path:
        (root / "skills/summon/scripts").mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "plugin.json").write_text(
            json.dumps({"version": "3.3.0"}), encoding="utf-8")
        for name, assignment in (
            ("run_subagent.py", "__version__"),
            ("_telemetry.py", "SUMMON_VERSION"),
            ("mcp_server.py", "SERVER_VERSION"),
        ):
            (root / "skills/summon/scripts" / name).write_text(
                f'{assignment} = "3.3.0"\n', encoding="utf-8")
        shutil.copyfile(
            ROOT / "docs/PHASE1_MIGRATION_ROLLBACK.md",
            root / "docs/PHASE1_MIGRATION_ROLLBACK.md")
        return root

    def test_mismatch_is_not_ready(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "skills/summon/scripts").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "plugin.json").write_text(json.dumps({"version": "3.0.0"}), encoding="utf-8")
            (root / "skills/summon/scripts/run_subagent.py").write_text(
                '__version__ = "2.2.0"\n', encoding="utf-8"
            )
            (root / "skills/summon/scripts/mcp_server.py").write_text(
                'SERVER_VERSION = "3.0.0"\n', encoding="utf-8"
            )
            result = contract.release_contract(root)
            self.assertFalse(result["ready"])
            self.assertTrue(any("dispatcher" in item for item in result["version"]["errors"]))

    def test_telemetry_version_is_part_of_the_convergence_contract(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "skills/summon/scripts").mkdir(parents=True)
            (root / "docs").mkdir()
            (root / "plugin.json").write_text(json.dumps({"version": "3.1.0"}), encoding="utf-8")
            (root / "skills/summon/scripts/run_subagent.py").write_text(
                '__version__ = "3.1.0"\n', encoding="utf-8"
            )
            (root / "skills/summon/scripts/_telemetry.py").write_text(
                'SUMMON_VERSION = "3.0.0"\n', encoding="utf-8"
            )
            (root / "skills/summon/scripts/mcp_server.py").write_text(
                'SERVER_VERSION = "3.1.0"\n', encoding="utf-8"
            )
            result = contract.release_contract(root)
            self.assertFalse(result["ready"])
            self.assertTrue(any("telemetry" in item for item in result["version"]["errors"]))


if __name__ == "__main__":
    unittest.main()

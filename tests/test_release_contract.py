from __future__ import annotations

import json
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
        self.assertEqual(facts["canonical"], "3.1.0-preview.1")
        self.assertEqual(set(facts["versions"]), {"dispatcher", "mcp_server", "plugin"})

    def test_migration_contract_is_present_and_complete(self):
        migration = contract.migration_contract(ROOT)
        self.assertTrue(migration["present"], migration)
        self.assertTrue(migration["complete"], migration)

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


if __name__ == "__main__":
    unittest.main()

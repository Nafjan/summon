from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_install import _dest, _fake_home, _run


class MigrationRollbackGateTests(unittest.TestCase):
    def test_isolated_upgrade_keeps_owned_manifest_and_companions(self):
        home = _fake_home()
        try:
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            manifest = json.loads(Path(_dest(home), ".summon-install.json").read_text())
            self.assertEqual(manifest.get("installed_by"), "summon")
            self.assertTrue(Path(home, ".claude", "skills", "council", "SKILL.md").is_file())
            self.assertTrue(Path(home, ".claude", "skills", "deliberate", "SKILL.md").is_file())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_interrupted_swap_restores_previous_owned_tree(self):
        home = _fake_home()
        try:
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            dest = Path(_dest(home))
            previous = Path(str(dest) + ".previous")
            os.replace(dest, previous)
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((dest / "SKILL.md").is_file())
            self.assertFalse(previous.exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_foreign_tree_blocks_upgrade_without_mutation(self):
        home = _fake_home()
        try:
            dest = Path(_dest(home))
            dest.mkdir(parents=True)
            marker = dest / "user-owned.txt"
            marker.write_text("keep", encoding="utf-8")
            result = _run(home, "--hosts", "claude", "--no-agents")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

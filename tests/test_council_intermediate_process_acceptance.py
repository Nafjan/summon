"""Real local council subprocess acceptance; fixtures never launch providers.

Each test runs in a clean, owned process and source snapshot. The synthetic child
uses the real CLI parser and the unmodified prompt-file intake AST from main.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


class CouncilIntermediateProcessAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory(prefix="summon-council-process-")
        cls.addClassCleanup(cls._temporary.cleanup)
        cls.packet = Path(cls._temporary.name)
        repo = Path(__file__).resolve().parents[1]
        scripts = cls.packet / "scripts"
        scripts.mkdir()
        # Only public Python source is copied; no runtime/private state is used.
        for source in (repo / "skills" / "summon" / "scripts").glob("*.py"):
            if not source.name.startswith("test_"):
                shutil.copy2(source, scripts / source.name)
        fixtures = Path(__file__).parent / "fixtures" / "council_intermediate_process"
        for source_name, target_name in (("council_process_worker.py", "test_council_process_acceptance.py"), ("synthetic_dispatcher.py", "synthetic_dispatcher.py"), ("audit_fence.py", "audit_fence.py")):
            shutil.copy2(fixtures / source_name, cls.packet / target_name)
        home = cls.packet / "synthetic-home"
        home.mkdir()
        cls.environment = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
        cls.environment.update({key: str(home) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP")})
        cls.environment.update(PATH="", SUMMON_TELEMETRY="0", PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")

    def run_guarded(self, case):
        completed = subprocess.run(
            [sys.executable, "-B", str(self.packet / "test_council_process_acceptance.py"),
             "CouncilProcessTests." + case, "-v"],
            cwd=self.packet, env=self.environment, capture_output=True, text=True,
            timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (completed.stdout + completed.stderr).replace(str(self.packet), "<owned-fixture>")
        self.assertEqual(completed.returncode, 0, output)
        self.assertIn("Ran 1 test", output)
        self.assertIn("OK", output)

    def test_actual_dispatch_preserves_exact_unicode_newlines_and_boms(self):
        self.run_guarded("test_actual_dispatch_preserves_exact_unicode_newlines_and_boms")

    def test_external_prompt_file_keeps_legacy_normalization(self):
        self.run_guarded("test_external_prompt_file_keeps_legacy_normalization")

    def test_actual_dispatch_at_eight_mib_and_chair_permission(self):
        self.run_guarded("test_actual_dispatch_at_eight_mib_and_chair_permission")

    def test_actual_child_failure_is_bounded_and_prompt_is_cleaned(self):
        self.run_guarded("test_actual_child_failure_is_bounded_and_prompt_is_cleaned")

    def test_actual_cli_over_limit_emits_last_resort_terminal_envelope(self):
        self.run_guarded("test_actual_cli_over_limit_emits_last_resort_terminal_envelope")

    def test_actual_cli_prompt_creation_failure_emits_last_resort_terminal_envelope(self):
        self.run_guarded("test_actual_cli_prompt_creation_failure_emits_last_resort_terminal_envelope")

    def test_admitted_near_sixty_four_kib_context_reaches_actual_round_two_dispatchers(self):
        self.run_guarded("test_admitted_near_sixty_four_kib_context_reaches_actual_round_two_dispatchers")


if __name__ == "__main__":
    unittest.main()





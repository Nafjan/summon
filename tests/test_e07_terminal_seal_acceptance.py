"""Actual authenticated job terminal sealing with a guarded, process-inert worker."""
from pathlib import Path
import os, shutil, subprocess, sys, tempfile, unittest
class TerminalSealAcceptance(unittest.TestCase):
 def run_guarded(self, case):
  with tempfile.TemporaryDirectory(prefix='summon-terminal-seal-') as name:
   packet=Path(name);scripts=packet/'scripts';scripts.mkdir()
   root=Path(__file__).resolve().parents[1]
   for f in (root/'skills'/'summon'/'scripts').glob('*.py'):
    if not f.name.startswith('test_'):shutil.copy2(f,scripts/f.name)
   for f in (Path(__file__).parent/'fixtures'/'e07_terminal_seal').glob('*.py'):shutil.copy2(f,packet/f.name)
   home=packet/'home';home.mkdir()
   env={k:os.environ[k] for k in ('SystemRoot','WINDIR') if k in os.environ}
   env.update({k:str(home) for k in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP')})
   env.update(PATH='',SUMMON_TELEMETRY='0',PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
   done=subprocess.run([sys.executable,'-B',str(packet/'terminal_seal_worker.py'),'TerminalSealTests.'+case,'-v'],cwd=packet,env=env,capture_output=True,text=True,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   output=(done.stdout+done.stderr).replace(str(packet),'<owned-fixture>')
   report=packet/'safe-results.json'
   if report.exists():output+='\nSAFE_RESULTS='+report.read_text(encoding='utf-8')
   self.assertEqual(done.returncode,0,output)
   self.assertIn('Ran 1 test',output)
 def test_no_accounting_emission_seals_actual_claim(self):
  self.run_guarded("test_no_accounting_emission_seals_actual_claim")
 def test_private_accounting_publication_seals_actual_claim(self):
  self.run_guarded("test_private_accounting_publication_seals_actual_claim")
 def test_published_accounting_is_canonical_for_actual_reconciliation(self):
  self.run_guarded("test_published_accounting_is_canonical_for_actual_reconciliation")
 def test_indeterminate_contact_remains_unknown_after_emission(self):
  self.run_guarded("test_indeterminate_contact_remains_unknown_after_emission")
 def test_changed_canonical_identity_is_refused_by_terminal_reader(self):
  self.run_guarded("test_changed_canonical_identity_is_refused_by_terminal_reader")
if __name__=='__main__':unittest.main()



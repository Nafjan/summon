"""Portable exact-boundary adapter acceptance; guarded owned consumers only."""
from pathlib import Path
import os,shutil,subprocess,sys,tempfile,unittest
class OperatorBoundaryAcceptance(unittest.TestCase):
 def run_guarded(self,case):
  with tempfile.TemporaryDirectory(prefix='summon-operator-boundary-') as name:
   packet=Path(name);scripts=packet/'scripts';scripts.mkdir();root=Path(__file__).resolve().parents[1]
   for f in (root/'skills'/'summon'/'scripts').glob('*.py'):
    if not f.name.startswith('test_'):shutil.copy2(f,scripts/f.name)
   for f in (Path(__file__).parent/'fixtures'/'operator_boundary').glob('*.py'):shutil.copy2(f,packet/f.name)
   home=packet/'home';home.mkdir()
   env={k:os.environ[k] for k in ('SystemRoot','WINDIR') if k in os.environ};env.update({k:str(home) for k in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP')});env.update(PATH='',SUMMON_TELEMETRY='0',PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
   done=subprocess.run([sys.executable,'-B',str(packet/'operator_boundary_worker.py'),'OperatorBoundaryTests.'+case,'-v'],cwd=packet,env=env,capture_output=True,text=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   output=(done.stdout+done.stderr).replace(str(packet),'<owned-fixture>');self.assertEqual(done.returncode,0,output);self.assertIn('Ran 1 test',output)
 def test_operator_message_exact_complete_json_4096(self):self.run_guarded("test_operator_message_exact_complete_json_4096")
 def test_operator_message_complete_json_4097_no_mutation(self):self.run_guarded("test_operator_message_complete_json_4097_no_mutation")
 def test_operator_cancel_exact_complete_json_4096(self):self.run_guarded("test_operator_cancel_exact_complete_json_4096")
 def test_operator_cancel_complete_json_4097_no_mutation(self):self.run_guarded("test_operator_cancel_complete_json_4097_no_mutation")
if __name__=='__main__':unittest.main()

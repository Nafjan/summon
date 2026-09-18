"""Portable exact-boundary adapter acceptance; guarded owned consumers only."""
from pathlib import Path
import os,shutil,subprocess,sys,tempfile,unittest
class TransportAdapterExactAcceptance(unittest.TestCase):
 def run_guarded(self,case):
  with tempfile.TemporaryDirectory(prefix='summon-adapter-exact-') as name:
   packet=Path(name);scripts=packet/'scripts';scripts.mkdir();root=Path(__file__).resolve().parents[1]
   for f in (root/'skills'/'summon'/'scripts').glob('*.py'):
    if not f.name.startswith('test_'):shutil.copy2(f,scripts/f.name)
   for f in (Path(__file__).parent/'fixtures'/'transport_adapter_exact').glob('*.py'):shutil.copy2(f,packet/f.name)
   home=packet/'home';home.mkdir()
   env={k:os.environ[k] for k in ('SystemRoot','WINDIR') if k in os.environ};env.update({k:str(home) for k in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP')});env.update(PATH='',SUMMON_TELEMETRY='0',PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
   done=subprocess.run([sys.executable,'-B',str(packet/'adapter_exact_worker.py'),'AdapterExactTests.'+case,'-v'],cwd=packet,env=env,capture_output=True,text=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   output=(done.stdout+done.stderr).replace(str(packet),'<owned-fixture>');self.assertEqual(done.returncode,0,output);self.assertIn('Ran 1 test',output)
 def test_http_exact_eight_mib_request_data(self):self.run_guarded("test_http_exact_eight_mib_request_data")
 def test_http_eight_mib_plus_one_refuses_before_opener(self):self.run_guarded("test_http_eight_mib_plus_one_refuses_before_opener")
 def test_acp_exact_eight_mib_actual_pipe_bytes(self):self.run_guarded("test_acp_exact_eight_mib_actual_pipe_bytes")
 def test_acp_eight_mib_plus_one_writes_nothing(self):self.run_guarded("test_acp_eight_mib_plus_one_writes_nothing")
 def test_acp_resume_refuses_without_child(self):self.run_guarded("test_acp_resume_refuses_without_child")
 def test_gemini_actual_builder_eight_mib_file_and_final_argv(self):self.run_guarded("test_gemini_actual_builder_eight_mib_file_and_final_argv")
 def test_gemini_actual_builder_eight_mib_plus_one_refuses(self):self.run_guarded("test_gemini_actual_builder_eight_mib_plus_one_refuses")
 def test_gemini_resume_remains_refused(self):self.run_guarded("test_gemini_resume_remains_refused")
 def test_gemini_file_route_still_refuses_oversize_final_argv(self):self.run_guarded("test_gemini_file_route_still_refuses_oversize_final_argv")
 def test_ark_fresh_exact_argv_and_actual_environment(self):self.run_guarded("test_ark_fresh_exact_argv_and_actual_environment")
 def test_ark_fresh_limit_plus_one_refuses(self):self.run_guarded("test_ark_fresh_limit_plus_one_refuses")
 def test_ark_resume_exact_argv_and_actual_environment(self):self.run_guarded("test_ark_resume_exact_argv_and_actual_environment")
 def test_ark_resume_limit_plus_one_refuses(self):self.run_guarded("test_ark_resume_limit_plus_one_refuses")
 @unittest.skipUnless(os.name=="nt", "Windows serialized environment ceiling")
 def test_ark_windows_environment_exact_limit(self):self.run_guarded("test_ark_windows_environment_exact_limit")
 @unittest.skipUnless(os.name=="nt", "Windows serialized environment ceiling")
 def test_ark_windows_environment_limit_plus_one(self):self.run_guarded("test_ark_windows_environment_limit_plus_one")
if __name__=='__main__':unittest.main()

"""Portable exact-boundary adapter acceptance; guarded owned consumers only."""
from pathlib import Path
import os,shutil,subprocess,sys,tempfile,unittest
class ProfileResumeAcceptance(unittest.TestCase):
 def run_guarded(self,case):
  with tempfile.TemporaryDirectory(prefix='summon-profile-resume-') as name:
   packet=Path(name);scripts=packet/'scripts';scripts.mkdir();root=Path(__file__).resolve().parents[1]
   for f in (root/'skills'/'summon'/'scripts').glob('*.py'):
    if not f.name.startswith('test_'):shutil.copy2(f,scripts/f.name)
   for f in (Path(__file__).parent/'fixtures'/'r15_profile_resume').glob('*.py'):shutil.copy2(f,packet/f.name)
   home=packet/'home';home.mkdir()
   env={k:os.environ[k] for k in ('SystemRoot','WINDIR') if k in os.environ};env.update({k:str(home) for k in ('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','TEMP','TMP')});env.update(PATH='',SUMMON_TELEMETRY='0',PYTHONDONTWRITEBYTECODE='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
   done=subprocess.run([sys.executable,'-B',str(packet/'profile_resume_worker.py'),'ProfileResumeTests.'+case,'-v'],cwd=packet,env=env,capture_output=True,text=True,timeout=45,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
   output=(done.stdout+done.stderr).replace(str(packet),'<owned-fixture>');self.assertEqual(done.returncode,0,output);self.assertIn('Ran 1 test',output)
 def test_unchanged_profile_reaches_authenticated_launch_claim(self):self.run_guarded("test_unchanged_profile_reaches_authenticated_launch_claim")
 def test_rotated_profile_refuses_before_authenticated_launch_claim(self):self.run_guarded("test_rotated_profile_refuses_before_authenticated_launch_claim")
 def test_profile_rotation_after_loaded_validation_refuses_final_launch_claim(self):self.run_guarded("test_profile_rotation_after_loaded_validation_refuses_final_launch_claim")
 def test_profile_rotation_before_child_load_refuses_without_baselining(self):self.run_guarded("test_profile_rotation_before_child_load_refuses_without_baselining")
 def test_legacy_profile_binding_is_readable_but_resume_refuses(self):self.run_guarded("test_legacy_profile_binding_is_readable_but_resume_refuses")
 def test_profile_binding_resolve_failure_refuses_without_claim_mutation(self):self.run_guarded("test_profile_binding_resolve_failure_refuses_without_claim_mutation")
 def test_v2_profile_source_is_claim_bound_and_public_projection_redacts_it(self):self.run_guarded("test_v2_profile_source_is_claim_bound_and_public_projection_redacts_it")
if __name__=='__main__':unittest.main()

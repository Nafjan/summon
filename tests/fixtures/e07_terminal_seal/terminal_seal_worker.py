"""Guarded, executor-free authenticated terminal publication acceptance."""
from pathlib import Path
import contextlib, copy, hashlib, io, json, os, socket, subprocess, sys, tempfile, unittest
from unittest import mock
PACKET=Path(__file__).resolve().parent
from audit_fence import install
AUDIT_HITS, _ = install(PACKET, child=True)
sys.path.insert(0,str(PACKET/'scripts'))
HITS=[]
def forbidden(*args,**kwargs):
 HITS.append('forbidden');raise AssertionError('forbidden synthetic boundary')
GUARDS=contextlib.ExitStack()
GUARDS.enter_context(mock.patch.object(subprocess,'Popen',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(os,'system',side_effect=forbidden))
import _windows_credentials, _nous_credentials
for mod,names in ((_windows_credentials,('read_credential','_read_hermes_env_key','resolve_openrouter_api_key')),(_nous_credentials,('_candidate_paths','_parse_key','resolve_nous_api_key'))):
 for name in names:GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import _auth, _executor, _background, run_subagent as dispatcher
for mod,name in ((_auth,'run_auth_action'),(_executor,'execute_agent'),(_background,'spawn_background'),(dispatcher,'_dispatch_with_retries'),(dispatcher,'execute_agent')):
 GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import claim_fixtures as fixture
import _jobs, _job_resume as resume, _submission_accounting as accounting
RESULTS={}

class TerminalSealTests(unittest.TestCase):
 def setUp(self):
  self.assertEqual(AUDIT_HITS,[]);self.assertEqual(HITS,[])
  self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET)
  self.addCleanup(self.temp.cleanup)
  self.root,self.source,self.reservation,self.prepared=fixture._prepare(Path(self.temp.name))
  self.job_file=_jobs.result_path(self.root,self.reservation.successor_job_id)
  self.record=_jobs.read_json(_jobs.record_path(self.root,self.reservation.successor_job_id))
  resume.claim_transition(self.root,self.source,self.reservation.claim_id,field='parent_phase',expected='successor_prepared',target='child_launch_claimed')
  self.lineage=resume.authenticated_lineage(self.job_file)
  self.context=resume.load_child_context(self.job_file,self.prepared['claim_file'])
 def tearDown(self):
  self.assertEqual(AUDIT_HITS,[]);self.assertEqual(HITS,[])
  self.assertFalse(Path(self.job_file+'.terminal.lock').exists())
 def obj(self,private=False):
  value={'status':'blocked','execution_status':'not_run','attempt_status':'not_run','attempts':0,'provider_contacted':False,'exit_code':1,'summon':self.record['summon']}
  if private:
   estimate=accounting.estimate_payload([('user','owned synthetic request')],boundary='synthetic_prelaunch')
   value['submission_accounting']=accounting.private_record(attempt_id=self.reservation.successor_job_id,attempt_kind='resume',attempt_ordinal=1,parent_attempt_id=None,request_sha256='a'*64,estimate=estimate,envelope=value)
  return value
 def emit(self,value):
  error=None
  with mock.patch.object(dispatcher,'_JOB_FILE',self.job_file),mock.patch.object(dispatcher,'_GOVERNED_RESUME_LINEAGE',self.lineage),mock.patch.dict(os.environ,{'SUMMON_JOB_ID':self.reservation.successor_job_id,'SUMMON_JOB_NONCE':self.record['nonce']}):
   try:dispatcher._emit(value,operation='resume')
   except resume.ResumeError as exc:error=exc.kind
  stored=_jobs.read_json(self.job_file)
  self.assertIsInstance(stored,dict);self.assertEqual(stored['lineage'],self.lineage)
  return error,stored
 def canonical(self,stored,claim):
  self.assertEqual(claim['terminal_sha256'],hashlib.sha256(json.dumps(stored,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest())
  self.assertEqual(resume.reconcile_terminal(self.reservation)['terminal_sha256'],claim['terminal_sha256'])
 def test_no_accounting_emission_seals_actual_claim(self):
  error,stored=self.emit(self.obj());self.assertIsNone(error)
  claim=resume.get_claim(self.reservation);self.assertEqual(claim['parent_phase'],'terminal');self.assertIs(claim['provider_contacted'],False)
  self.canonical(stored,claim)
  RESULTS['no_accounting']='sealed_canonical_digest'
 def test_private_accounting_publication_seals_actual_claim(self):
  value=self.obj(True);private=copy.deepcopy(value['submission_accounting'])
  error,stored=self.emit(value)
  self.assertEqual(value['submission_accounting'],private)
  self.assertNotIn('submission_accounting',stored)
  self.assertEqual(stored['submission_summary'],accounting.public_summary(private))
  self.assertNotIn(private['identity']['material_sha256'],json.dumps(stored))
  claim=resume.get_claim(self.reservation)
  RESULTS['private_accounting']={'emit_error_kind':error,'parent_phase':claim['parent_phase'],'terminal_digest_present':claim.get('terminal_sha256') is not None,'failure_marker':claim.get('terminalization_error_kind'),'public_summary_preserved':True,'private_record_preserved':True}
  self.assertIsNone(error,'E07-SEAL-01: projected publication must seal the canonical public object')
  self.assertEqual(claim['parent_phase'],'terminal');self.canonical(stored,claim)
 def test_published_accounting_is_canonical_for_actual_reconciliation(self):
  value=self.obj(True);error,stored=self.emit(value);before=Path(self.job_file).read_bytes()
  claim=resume.reconcile_terminal(self.reservation)
  self.assertEqual(claim['parent_phase'],'terminal');self.canonical(stored,claim)
  self.assertEqual(Path(self.job_file).read_bytes(),before)
  RESULTS['canonical_reconciliation']={'initial_emit_error_kind':error,'terminal_after_actual_reader':True,'published_bytes_unchanged':True}
 def test_indeterminate_contact_remains_unknown_after_emission(self):
  control=resume.provider_launch_control(self.context)
  control.before_provider_launch(fixture._launch_evidence())
  control.launch_indeterminate(RuntimeError('synthetic unknown launch'))
  error,stored=self.emit(self.obj());self.assertIsNone(error)
  claim=resume.get_claim(self.reservation)
  self.assertEqual(claim['parent_phase'],'terminal');self.assertEqual(claim['provider_phase'],'indeterminate');self.assertIsNone(claim['provider_contacted'])
  self.assertTrue(resume.public_projection(claim)['recovery_required']);self.canonical(stored,claim)
  RESULTS['uncertainty']='terminal_with_unknown_contact_and_recovery_required'
 def test_changed_canonical_identity_is_refused_by_terminal_reader(self):
  error,stored=self.emit(self.obj());self.assertIsNone(error)
  stored['job_nonce']='synthetic-conflicting-nonce';_jobs._atomic_write_json(self.job_file,stored)
  with self.assertRaises(resume.ResumeError) as caught:resume.reconcile_terminal(self.reservation)
  self.assertEqual(caught.exception.kind,'resume_terminal_untrusted')
  RESULTS['changed_identity']='refused'

if __name__=='__main__':
 try:unittest.main()
 finally:
  (PACKET/'safe-results.json').write_text(json.dumps(RESULTS,indent=2),encoding='utf-8')
  GUARDS.close()

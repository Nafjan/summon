"""Read selected synthetic profile only; no provider process is permitted."""
from pathlib import Path
from types import SimpleNamespace
from dataclasses import replace
import contextlib,hashlib,json,os,subprocess,sys,tempfile,unittest
from unittest import mock
PACKET=Path(__file__).resolve().parent
from audit_fence import install
AUDIT_HITS,_=install(PACKET,child=True)
sys.path.insert(0,str(PACKET/'scripts'))
HITS=[]
def forbidden(*args,**kwargs):
 HITS.append('forbidden');raise AssertionError('forbidden synthetic boundary')
GUARDS=contextlib.ExitStack()
GUARDS.enter_context(mock.patch.object(subprocess,'Popen',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(os,'system',side_effect=forbidden))
SELECTED=None
PROFILE_NAMES={'.credentials.json','.claude.json','settings.json','auth.json','config.toml'}
def profile_fence(event,args):
 if event=='open' and args and isinstance(args[0],(str,bytes,os.PathLike)):
  path=Path(os.fsdecode(args[0])).resolve()
  if path.name in PROFILE_NAMES and (SELECTED is None or path.parent!=SELECTED):forbidden()
sys.addaudithook(profile_fence)
import _windows_credentials,_nous_credentials
for mod,names in ((_windows_credentials,('read_credential','_read_hermes_env_key','resolve_openrouter_api_key')),(_nous_credentials,('_candidate_paths','_parse_key','resolve_nous_api_key'))):
 for name in names:GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import _executor,_profiles,_jobs,_job_resume as resume,_launch_binding
import claim_fixtures as fixture
def loaded(context):
    source = context.source
    invocation = _executor.AgentInvocation(
        cli="claude", prompt=context.prompt, cwd=source["workspace"]["path"],
        agent_file=source["agent"]["file"], permission=context.effective_permission,
        transport="subprocess", model=source["model"]["targeted"],
        model_exact_required=True, model_exact_source="named-seat",
        effort=source["authority"]["effort"],
        read_roots=tuple(source["authority"]["read_roots"]),
        isolated_lane=source["authority"]["isolated_lane"],
        allow_tool_credentials=source["authority"]["allow_tool_credentials"],
        profile=source["backend"]["profile"], resume_id=context.resume_handle,
    )
    args = SimpleNamespace(
        agent=source["agent"]["requested"],
        _resolved_agent=source["agent"]["resolved"], _role_provenance={},
        agents_dir=source["agent"]["agents_dir"],
        strict_agents_dir=source["authority"]["strict_agents_dir"],
        enable_roles=source["authority"]["enable_roles"],
        allow_credit=context.allow_credit, allow_payg=context.allow_payg,
        allow_text_only=source["authority"]["allow_text_only"],
        require_tools=source["authority"]["require_tools"],
        gate_with=context.gate_with, gate_timeout=context.gate_timeout_ms,
        timeout=context.timeout_ms, max_runtime=context.max_runtime_ms,
        retries=0, no_contract_repair=True, no_acp_fallback=True,
    )
    receipt = {
        "agent_def": {"sha256": source["agent"]["definition_sha256"]},
        "profile": {
            "name": source["backend"]["profile"],
            "path_sha256": source["backend"]["profile_path_sha256"],
            "registry_sha256": source["backend"]["profile_registry_sha256"],
            "command_sha256": source["backend"]["profile_command_sha256"],
        },
    }
    return invocation,args,receipt
class ProfileResumeTests(unittest.TestCase):
 def exercise(self,rotated):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name);SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root)
   job_file=_jobs.result_path(jobs,reservation.successor_job_id)
   resume.claim_transition(jobs,job_id,reservation.claim_id,field='parent_phase',expected='successor_prepared',target='child_launch_claimed')
   context=resume.load_child_context(job_file,prepared['claim_file'])
   before=_profiles.resolve_profile('review-profile','claude',str(root/'workspace'))
   identity_args=dict(agent='reviewer',prompt='private',cwd=str(root/'workspace'),agents_dir=str(root/'agents'),cli='claude',model='claude-opus-5',profile='review-profile')
   bound_identity=_executor.build_request_identity(**identity_args)
   self.assertEqual(context.source['request_sha256'],_executor.request_fingerprint(**bound_identity))
   if rotated:(SELECTED/'.credentials.json').write_text('{"fixture":"rotated"}',encoding='utf-8')
   current=_profiles.resolve_profile('review-profile','claude',str(root/'workspace'))
   self.assertEqual(before['state_sha256']!=current['state_sha256'],rotated)
   now_identity=_executor.build_request_identity(**identity_args)
   self.assertEqual(_executor.request_fingerprint(**bound_identity)!=_executor.request_fingerprint(**now_identity),rotated)
   for key in ('name','path_sha256','registry_sha256','command_sha256'):self.assertEqual(before[key],current[key])
   inv,args,receipt=loaded(context)
   inv=replace(inv,profile_env=current['env'],profile_auth_mode=current['auth_mode'])
   # The authenticated v2 source binds the original selected profile state.
   receipt['profile']={key:current.get(key) for key in ('name','cli','path_sha256','registry_sha256','command_sha256','auth_mode')}
   self.assertEqual(context.source['backend']['profile_state_sha256'],
                    before['state_sha256'])
   observed=_launch_binding.observation(str(root/'claude.exe'),[],inv.cwd,current['env'],backend='claude',external_cli_version='claude-test/1.0')
   evidence={key:observed[key] for key in ('backend','transport','command_sha256','argv_sha256','cwd_sha256','env_names_sha256','env_sha256')}
   evidence.update(schema='summon.fleet-launch-evidence/v1',launch_observation=observed)
   kind=None
   try:
    resume.validate_loaded_invocation(context,inv,args,receipt)
    resume.provider_launch_control(context).before_provider_launch(evidence)
   except resume.ResumeError as exc:kind=exc.kind
   claim=resume.get_claim(reservation)
   print(json.dumps({'rotated':rotated,'validation_refusal':kind,'provider_phase':claim['provider_phase'],'provider_contacted':claim['provider_contacted'],'guard_hits':len(HITS)+len(AUDIT_HITS)},sort_keys=True))
   self.assertEqual(HITS,[]);self.assertEqual(AUDIT_HITS,[])
   self.assertEqual(inv.profile,'review-profile');self.assertEqual(inv.profile_env,current['env'])
   if rotated:
    self.assertIsNotNone(kind,'rotated profile passed actual validation and durable launch admission')
    self.assertEqual(claim['provider_phase'],'pending')
   else:
    self.assertIsNone(kind);self.assertEqual(claim['provider_phase'],'launch_claimed')
 def test_unchanged_profile_reaches_authenticated_launch_claim(self):self.exercise(False)
 def test_rotated_profile_refuses_before_authenticated_launch_claim(self):self.exercise(True)

 def test_profile_rotation_after_loaded_validation_refuses_final_launch_claim(self):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name); SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root)
   job_file=_jobs.result_path(jobs,reservation.successor_job_id)
   resume.claim_transition(jobs,job_id,reservation.claim_id,field='parent_phase',expected='successor_prepared',target='child_launch_claimed')
   context=resume.load_child_context(job_file,prepared['claim_file'])
   selected=_profiles.resolve_profile('review-profile','claude',str(root/'workspace'))
   inv,args,receipt=loaded(context)
   inv=replace(inv,profile_env=selected['env'],profile_auth_mode=selected['auth_mode'])
   receipt['profile']={key:selected.get(key) for key in ('name','cli','path_sha256','registry_sha256','command_sha256','auth_mode')}
   # Establish the actual successful loaded-child boundary before changing any profile bytes.
   resume.validate_loaded_invocation(context,inv,args,receipt)
   control=resume.provider_launch_control(context)
   claim_before=resume.get_claim(reservation)
   ledger=Path(resume.ledger_path(jobs,job_id))
   ledger_before=ledger.read_bytes()
   registry_before=(root/'profiles.json').read_bytes()
   self.assertEqual(claim_before['provider_phase'],'pending')
   self.assertIsNone(claim_before['provider_contacted'])
   (SELECTED/'.credentials.json').write_text('{"fixture":"rotated-after-loaded-validation"}',encoding='utf-8')
   current=_profiles.resolve_profile('review-profile','claude',str(root/'workspace'))
   self.assertNotEqual(selected['state_sha256'],current['state_sha256'])
   for key in ('name','path_sha256','registry_sha256','command_sha256','env','auth_mode'):
    self.assertEqual(selected[key],current[key])
   self.assertEqual((root/'profiles.json').read_bytes(),registry_before)
   # Real measurement of the same owned inert executable; no provider is run.
   observed=_launch_binding.observation(str(root/'claude.exe'),[],inv.cwd,current['env'],backend='claude',external_cli_version='claude-test/1.0')
   evidence={key:observed[key] for key in ('backend','transport','command_sha256','argv_sha256','cwd_sha256','env_names_sha256','env_sha256')}
   evidence.update(schema='summon.fleet-launch-evidence/v1',launch_observation=observed)
   kind=None
   try:
    control.before_provider_launch(evidence)
   except resume.ResumeError as exc:
    kind=exc.kind
   except _executor.ProviderLaunchRefusal as exc:
    kind=exc.error_kind
   claim_after=resume.get_claim(reservation)
   print(json.dumps({'case':'after_loaded_validation','typed_refusal':kind,
    'provider_phase':claim_after['provider_phase'],
    'provider_contacted':claim_after['provider_contacted'],
    'claim_unchanged':claim_after==claim_before,'ledger_unchanged':ledger.read_bytes()==ledger_before,
    'guard_hits':len(HITS)+len(AUDIT_HITS)},sort_keys=True))
   self.assertEqual(HITS,[]);self.assertEqual(AUDIT_HITS,[])
   self.assertEqual(inv.profile,'review-profile');self.assertEqual(inv.profile_env,selected['env'])
   self.assertEqual(set(json.loads((root/'profiles.json').read_text())['profiles']),{'review-profile'})
   self.assertEqual((root/'profiles.json').read_bytes(),registry_before)
   self.assertIsNone(claim_after['provider_contacted'],'no synthetic provider contact occurred')
   self.assertIsNotNone(kind,'profile rotation after loaded validation passed actual final launch admission')
   self.assertEqual(claim_after,claim_before,'refusal mutated authenticated claim or prior uncertainty')
   self.assertEqual(ledger.read_bytes(),ledger_before,'refusal rewrote durable claim ledger')

 def test_profile_rotation_before_child_load_refuses_without_baselining(self):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name); SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root)
   job_file=_jobs.result_path(jobs,reservation.successor_job_id)
   ledger=Path(resume.ledger_path(jobs,job_id)); ledger_before=ledger.read_bytes()
   source_path=Path(fixture.continuation.continuation_path(jobs,job_id)); source_before=source_path.read_bytes()
   (SELECTED/'.credentials.json').write_text('{"fixture":"rotated-before-load"}',encoding='utf-8')
   with self.assertRaises(resume.ResumeError) as caught:
    resume.load_child_context(job_file,prepared['claim_file'])
   self.assertEqual(caught.exception.kind,'resume_profile_drift')
   self.assertEqual(ledger.read_bytes(),ledger_before)
   self.assertEqual(source_path.read_bytes(),source_before)
   claim=resume.get_claim(reservation)
   self.assertEqual(claim['provider_phase'],'pending'); self.assertIsNone(claim['provider_contacted'])

 def test_legacy_profile_binding_is_readable_but_resume_refuses(self):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name); SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root,legacy_profile_binding=True)
   job_file=_jobs.result_path(jobs,reservation.successor_job_id)
   source=resume.read_private_source(jobs,job_id)
   self.assertEqual(source['schema'],'summon.job-continuation-source/v1')
   with self.assertRaises(resume.ResumeError) as caught:
    resume.load_child_context(job_file,prepared['claim_file'])
   self.assertEqual(caught.exception.kind,'resume_profile_unverified')
   claim=resume.get_claim(reservation)
   self.assertEqual(claim['provider_phase'],'pending'); self.assertIsNone(claim['provider_contacted'])

 def test_profile_binding_resolve_failure_refuses_without_claim_mutation(self):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name); SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root)
   job_file=_jobs.result_path(jobs,reservation.successor_job_id)
   ledger=Path(resume.ledger_path(jobs,job_id)); ledger_before=ledger.read_bytes()
   source_path=Path(fixture.continuation.continuation_path(jobs,job_id))
   source_before=source_path.read_bytes()
   moved=root/'profile-moved'; SELECTED.rename(moved); SELECTED.write_text('not a profile',encoding='utf-8')
   with self.assertRaises(resume.ResumeError) as caught:
    resume.load_child_context(job_file,prepared['claim_file'])
   self.assertIn(caught.exception.kind,('resume_profile_drift','resume_profile_unverified'))
   self.assertEqual(ledger.read_bytes(),ledger_before)
   self.assertEqual(source_path.read_bytes(),source_before)
   claim=resume.get_claim(reservation)
   self.assertEqual(claim['provider_phase'],'pending'); self.assertIsNone(claim['provider_contacted'])

 def test_v2_profile_source_is_claim_bound_and_public_projection_redacts_it(self):
  global SELECTED
  with tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET) as name:
   root=Path(name); SELECTED=root/'profile'
   jobs,job_id,reservation,prepared=fixture._prepare(root)
   source=resume.read_private_source(jobs,job_id)
   claim=resume.get_claim(reservation)
   self.assertEqual(source['schema'],'summon.job-continuation-source/v2')
   claim_file=fixture.continuation._read_strict(prepared['claim_file'])
   self.assertEqual(claim_file['source_sha256'],resume._digest(source))
   public=fixture.continuation.public_projection(source)
   public_text=json.dumps(public,sort_keys=True)
   self.assertNotIn('profile_path',public_text)
   self.assertNotIn('profile_state_sha256',public_text)

if __name__=='__main__':unittest.main()

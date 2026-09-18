"""Actual local dispatcher process tests; no real backend is launched.
Internal exact intake and legacy external intake have separate positive controls.
"""
from pathlib import Path
import argparse,contextlib,hashlib,io,json,os,socket,subprocess,sys,tempfile,threading,unittest
from unittest import mock
PACKET=Path(__file__).resolve().parent
from audit_fence import install
AUDIT_HITS, AUDIT_LAUNCH = install(PACKET)
SCRIPTS=PACKET/'scripts'
sys.path.insert(0,str(SCRIPTS))
HITS=[];CHILDREN=[]
def forbidden(*args,**kwargs):
 HITS.append('forbidden');raise AssertionError('forbidden synthetic boundary')
GUARDS=contextlib.ExitStack()
REAL_POPEN=subprocess.Popen
GUARDS.enter_context(mock.patch.object(os,'system',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(socket.socket,'connect',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(socket.socket,'connect_ex',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(socket.socket,'sendto',side_effect=forbidden))
def owned_popen(cmd,*args,**kwargs):
 if (not isinstance(cmd,list) or len(cmd)<2 or Path(cmd[0]).resolve()!=Path(sys.executable).resolve()
     or Path(cmd[1]).resolve()!=SCRIPTS/'run_subagent.py' or '--prompt-file' not in cmd or '--prompt' in cmd):return forbidden()
 prompt=Path(cmd[cmd.index('--prompt-file')+1]).resolve()
 if PACKET not in prompt.parents:return forbidden()
 copied=list(cmd);copied[1]=str(PACKET/'synthetic_dispatcher.py')
 empty=PACKET/'child-stdin.txt'
 empty.write_bytes(b'')
 with empty.open('rb') as stdin:
  if kwargs.get('stdin') is subprocess.DEVNULL:kwargs['stdin']=stdin
  AUDIT_LAUNCH['command']=copied
  try:proc=REAL_POPEN(copied,*args,**kwargs)
  finally:AUDIT_LAUNCH['command']=None
 CHILDREN.append(proc);return proc
GUARDS.enter_context(mock.patch.object(subprocess,'Popen',side_effect=owned_popen))

import _windows_credentials,_nous_credentials
for mod,names in ((_windows_credentials,('read_credential','_read_hermes_env_key','resolve_openrouter_api_key')),(_nous_credentials,('_candidate_paths','_parse_key','resolve_nous_api_key'))):
 for name in names:GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import _auth,_executor,_background,run_subagent
for mod,name in ((_auth,'run_auth_action'),(_executor,'execute_agent'),(_background,'spawn_background'),(run_subagent,'_dispatch_with_retries'),(run_subagent,'execute_agent')):
 GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import _council as council,_council_between_round as between,_rundir as rd



class CouncilProcessTests(unittest.TestCase):
 def setUp(self):
  self.assertEqual(AUDIT_HITS,[])
  HITS.clear();CHILDREN.clear()
  self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET);self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name)
  for who in ('m1','m2','chair'):(self.root/(who+'.md')).write_text('---\nrun-agent: claude\npermission: safe-edit\n---\n# fixture\n')
 def tearDown(self):
  self.assertEqual(HITS,[])
  self.assertEqual(AUDIT_HITS,[])
  self.assertTrue(all(p.poll() is not None for p in CHILDREN))
  self.assertEqual(list(self.root.rglob('.summon-council-prompt-*.txt')),[])
  for run in self.root.iterdir():
   if run.is_dir() and (run/'generation.txt').exists():self.assertIsNone(rd.read_owner(str(run)))
 def args(self,question='X or Y?',pause=False):
  return argparse.Namespace(question=question,question_file=None,members='m1,m2',chairman='chair',rounds=2,cwd=str(self.root),agents_dir=str(self.root),timeout=10000,out=None,run_dir=str(self.root),results_dir=None,resume_run=None,council=True,pause_after_round=1 if pause else None,overall_timeout=100000,member_timeout=None,chair_timeout=None,quorum=None,min_successful=None,chairman_fallback=None,strict_agents_dir=False,enable_roles=False,allow_kimi_acp_fallback=False,council_continue=False,between_round_context=None,council_original_deadline_unix_ms=None)
 def call(self,fn,args):
  out=io.StringIO()
  with contextlib.redirect_stdout(out),contextlib.redirect_stderr(io.StringIO()):code=fn(args)
  return code,json.loads(out.getvalue())
 def direct(self,text,tag='g1-r2-m1'):
  events=[]
  result=council._dispatch('chair' if 'chairman' in tag else 'm1',text,str(self.root),str(self.root),10000,str(self.root),tag,on_spawn=lambda p:events.append('spawn'),on_reap=lambda p:events.append('reap'))
  self.assertEqual(events,['spawn','reap'])
  return result,json.loads((self.root/(tag+'.observation.json')).read_text())
 def test_actual_dispatch_preserves_exact_unicode_newlines_and_boms(self):
  text='\ufeff'+('\u03bb \U0001f642 "quoted" \\ slash\r\nnext \ufeff internal\n'*3000)
  raw=text.encode();self.assertGreater(len(raw),65536)
  result,seen=self.direct(text)
  self.assertEqual(result['status'],'success')
  self.assertEqual(seen['bytes'],len(raw));self.assertEqual(seen['sha256'],hashlib.sha256(raw).hexdigest())
  decoded=text
  self.assertEqual(seen['decoded_sha256'],hashlib.sha256(decoded.encode()).hexdigest())
  self.assertTrue(seen['file_exists_during_child']);self.assertTrue(seen['require_tools'])
 def test_external_prompt_file_keeps_legacy_normalization(self):
  import _manifest
  text='\ufefflegacy\r\ninternal \ufeff BOM\rstandalone\n'
  prompt=self.root/'external.txt';prompt.write_bytes(text.encode())
  output=self.root/'legacy.json'
  cmd=[sys.executable,str(SCRIPTS/'run_subagent.py'),'--agent','m1','--prompt-file',str(prompt),'--cwd',str(self.root),'--out',str(output),'--timeout','10000']
  result,error=_manifest._dispatch_child(cmd,10)
  self.assertIsNone(error);self.assertEqual(result.returncode,0)
  seen=json.loads(output.with_suffix('.observation.json').read_text())
  expected=text.removeprefix('\ufeff').replace('\r\n','\n').replace('\r','\n').encode()
  self.assertEqual(seen['sha256'],hashlib.sha256(text.encode()).hexdigest())
  self.assertEqual(seen['decoded_sha256'],hashlib.sha256(expected).hexdigest())
  self.assertNotEqual(seen['decoded_sha256'],seen['sha256'])
 def test_actual_dispatch_at_eight_mib_and_chair_permission(self):
  raw=b'x'*(8*1024*1024)
  result,seen=self.direct(raw.decode(),'g1-chairman')
  self.assertEqual(result['status'],'success');self.assertEqual(seen['bytes'],len(raw));self.assertEqual(seen['sha256'],hashlib.sha256(raw).hexdigest());self.assertTrue(seen['chair_clamped'])
 def test_actual_child_failure_is_bounded_and_prompt_is_cleaned(self):
  with mock.patch.dict(os.environ,{'SYNTHETIC_DISPATCH_FAIL':'1'}):result,seen=self.direct('synthetic child failure')
  self.assertEqual(result['status'],'error');self.assertIn('exit 7',result['error']);self.assertTrue(seen['file_exists_during_child'])
 def _actual_cli_failure(self,creation_failure):
  import runpy
  question=self.root/'question.txt'
  text='X or Y?' if creation_failure else 'x'*(8*1024*1024+1-len(council._round1_prompt('').encode()))
  if not creation_failure:self.assertEqual(len(council._round1_prompt(text).encode()),8*1024*1024+1)
  question.write_text(text)
  argv=[str(SCRIPTS/'run_subagent.py'),'--council','--question-file',str(question),'--members','m1,m2','--chairman','chair','--rounds','2','--cwd',str(self.root),'--agents-dir',str(self.root),'--run-dir',str(self.root),'--timeout','10000','--overall-timeout','100000','--json']
  original=tempfile.mkstemp
  def fail(*args,**kwargs):
   if creation_failure and kwargs.get('prefix')=='.summon-council-prompt-':raise OSError('synthetic creation failure')
   return original(*args,**kwargs)
  output=io.StringIO()
  with mock.patch.object(sys,'argv',argv),mock.patch.object(tempfile,'mkstemp',side_effect=fail),mock.patch.object(run_subagent._telemetry,'record',return_value=None),contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
   with self.assertRaises(SystemExit) as stopped:runpy.run_path(str(SCRIPTS/'run_subagent.py'),run_name='__main__')
  self.assertEqual(stopped.exception.code,1)
  raw=output.getvalue();self.assertLess(len(raw.encode()),8192)
  result=json.loads(raw);self.assertEqual(result['status'],'error');self.assertEqual(CHILDREN,[])
  self.assertNotIn(str(self.root),raw)
  safe={k:result.get(k) for k in ('status','error_kind','attempts','attempt_status','provider_contacted','execution_status','dispatcher_status')}
  (PACKET/('cli-creation-result.json' if creation_failure else 'cli-oversize-result.json')).write_text(json.dumps(safe))
 def test_actual_cli_over_limit_emits_last_resort_terminal_envelope(self):
  self._actual_cli_failure(False)
 def test_actual_cli_prompt_creation_failure_emits_last_resort_terminal_envelope(self):
  self._actual_cli_failure(True)
 def test_admitted_near_sixty_four_kib_context_reaches_actual_round_two_dispatchers(self):
  code,paused=self.call(council.run_council,self.args(pause=True));self.assertEqual(code,0)
  checkpoint=between.read_checkpoint(paused['run_dir'],paused['run_id'])
  atom='\u03bb \U0001f642 "quote" \\ backslash\r\nline\n'
  entry='\ufeff'+(atom*110)[:3500]
  packet={'schema':between.CONTEXT_SCHEMA,'run_id':paused['run_id'],'checkpoint_sha256':checkpoint['checkpoint_sha256'],'checkpoint_generation':checkpoint['source_generation'],'operation_key':'a'*32,'entries':[]}
  while True:
   candidate=packet|{'entries':packet['entries']+[{'kind':'note','author':'human','text':entry}]}
   if len(json.dumps(candidate,ensure_ascii=False).encode())>64000:break
   packet=candidate
  raw=json.dumps(packet,ensure_ascii=False).encode();self.assertGreater(len(raw),58000);self.assertLess(len(raw),65536)
  context=self.root/'context.json';context.write_bytes(raw)
  code,submitted=self.call(council.run_council_context_submit,argparse.Namespace(council_context_submit=paused['run_id'],council_context_file=str(context),council_operation_key='a'*32,council_expect_generation=checkpoint['source_generation'],run_dir=str(self.root),cwd=str(self.root),json=True,out=None));self.assertEqual(code,0)
  expected={};original=council._dispatch
  def observe(agent,prompt,cwd,agents_dir,timeout_ms,out_dir,tag,**kwargs):
   expected[tag]=prompt.encode();return original(agent,prompt,cwd,agents_dir,timeout_ms,out_dir,tag,**kwargs)
  with mock.patch.object(council,'_dispatch',side_effect=observe):
   code,result=self.call(council.run_council_continue,argparse.Namespace(council_continue=paused['run_id'],council_expect_generation=checkpoint['source_generation'],run_dir=str(self.root),cwd=str(self.root),json=True,out=None))
  self.assertEqual(code,0);self.assertEqual(result['status'],'success');self.assertEqual(len(CHILDREN),5)
  r2=[(tag,raw) for tag,raw in expected.items() if '-r2-' in tag];self.assertEqual(len(r2),2)
  for tag,payload in r2:
   seen=json.loads((Path(paused['run_dir'])/(tag+'.observation.json')).read_text())
   self.assertEqual(seen['bytes'],len(payload));self.assertEqual(seen['sha256'],hashlib.sha256(payload).hexdigest())
   self.assertEqual(seen['decoded_sha256'],hashlib.sha256(payload).hexdigest(), 'exact internal intake changed immutable admitted context')
   text=payload.decode()
   for item in packet['entries']:self.assertIn(item['text'],text)

if __name__=='__main__':
 try:unittest.main()
 finally:GUARDS.close()



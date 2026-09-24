"""Final-byte transport acceptance using guarded owned synthetic consumers."""
from pathlib import Path
import contextlib,hashlib,io,json,os,subprocess,sys,tempfile,threading,unittest
from types import SimpleNamespace
from unittest import mock
PACKET=Path(__file__).resolve().parent
from audit_fence import install
AUDIT_HITS,AUDIT_LAUNCH=install(PACKET)
sys.path.insert(0,str(PACKET/'scripts'))
HITS=[];CHILDREN=[]
def forbidden(*args,**kwargs):
 HITS.append('forbidden');raise AssertionError('forbidden synthetic boundary')
GUARDS=contextlib.ExitStack();REAL_POPEN=subprocess.Popen
RECEIVER=PACKET/'synthetic_receiver.py'
def owned_popen(cmd,*args,**kwargs):
 if (not isinstance(cmd,list) or len(cmd)<4 or Path(cmd[0]).resolve()!=Path(sys.executable).resolve() or Path(cmd[1]).resolve()!=RECEIVER or cmd[2] not in {'acp','gemini','ark'} or PACKET not in Path(cmd[3]).resolve().parents):return forbidden()
 with contextlib.ExitStack() as files:
  if kwargs.get('stdin') is subprocess.DEVNULL:
   empty=PACKET/'empty-stdin';empty.write_bytes(b'');kwargs['stdin']=files.enter_context(empty.open('rb'))
  AUDIT_LAUNCH['command']=cmd
  try:proc=REAL_POPEN(cmd,*args,**kwargs)
  finally:AUDIT_LAUNCH['command']=None
 AUDIT_LAUNCH['pids'].add(proc.pid)
 CHILDREN.append(proc);return proc
GUARDS.enter_context(mock.patch.object(subprocess,'Popen',side_effect=owned_popen))
GUARDS.enter_context(mock.patch.object(os,'system',side_effect=forbidden))
# Optional Windows Job Object support is unavailable in this byte-only fixture.
# Keep native FFI denied; direct owned Popen handles still provide cleanup.
def optional_job_unavailable(process,*args,**kwargs):
 if process not in CHILDREN:return forbidden()
 return False
GUARDS.enter_context(mock.patch.dict(sys.modules,{'_jobobj':SimpleNamespace(attach=optional_job_unavailable,close=optional_job_unavailable,terminate=optional_job_unavailable)}))
import _windows_credentials,_nous_credentials
for mod,names in ((_windows_credentials,('read_credential','_read_hermes_env_key','resolve_openrouter_api_key')),(_nous_credentials,('_candidate_paths','_parse_key','resolve_nous_api_key'))):
 for name in names:GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
import _apibackend as api,_acpbackend as acp,_arkcli_backend as ark,_executor as executor,_builder as builder,_transport_budget as budget
import _receipt
# Unrelated repository-status probes are explicitly unavailable in owned fixtures.
GUARDS.enter_context(mock.patch.object(_receipt,'_run_git_bounded',return_value=(None,None,'synthetic_snapshot_unavailable')))
# API route preparation is not part of Request.data tests; any accidental resolver call refuses.
for name in ('resolve_api_credential',):
 if hasattr(api,name):GUARDS.enter_context(mock.patch.object(api,name,side_effect=forbidden))
CAP=8*1024*1024
SEED='\ufeff\u03bb \U0001f642 "quote" \\ slash\r\nnext \ufeff\n'

class AdapterExactTests(unittest.TestCase):
 def setUp(self):
  self.assertEqual(AUDIT_HITS,[]);self.assertEqual(HITS,[])
  self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET);self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
 def tearDown(self):
  for proc in CHILDREN:
   if proc.poll() is None:
    proc.kill();proc.wait(timeout=5)
  self.assertTrue(all(p.poll() is not None for p in CHILDREN))
  self.assertEqual(AUDIT_HITS,[]);self.assertEqual(HITS,[])
 def http(self,target):
  def body(prompt):return json.dumps({'model':'fixture-model','messages':[{'role':'system','content':SEED},{'role':'user','content':prompt}],'stream':False}).encode('utf-8')
  prompt=SEED+'x'*(target-len(body(SEED)))
  expected=body(prompt);self.assertEqual(len(expected),target)
  seen=[];facts=[];real=budget.evaluate_serialized_payload
  def observe(**kwargs):
   result=real(**kwargs);facts.append((kwargs['payload'],result));return result
  class Opener:
   def open(inner,request,timeout):
    seen.append(request.data)
    return io.BytesIO(json.dumps({'choices':[{'message':{'content':'synthetic complete'}}]}).encode())
  with mock.patch.object(api,'_opener',return_value=Opener()),mock.patch.object(budget,'evaluate_serialized_payload',side_effect=observe):
   result=api._do_request('https://example.invalid/v1','fixture-model',SEED,prompt,'synthetic-key',5000,'openai-compat')
  self.assertEqual(facts[0][0],expected)
  self.assertEqual(facts[0][1]['content_sha256'],hashlib.sha256(expected).hexdigest())
  if target==CAP:
   self.assertEqual(seen,[expected]);self.assertEqual(result['status'],'success')
   decoded=json.loads(seen[0]);self.assertEqual(decoded['messages'][0]['content'],SEED);self.assertEqual(decoded['messages'][1]['content'],prompt)
  else:
   self.assertEqual(seen,[]);self.assertEqual(result['error_kind'],'transport_budget_exceeded');self.assertIs(result['provider_contacted'],False)
 def test_http_exact_eight_mib_request_data(self):self.http(CAP)
 def test_http_eight_mib_plus_one_refuses_before_opener(self):self.http(CAP+1)
 def acp_send(self,target):
  def message(text):return {'jsonrpc':'2.0','id':3,'method':'session/prompt','params':{'sessionId':'owned-session','prompt':[{'type':'text','text':text}]}}
  def line(text):return (json.dumps(message(text),ensure_ascii=False)+os.linesep).encode('utf-8')
  text=SEED+'x'*(target-len(line(SEED)));expected=line(text);self.assertEqual(len(expected),target)
  output=self.root/'received.json'
  proc=subprocess.Popen([sys.executable,str(RECEIVER),'acp',str(output)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
  client=acp._AcpClient(proc,'read-only',max_frame_bytes=CAP)
  error=None
  try:client._send(message(text))
  except acp._AcpPayloadTooLarge as exc:error=exc
  finally:
   proc.stdin.close();proc.wait(timeout=15)
   client._reader.join(timeout=2);client._err_reader.join(timeout=2)
  self.assertEqual(proc.returncode,0)
  seen=json.loads(output.read_text(encoding='utf-8'))
  if target==CAP:
   self.assertIsNone(error);self.assertEqual(seen['bytes'],CAP,'actual ACP pipe bytes exceed declared ceiling');self.assertEqual(seen['sha256'],hashlib.sha256(expected).hexdigest())
  else:
   self.assertIsNotNone(error);self.assertEqual(seen['bytes'],0)
 def test_acp_exact_eight_mib_actual_pipe_bytes(self):self.acp_send(CAP)
 def test_acp_eight_mib_plus_one_writes_nothing(self):self.acp_send(CAP+1)
 def test_acp_resume_refuses_without_child(self):
  before=len(CHILDREN)
  inv=builder.AgentInvocation(cli='gemini',prompt='synthetic',cwd=str(self.root),resume_id='synthetic-resume',permission='read-only',transport='acp')
  result=acp.call(inv,5000)
  self.assertEqual(len(CHILDREN),before);self.assertEqual(result['status'],'error');self.assertIn('resume',result['error'])

 def gemini(self,size):
  system=self.root/'GEMINI_SYSTEM.md'
  head=SEED.encode('utf-8');raw=head+b'x'*(size-len(head));system.write_bytes(raw)
  output=self.root/'gemini.json'
  inv=builder.AgentInvocation(cli='gemini',prompt=SEED,cwd=str(self.root),permission='yolo',agent_file=str(system),transport='subprocess')
  command,expected_args,environment=builder.build_invocation_args(inv,5000)
  self.assertEqual(command,'gemini');self.assertEqual(environment['GEMINI_SYSTEM_MD'],str(system))
  self.assertIn(SEED,expected_args);self.assertNotIn(raw.decode('utf-8'),expected_args)
  before=len(CHILDREN)
  def resolve(command,args):
   self.assertEqual(command,'gemini');return sys.executable,[str(RECEIVER),'gemini',str(output),*args]
  with mock.patch.object(executor,'_resolve_launch',side_effect=resolve):result=executor.execute_agent(inv,timeout_ms=5000)
  if size==CAP:
   self.assertEqual(len(CHILDREN),before+1);self.assertEqual(result['status'],'success')
   seen=json.loads(output.read_text(encoding='utf-8'));self.assertEqual(seen['bytes'],CAP)
   self.assertEqual(seen['sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(seen['decoded_sha256'],hashlib.sha256(raw).hexdigest());self.assertEqual(seen['argv'],expected_args)
   self.assertEqual(result['transport_budget']['measurement']['attachment_bytes'],CAP)
  else:
   self.assertEqual(len(CHILDREN),before);self.assertFalse(output.exists());self.assertEqual(result['error_kind'],'transport_budget_exceeded')
 def test_gemini_actual_builder_eight_mib_file_and_final_argv(self):self.gemini(CAP)
 def test_gemini_actual_builder_eight_mib_plus_one_refuses(self):self.gemini(CAP+1)
 def test_gemini_resume_remains_refused(self):
  inv=builder.AgentInvocation(cli='gemini',prompt=SEED,cwd=str(self.root),resume_id='synthetic-resume',permission='read-only',transport='subprocess')
  with self.assertRaisesRegex(ValueError,'resume is not supported'):builder.build_invocation_args(inv,5000)
 def test_gemini_file_route_still_refuses_oversize_final_argv(self):
  system=self.root/'GEMINI_SYSTEM.md';system.write_bytes(b'synthetic system')
  inv=builder.AgentInvocation(cli='gemini',prompt='x'*150000,cwd=str(self.root),permission='yolo',agent_file=str(system),transport='subprocess')
  output=self.root/'gemini-argv.json';before=len(CHILDREN)
  with mock.patch.object(executor,'_resolve_launch',side_effect=lambda command,args:(sys.executable,[str(RECEIVER),'gemini',str(output),*args])):
   result=executor.execute_agent(inv,timeout_ms=5000)
  self.assertEqual(len(CHILDREN),before);self.assertEqual(result['status'],'error');self.assertIs(result['provider_contacted'],False);self.assertNotIn('fallback',result)
 def ark_boundary(self,resumed,over):
  output=self.root/'ark.json';prefix=[sys.executable,str(RECEIVER),'ark',str(output)]
  fixed=[*prefix,'+chat','--no-progress','--model','fixture-model','--instructions',SEED]
  previous='synthetic-previous-response' if resumed else None
  if previous:fixed+=['--previous-response-id',previous]
  fixed+=['--']
  if os.name=='nt':
   measure=lambda text:len(subprocess.list2cmdline([*fixed,text]).encode('utf-16-le'))//2+1
   prompt=SEED+'x'*(32767+over-measure(SEED));self.assertEqual(measure(prompt),32767+over)
  else:
   prompt=SEED+'x'*(131071+over-len(SEED.encode('utf-8')))
  inv=SimpleNamespace(model='fixture-model',prompt=prompt,system_context=SEED,resume_id=previous)
  captured=[];real=budget.evaluate_invocation
  def observe(**kwargs):captured.append(kwargs);return real(**kwargs)
  before=len(CHILDREN)
  with mock.patch.dict(os.environ,{'R12_SYNTHETIC_MARKER':SEED}),mock.patch.object(ark.shutil,'which',return_value=sys.executable),mock.patch.object(ark,'_arkcli_cmd',return_value=prefix),mock.patch.object(budget,'evaluate_invocation',side_effect=observe):
   result=ark.call(inv,5000)
  measured=captured[0];self.assertEqual([measured['command'],*measured['args']],[*fixed,prompt]);self.assertEqual(measured['env']['R12_SYNTHETIC_MARKER'],SEED)
  if over:
   self.assertEqual(len(CHILDREN),before);self.assertFalse(output.exists());self.assertEqual(result['error_kind'],'transport_budget_exceeded')
  else:
   self.assertEqual(result['status'],'success');self.assertEqual(len(CHILDREN),before+1)
   seen=json.loads(output.read_text(encoding='utf-8'));self.assertEqual(seen['argv'],[*fixed[len(prefix):],prompt]);self.assertEqual(seen['env'],measured['env'])
 def test_ark_fresh_exact_argv_and_actual_environment(self):self.ark_boundary(False,0)
 def test_ark_fresh_limit_plus_one_refuses(self):self.ark_boundary(False,1)
 def test_ark_resume_exact_argv_and_actual_environment(self):self.ark_boundary(True,0)
 def test_ark_resume_limit_plus_one_refuses(self):self.ark_boundary(True,1)

 def ark_environment(self,over):
  self.assertEqual(os.name,'nt')
  from _spawn import scrub_provider_env
  output=self.root/'ark-env.json';prefix=[sys.executable,str(RECEIVER),'ark',str(output)]
  def units(env):return len(('\0'.join(f'{k}={v}' for k,v in env.items())+'\0\0').encode('utf-16-le'))//2
  # Only owned synthetic environment enters this calculation or the child.
  with mock.patch.dict(os.environ,{'R12_SYNTHETIC_MARKER':SEED,'R12_PADDING':''}):
   empty=scrub_provider_env(dict(os.environ));padding='x'*(32767+over-units(empty))
  captured=[];real=budget.evaluate_invocation
  def observe(**kwargs):
   result=real(**kwargs);captured.append((kwargs,result));return result
  before=len(CHILDREN)
  with mock.patch.dict(os.environ,{'R12_SYNTHETIC_MARKER':SEED,'R12_PADDING':padding}),mock.patch.object(ark.shutil,'which',return_value=sys.executable),mock.patch.object(ark,'_arkcli_cmd',return_value=prefix),mock.patch.object(budget,'evaluate_invocation',side_effect=observe):
   result=ark.call(SimpleNamespace(model='fixture-model',prompt=SEED,system_context=SEED,resume_id=None),5000)
  invocation,fact=captured[0];self.assertEqual(units(invocation['env']),32767+over);self.assertEqual(fact['measurement']['serialized_env_utf16_units'],32767+over)
  if over:
   self.assertEqual(result['error_kind'],'transport_budget_exceeded');self.assertEqual(len(CHILDREN),before);self.assertFalse(output.exists())
  else:
   self.assertEqual(result['status'],'success');self.assertEqual(len(CHILDREN),before+1)
   seen=json.loads(output.read_text(encoding='utf-8'));self.assertEqual(seen['env'],invocation['env']);self.assertEqual(units(seen['env']),32767)
 def test_ark_windows_environment_exact_limit(self):self.ark_environment(0)
 def test_ark_windows_environment_limit_plus_one(self):self.ark_environment(1)

if __name__=='__main__':
 try:unittest.main()
 finally:GUARDS.close()

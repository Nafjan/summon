"""Authenticated real handler/adapters/journal with in-memory HTTP streams."""
from pathlib import Path
import contextlib,io,json,os,subprocess,sys,tempfile,unittest
from types import SimpleNamespace
from email.message import Message
from unittest import mock
PACKET=Path(__file__).resolve().parent
from audit_fence import install
AUDIT_HITS,_=install(PACKET,child=True)
sys.path.insert(0,str(PACKET/'scripts'))
HITS=[]
def forbidden(*args,**kwargs):
 HITS.append('forbidden');raise AssertionError('forbidden boundary')
GUARDS=contextlib.ExitStack()
GUARDS.enter_context(mock.patch.object(subprocess,'Popen',side_effect=forbidden))
GUARDS.enter_context(mock.patch.object(os,'system',side_effect=forbidden))
import _windows_credentials,_nous_credentials
for mod,names in ((_windows_credentials,('read_credential','_read_hermes_env_key','resolve_openrouter_api_key')),(_nous_credentials,('_candidate_paths','_parse_key','resolve_nous_api_key'))):
 for name in names:GUARDS.enter_context(mock.patch.object(mod,name,side_effect=forbidden))
# OS ACL qualification is outside this serialized-body fixture. Keep FFI
# forbidden and constrain this environment-only substitution to owned paths.
import _fleet_approval
def owned_private(path,*,directory):
 value=Path(path).resolve()
 if PACKET not in value.parents or (PACKET/'scripts') in value.parents:return forbidden()
 if directory:assert value.is_dir()
 else:assert value.is_file()
GUARDS.enter_context(mock.patch.object(_fleet_approval,'_verify_private',side_effect=owned_private))
# Owned descriptor-to-path substitute for Windows handle-path FFI only.
# Actual manifest byte/identity/reparse checks remain in the consumer.
import _context_target
OPEN_PATHS={};REAL_OPEN=os.open;REAL_CLOSE=os.close
def owned_open(path,*args,**kwargs):
 fd=REAL_OPEN(path,*args,**kwargs);OPEN_PATHS[fd]=str(Path(path).resolve());return fd
def owned_close(fd):
 try:return REAL_CLOSE(fd)
 finally:OPEN_PATHS.pop(fd,None)
def owned_final_path(fd):
 value=OPEN_PATHS.get(fd)
 if value is None or PACKET not in Path(value).parents:return forbidden()
 return value
GUARDS.enter_context(mock.patch.object(os,'open',side_effect=owned_open))
GUARDS.enter_context(mock.patch.object(os,'close',side_effect=owned_close))
GUARDS.enter_context(mock.patch.object(_context_target,'_final_open_path',side_effect=owned_final_path))
import _workspace_ui as ui
from _workspace_commands import OperatorMessageAdapter,OperatorCommandAdapter
from _workspace_content import ContentRef
from operator_harness import OperatorRuntimeHarness

class OperatorBoundaryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='fixture-',dir=PACKET);self.addCleanup(self.temp.cleanup)
  self.h=OperatorRuntimeHarness(Path(self.temp.name));self.addCleanup(self.h.demo.cleanup)
  self.surface=ui.WorkspaceSurface(self.h.demo.runtime._coordinator,self.h.demo.workspace_id,operator_messages=OperatorMessageAdapter(self.h.demo.runtime,self.h.handle),presentation_key=b'm'*32)
  # Host-only start prerequisites, with no socket/server thread. Authentication,
  # bootstrap, scope adapters, runtime authority and journal remain real.
  self.surface._server=SimpleNamespace(server_port=43129)
  self.surface._key=self.surface._configured_key
  self.surface.issue_bootstrap()
  status,result=self.post('/api/bootstrap',json.dumps({'code':self.surface.bootstrap_code}).encode(),None)
  self.assertEqual(status,200);self.token=result['bearer']
  self.assertEqual(result['scope'],'workspace_operator')
 def tearDown(self):
  self.assertEqual(AUDIT_HITS,[]);self.assertEqual(HITS,[])
  self.assertEqual(self.h.demo.total_workers,0)
 def post(self,path,raw,token):
  handler=ui._Handler.__new__(ui._Handler)
  handler.server=SimpleNamespace(surface=self.surface,server_port=43129)
  handler.connection=SimpleNamespace(settimeout=lambda _:None)
  handler.command='POST';handler.path=path;handler.request_version='HTTP/1.1';handler.requestline='POST '+path+' HTTP/1.1'
  handler.headers=Message()
  for k,v in {'Host':'127.0.0.1:43129','Origin':'http://127.0.0.1:43129','Content-Type':'application/json','Content-Length':str(len(raw))}.items():handler.headers[k]=v
  if token is not None:handler.headers['Authorization']='Bearer '+token
  handler.rfile=io.BytesIO(raw);handler.wfile=io.BytesIO();handler._body_bytes_read=0
  handler.do_POST()
  header,body=handler.wfile.getvalue().split(b'\r\n\r\n',1)
  return int(header.split(b' ')[1]),json.loads(body)
 def files(self):
  root=Path(self.h.demo.root)
  return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
 def message(self,n):
  target=self.surface.snapshot()['operator_messages']['targets'][0]['id']
  # JSON.stringify-style non-ASCII UTF-8, with significant escape overhead.
  seed='\ufeff\u03bb\U0001f642"\r\n'
  self.assertEqual([ord(char) for char in seed],[0xfeff,0x03bb,0x1f642,0x22,0x0d,0x0a])
  self.assertTrue(seed.endswith('\r\n'))
  text=seed+'\\'*1950
  body={'operation_key':'a'*32,'target':target,'text':text}
  encode=lambda:json.dumps(body,ensure_ascii=False,separators=(',',':')).encode('utf-8')
  body['text']+='x'*(n-len(encode()))
  raw=encode();self.assertEqual(len(raw),n)
  self.assertLessEqual(len(body['text'].encode('utf-8')),2048)
  self.assertNotEqual(len(raw),len(body['text'].encode('utf-8')))
  self.h.permit(body|{'target':'operator-target'},False)
  before=self.files();state=self.h.demo.state()
  status,result=self.post('/api/messages',raw,self.token)
  if n==4097:
   self.assertEqual((status,result),(400,{'error':'body_bound_exceeded'}));self.assertEqual(self.files(),before);self.assertEqual(self.h.demo.state(),state)
  else:
   self.assertEqual(status,200,result)
   after=self.h.demo.state();self.assertEqual(len(after['workspace']['messages']),len(state['workspace']['messages'])+1)
   message=next(v for k,v in after['workspace']['messages'].items() if k not in state['workspace']['messages'])
   c=message['content'];stored=self.h.demo.runtime._content_store().read(ContentRef(c['ref'],c['sha256'],c['utf8_bytes']))
   self.assertEqual(stored,body['text'].encode('utf-8'))
   for key in ('workers','claims','tasks'):self.assertEqual(after[key],state[key])
 def cancel(self,n):
  grant=self.h.demo.grant('main-1','worker-a-1',suffix='cancel-grant')[0]
  response=self.h.demo.message('main-1','worker-a-1',grant,1,'[2,3]')
  scope={'workspace_id':self.h.demo.workspace_id,'run_id':self.h.demo.run_id,'targets':[{'delivery_id':response['delivery_id'],'actions':['cancel_queued_context']}]}
  self.h.demo.permit({'operation':'install_operator_commands','scope':scope})
  handle=self.h.demo.runtime.install_operator_commands(scope,authorize_command=lambda _s,_w:True)
  self.surface.operator_commands=OperatorCommandAdapter(self.h.demo.runtime,handle)
  view=self.surface.snapshot();target=next(iter(view['operator_commands']['actions_by_delivery']))
  body={'operation_key':'c'*32,'action':'cancel_queued_context','target':target}
  raw=json.dumps(body,separators=(',',':')).encode();raw+=b' '*(n-len(raw))
  self.assertEqual(len(raw),n);before=self.files();state=self.h.demo.state()
  status,result=self.post('/api/commands',raw,self.token)
  if n==4097:
   self.assertEqual((status,result),(400,{'error':'body_bound_exceeded'}));self.assertEqual(self.files(),before);self.assertEqual(self.h.demo.state(),state)
  else:
   self.assertEqual(status,200,result)
   self.assertEqual(self.h.demo.state()['workspace']['deliveries'][response['delivery_id']]['state'],'cancelled')
 def test_operator_message_exact_complete_json_4096(self):self.message(4096)
 def test_operator_message_complete_json_4097_no_mutation(self):self.message(4097)
 def test_operator_cancel_exact_complete_json_4096(self):self.cancel(4096)
 def test_operator_cancel_complete_json_4097_no_mutation(self):self.cancel(4097)
if __name__=='__main__':unittest.main()

"""Actual page/WebCrypto numeric record and AAD version acceptance.

The DOM, storage, bearer and HTTP responses are synthetic. No server or browser
is started; these checks do not certify rendering or full view-shape validation.
"""
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from _workspace_page import page_bytes
from _spawn import popen_flags
from test_workspace_page import _script_harness

_GUARDS = r'''
const nativeCrypto=require('node:crypto').webcrypto;
const nativeWait=require('node:timers').setTimeout;
let boundaryHits=0;
const denied=()=>{boundaryHits++;throw new Error('forbidden synthetic boundary')};
const child=require('node:child_process');for(const key of ['spawn','spawnSync','exec','execSync','execFile','execFileSync','fork'])child[key]=denied;
const net=require('node:net');net.connect=denied;net.createConnection=denied;net.Socket.prototype.connect=denied;
for(const name of ['node:http','node:https']){const module=require(name);module.request=denied;module.get=denied}
const fs=require('node:fs');for(const key of ['readFile','readFileSync','open','openSync','writeFile','writeFileSync','readdir','readdirSync'])fs[key]=denied;
for(const key of ['readFile','open','writeFile','readdir'])fs.promises[key]=denied;
globalThis.crypto=nativeCrypto;
globalThis.btoa=value=>Buffer.from(value,'binary').toString('base64');
globalThis.atob=value=>Buffer.from(value,'base64').toString('binary');
'''

_CHECKS = r'''
(async()=>{
 const workspace='workspace_'+('w'.repeat(32)),target='operator_target_'+('t'.repeat(32));
 const body={operation_key:'e'.repeat(32),target,text:'Synthetic retained private context'};
 const base={schema:'summon.workspace.view/v1',workspace_id:workspace,snapshot:'confirmed',selected_task_id:null,
  workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic objective',criteria:[]},
  closure:{state:'open',unsettled_delivery_count:1,closed:false,ready:false},tasks:[],deliveries:[],
  assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',
  operator_commands:{available:false,actions_by_delivery:{},reason:'scope_required'},
  operator_messages:{available:true,targets:[{id:target,label:'Task 1',available:true}],reason:null},
  operator_drafts:{available:true,operator_scope:'a'.repeat(64),key_epoch:'epoch-1',max_text_bytes:2048,max_record_bytes:4096,reason:null}};
 const requests=[];let keyUnavailable=false;
 globalThis.fetch=async(path,options={})=>{
  requests.push({path,body:options.body?JSON.parse(options.body):null});
  if(path==='/api/messages/draft-key'){
   if(keyUnavailable)return {status:401,ok:false,json:async()=>({})};
   return {status:200,ok:true,json:async()=>({key_b64:Buffer.alloc(32,7).toString('base64url'),operator_scope:base.operator_drafts.operator_scope,key_epoch:'epoch-1',max_text_bytes:2048,max_record_bytes:4096})};
  }
  if(path==='/api/bootstrap')return {status:200,ok:true,json:async()=>({bearer:'synthetic-renewed'})};
  if(path.startsWith('/api/view'))return {status:200,ok:true,json:async()=>base};
  throw new Error('unexpected synthetic endpoint');
 };
 bearer='synthetic-only';render(base);el('message-target').value=target;
 // Establish an actual current producer control, then alter only owned fixture bytes.
 await sealMessageRecord(body,state);
 let record=JSON.parse(stored.get(PENDING_MESSAGE_SLOT));
 assert.equal(record.v,1);assert.equal(validMessageRecord(record),true);
 if(boundary==='record'){
  if(version==='missing')delete record.v;
  else record.v=({future:2,string:'1',boolean:true,null:null,object:{version:1},fraction:1.5})[version];
 }
 if(boundary==='aad'){
  // AES-GCM is genuine: the independently supplied AAD decrypts this fixture.
  // Production messageAad is not replaced or patched.
  const aadObject={version:1,workspace,operator_scope:base.operator_drafts.operator_scope,target,
   operation_key:body.operation_key,state,key_epoch:'epoch-1'};
  if(version==='missing')delete aadObject.version;
  else if(version!=='supported')aadObject.version=({future:2,string:'1',null:null})[version];
  const aad=new TextEncoder().encode(JSON.stringify(aadObject));
  const key=await crypto.subtle.importKey('raw',Buffer.alloc(32,7),{name:'AES-GCM'},false,['encrypt','decrypt']);
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const cipher=await crypto.subtle.encrypt({name:'AES-GCM',iv,additionalData:aad,tagLength:128},key,new TextEncoder().encode(body.text));
  const verified=await crypto.subtle.decrypt({name:'AES-GCM',iv,additionalData:aad,tagLength:128},key,cipher);
  assert.equal(new TextDecoder().decode(verified),body.text);
  record.iv=b64url(iv);record.ciphertext=b64url(new Uint8Array(cipher));
 }
 const sealed=JSON.stringify(record);stored.set(PENDING_MESSAGE_SLOT,sealed);
 assert.equal(sealed.includes(body.text),false);
 pendingMessage=null;retainedMessageRecord=null;messageStorageProblem=null;messageRestored=false;
 el('message-text').value='';requests.length=0;
 if(boundary==='reauth')keyUnavailable=true;
 restorePendingMessageRecord();await restoreEncryptedMessage();renderMessageComposer();
 const supported=boundary==='supported'||boundary==='aad'&&version==='supported';
 if(supported){
  assert.equal(messageStorageProblem,null);assert.equal(retainedMessageRecord,null);
  assert.deepEqual(pendingMessage.body,body);assert.equal(pendingMessage.workspace,workspace);
  assert.equal(pendingMessage.status,state==='unsent'?'draft':'uncertain');
  assert.equal(el('message-text').value,body.text);
  assert.equal(el('message-text').disabled,state!=='unsent');
  assert.equal(el('message-send').disabled,state!=='unsent');
 }else{
  assert.equal(pendingMessage,null);
  if(boundary==='reauth'){assert.equal(el('app').hidden,true,'expired workspace is hidden')}else{
   assert.equal(el('message-text').disabled,true,'unreadable text is blocked');
   assert.equal(el('message-target').disabled,true,'unreadable target is blocked');assert.equal(el('message-send').disabled,true,'unreadable submission is blocked');
  }
  assert.equal(el('message-recovery').hidden,false);
  assert.match(el('message-recovery').textContent,/unresolved/);
  if(boundary==='record')assert.equal(retainedMessageRecord,null);
  else assert.equal(retainedMessageRecord.operation_key,body.operation_key);
  // A queued persistence callback must not replace unreadable ciphertext.
  el('message-text').value='Unsent replacement';await persistUnsentMessage();
  assert.equal(pendingMessage,null);
 }
 assert.equal(stored.get(PENDING_MESSAGE_SLOT),sealed);
 assert.equal(JSON.parse(stored.get(PENDING_MESSAGE_SLOT)).operation_key,body.operation_key);
 assert.equal(JSON.parse(stored.get(PENDING_MESSAGE_SLOT)).target,body.target);
 const keyCalls=requests.filter(item=>item.path==='/api/messages/draft-key');
 assert.equal(keyCalls.length,boundary==='record'?0:1);
 for(const request of keyCalls)assert.deepEqual(request.body,{target,operation_key:body.operation_key,state});
 assert.equal(requests.some(item=>item.path==='/api/messages'||item.path==='/api/messages/lookup'),false);
 if(boundary==='reauth'){
  assert.equal(messageRestoreRetryable,true);keyUnavailable=false;
  await el('login').handlers.submit({preventDefault(){},submitter:el('login-submit')});
  for(let tries=0;messageRestoring&&tries<200;tries++)await new Promise(resolve=>nativeWait(resolve,1));
  assert.equal(messageRestoring,false);assert.equal(messageStorageProblem,null);
  assert.deepEqual(pendingMessage.body,body);assert.equal(pendingMessage.status,'uncertain');
  assert.equal(stored.get(PENDING_MESSAGE_SLOT),sealed);
  assert.equal(requests.filter(item=>item.path==='/api/bootstrap').length,1);
  assert.equal(requests.filter(item=>item.path==='/api/messages/draft-key').length,2);
  assert.equal(requests.some(item=>item.path==='/api/messages'||item.path==='/api/messages/lookup'),false);
 }
 assert.equal(boundaryHits,0);
 console.log(JSON.stringify({passed:true,boundary,state,version}));
})().catch(error=>{console.error(JSON.stringify({failure:error.name,detail:error.message}));process.exitCode=1});
'''


def _run_case(boundary, state='uncertain', version='supported'):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for actual WebCrypto acceptance')
    match = re.search(r'<script nonce="[^"]+">(.*?)</script>', page_bytes(nonce='n'*24).decode(), re.S)
    assert match is not None
    config = 'const boundary='+json.dumps(boundary)+';const state='+json.dumps(state)+';const version='+json.dumps(version)+';\n'
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
    result = subprocess.run([node], input=_script_harness()+_GUARDS+match.group(1)+config+_CHECKS,
                            text=True, encoding='utf-8', capture_output=True, timeout=10,
                            env=environment, **popen_flags())
    # Only synthetic assertions can reach stderr; never print page/private state.
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(passed=True,boundary=boundary,state=state,version=version)


@pytest.mark.parametrize('state', ['unsent', 'sending', 'uncertain'], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
def test_numeric_v1_producer_record_recovers_same_operation_without_submission(state):
    _run_case('supported', state)


@pytest.mark.parametrize('version', ['missing', 'future', 'string', 'boolean', 'null', 'object', 'fraction'], ids=['p002_case_001', 'p002_case_002', 'p002_case_003', 'p002_case_004', 'p002_case_005', 'p002_case_006', 'p002_case_007'])
def test_unsupported_numeric_record_version_retains_ciphertext_and_blocks_replacement(version):
    _run_case('record', version=version)


@pytest.mark.parametrize('version', ['supported', 'missing', 'future', 'string', 'null'], ids=['p003_case_001', 'p003_case_002', 'p003_case_003', 'p003_case_004', 'p003_case_005'])
def test_actual_aes_gcm_aad_version_requires_exact_compatible_bytes(version):
    _run_case('aad', version=version)


def test_explicit_reauthentication_recovers_compatible_ciphertext_with_same_identity():
    _run_case('reauth')

"""Acceptance at the shipped page's refresh/request/render schema boundary.

Uses the existing Node DOM model and real WebCrypto encryption. Fetch responses
are synthetic; no HTTP server/provider is contacted. This is not browser layout
qualification and does not modify the production script to make refusal pass.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
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


_CHECKS = r'''
(async()=>{
 const workspace='workspace_'+('w'.repeat(32));
 const delivery='delivery_'+('d'.repeat(32));
 const target='operator_target_'+('t'.repeat(32));
 const commandBody={operation_key:'c'.repeat(32),action:'cancel_queued_context',target:delivery};
 const messageBody={operation_key:'e'.repeat(32),target,text:'Synthetic pending context'};
 const base={schema:'summon.workspace.view/v1',workspace_id:workspace,snapshot:'confirmed',
  selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},
  goal:{objective:'Confirmed objective',criteria:[]},
  closure:{state:'open',unsettled_delivery_count:1,closed:false,ready:false},
  tasks:[],deliveries:[{id:delivery,label:'Queued',acknowledgement:null,
   certainty:{contact:'unknown',spend:'unknown',cleanup:'unknown'},sequence:1,
   turn_id:null,reason:null,inherited_uncertainty:['contact','spend']}],
  assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:'next-page'},
  controls:[],queue_evaluation:'Passive',
  operator_commands:{available:false,actions_by_delivery:{},reason:'scope_required'},
  operator_messages:{available:true,targets:[{id:target,label:'Task 1',available:true}],reason:null},
  operator_drafts:{available:true,operator_scope:'a'.repeat(64),key_epoch:'epoch-1',
   max_text_bytes:2048,max_record_bytes:4096,reason:null}};
 let incoming={...base,snapshot:'incoming',workspace_revision:3,
  goal:{objective:'SYNTHETIC_RESPONSE_BODY_MARKER',criteria:[]},
  operator_commands:{available:true,actions_by_delivery:{[delivery]:['cancel_queued_context']},reason:null}};
 if(acceptanceSchema==='missing')delete incoming.schema;
 if(acceptanceSchema==='future')incoming.schema='summon.workspace.view/v999';
 if(acceptanceSchema==='invalid_type')incoming.schema={version:1};
 if(acceptanceSchema==='null')incoming=null;
 if(acceptanceSchema==='array')incoming=[];
 let viewCalls=0,keyCalls=0,bootstrapCalls=0,otherCalls=0,errorBodyReads=0;
 let scheduledPoll=null,timerSequence=0;
 if(acceptanceTransient){
  globalThis.setTimeout=(callback,milliseconds)=>{const id=++timerSequence;if(callback===poll)scheduledPoll={callback,milliseconds,id};return id};
  globalThis.clearTimeout=id=>{if(scheduledPoll?.id===id)scheduledPoll=null};
 }
 globalThis.fetch=async(path)=>{
  if(path==='/api/bootstrap'){bootstrapCalls++;return {status:200,ok:true,json:async()=>({bearer:'synthetic-renewed'})}}
  if(path==='/api/messages/draft-key'){
   keyCalls++;
   return {status:200,ok:true,json:async()=>({key_b64:Buffer.alloc(32,7).toString('base64url'),
    operator_scope:base.operator_drafts.operator_scope,key_epoch:'epoch-1',
    max_text_bytes:2048,max_record_bytes:4096})};
  }
  if(path.startsWith('/api/view')){viewCalls++;
   if(acceptanceTransient&&viewCalls===1)return {status:503,ok:false,json:async()=>{
    errorBodyReads++;
    if(acceptanceTransient==='non_json')throw new Error('SYNTHETIC_RESPONSE_BODY_MARKER');
    return {error:'SYNTHETIC_RESPONSE_BODY_MARKER',schema:'summon.workspace.view/v999'};
   }};
   return {status:200,ok:true,json:async()=>{if(acceptanceSchema==='unreadable')throw new Error('SYNTHETIC_RESPONSE_BODY_MARKER');return incoming}}
  }
  otherCalls++;throw new Error('unexpected synthetic endpoint');
 };
 // Establish the prior confirmed v1 presentation and real encrypted recovery bytes.
 bearer='synthetic-only';render(base);
 pendingCommand={workspace,body:commandBody,status:acceptancePhase==='awaiting_confirmation'?'recorded':'uncertain',
  revision:3,error:acceptancePhase==='uncertain'?'Retained command uncertainty':null};
 assert.equal(savePending(),true);
 await sealMessageRecord(messageBody,'uncertain');
 pendingMessage={workspace,body:messageBody,status:acceptancePhase==='awaiting_confirmation'?'queued':'uncertain',
  revision:3,error:acceptancePhase==='uncertain'?'Retained message uncertainty':null};
 renderCommand();renderMessageComposer();
 const beforeCommand=JSON.stringify(pendingCommand),beforeMessage=JSON.stringify(pendingMessage);
 const beforeCommandBytes=stored.get(PENDING_SLOT),beforeCiphertext=stored.get(PENDING_MESSAGE_SLOT);
 assert.equal(validMessageRecord(JSON.parse(beforeCiphertext)),true);
 assert.equal(beforeCiphertext.includes(messageBody.text),false);
 const walk=node=>[node,...node.children.flatMap(walk)];
 const enabledActions=()=>walk(el('delivery-list')).filter(node=>node.dataset.contextAction&&node['aria-disabled']==='false').length;
 assert.equal(enabledActions(),0);
 // Initial loading is the first view after real expiry/bootstrap with retained recovery.
 if(acceptanceRoute==='initial')expire();
 const beforeConfirmed=current,beforeObjective=el('objective').textContent;
 if(acceptanceRoute==='initial')await el('login').handlers.submit({preventDefault(){},submitter:el('login-submit')});
 else if(acceptanceRoute==='poll')await poll();
 else if(acceptanceRoute==='pagination')await el('next-page').handlers.click();
 else await el('refresh').handlers.click();
 // Online/offline hints must not restart automatic polling of an incompatible view.
 if(acceptanceSchema!=='supported'){schedule();windowHandlers.online();windowHandlers.offline();schedule()}
 const textVisible=Array.from(nodes.values()).map(node=>node.textContent).join(' ');
 const result={
  confirmed_view_preserved:current===beforeConfirmed,
  incoming_view_installed:current===incoming,
  confirmed_objective_preserved:el('objective').textContent===beforeObjective,
  command_state_preserved:JSON.stringify(pendingCommand)===beforeCommand,
  message_state_preserved:JSON.stringify(pendingMessage)===beforeMessage,
  command_storage_preserved:stored.get(PENDING_SLOT)===beforeCommandBytes,
  ciphertext_preserved:stored.get(PENDING_MESSAGE_SLOT)===beforeCiphertext,
  pending_operations_cleared:pendingCommand===null&&pendingMessage===null,
  new_action_enabled:enabledActions()>0,
  idle_after_request:loading===false,
  only_expected_local_requests:viewCalls===1&&keyCalls===1&&bootstrapCalls===(acceptanceRoute==='initial'?1:0)&&otherCalls===0,
  pending_view_preserved:pending===null,
  compatible_view_staged:pending!==null&&pending===incoming,
  polling_paused:viewCompatibilityBlocked===true&&pollTimer===null,
  bounded_compatibility_error:el('notice').textContent.includes('view format is unsupported or unreadable')&&!textVisible.includes('SYNTHETIC_RESPONSE_BODY_MARKER'),
  response_content_hidden:!textVisible.includes('SYNTHETIC_RESPONSE_BODY_MARKER'),
 };
 if(acceptanceRecover){
  incoming={...base,schema:'summon.workspace.view/v1',snapshot:'recovered',workspace_revision:3};
  if(acceptanceRoute==='initial')await el('view-retry').handlers.click();else await el('refresh').handlers.click();
  result.explicit_retry_recovers=current===incoming&&viewCompatibilityBlocked===false&&pollTimer!==null&&pendingCommand===null&&pendingMessage===null&&sessionStorage.getItem(PENDING_MESSAGE_SLOT)===null;
 }
 if(acceptanceTransient){
  result.transient_status_not_parsed=errorBodyReads===0;
  result.normal_backoff_active=viewCompatibilityBlocked===false&&delay===4000&&pollTimer!==null&&scheduledPoll!==null&&scheduledPoll.milliseconds>=3200&&scheduledPoll.milliseconds<=4800;
  const retry=scheduledPoll;scheduledPoll=null;
  if(retry)await retry.callback();
  result.automatic_poll_recovers=viewCalls===2&&errorBodyReads===0&&viewCompatibilityBlocked===false&&delay===2000&&scheduledPoll!==null&&scheduledPoll.milliseconds>=1600&&scheduledPoll.milliseconds<=2400&&pending===incoming&&current===beforeConfirmed&&JSON.stringify(pendingCommand)===beforeCommand&&JSON.stringify(pendingMessage)===beforeMessage&&stored.get(PENDING_MESSAGE_SLOT)===beforeCiphertext;
 }
 console.log(JSON.stringify(result));
})().catch(()=>{process.exitCode=2;console.error('Synthetic workspace script setup or execution failed')});
'''


@pytest.mark.parametrize("schema", ["supported", "missing", "future"], ids=['p001_case_001', 'p001_case_002', 'p001_case_003'])
@pytest.mark.parametrize("phase", ["awaiting_confirmation", "uncertain"], ids=['p002_case_001', 'p002_case_002'])
def test_view_schema_is_checked_before_confirmation_or_recovery_mutation(schema, phase):
    _run_view_case(schema, phase)


def _run_view_case(schema, phase, route="refresh", recover=False, transient=None):
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for actual workspace script acceptance")
    match = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                      page_bytes(nonce="n" * 24).decode(), re.S)
    assert match is not None
    crypto = "globalThis.crypto=require('node:crypto').webcrypto;\n"
    crypto += "globalThis.btoa=value=>Buffer.from(value,'binary').toString('base64');\n"
    crypto += "globalThis.atob=value=>Buffer.from(value,'base64').toString('binary');\n"
    configuration = ("const acceptanceSchema=" + json.dumps(schema) + ";\n"
                     "const acceptancePhase=" + json.dumps(phase) + ";\n"
                     "const acceptanceRoute=" + json.dumps(route) + ";\n"
                     "const acceptanceRecover=" + json.dumps(recover) + ";\n"
                     "const acceptanceTransient=" + json.dumps(transient) + ";\n")
    # Node is positively resolved above; no inherited provider/account settings
    # or Node preload options enter this synthetic process.
    environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    result = subprocess.run([executable],
                            input=_script_harness() + crypto + match.group(1) + configuration + _CHECKS,
                            text=True, encoding="utf-8", capture_output=True, timeout=10,
                            env=environment, **popen_flags())
    if result.returncode != 0:
        pytest.fail("Actual page script did not produce a bounded acceptance result", pytrace=False)
    observed = json.loads(result.stdout)
    supported = schema == "supported" and transient is None
    installing = supported and route != "poll"
    clearing = installing and phase == "awaiting_confirmation"
    expected = {
        "confirmed_view_preserved": not installing,
        "incoming_view_installed": installing,
        "confirmed_objective_preserved": not installing,
        "command_state_preserved": not clearing,
        "message_state_preserved": not clearing,
        "command_storage_preserved": not clearing,
        "ciphertext_preserved": not clearing,
        "pending_operations_cleared": clearing,
        "new_action_enabled": clearing,
        "idle_after_request": True,
        "only_expected_local_requests": True,
        "pending_view_preserved": not (supported and route == "poll"),
        "compatible_view_staged": supported and route == "poll",
        "polling_paused": not supported and transient is None,
        "bounded_compatibility_error": not supported and transient is None,
        "response_content_hidden": not installing,
    }
    if recover:
        expected["explicit_retry_recovers"] = True
    if transient:
        expected.update(transient_status_not_parsed=True, normal_backoff_active=True, automatic_poll_recovers=True)
    differences = [name for name, value in expected.items() if observed.get(name) != value]
    if differences:
        pytest.fail("Actual view schema boundary violated: " + ", ".join(differences), pytrace=False)


@pytest.mark.parametrize("route", ["initial", "poll", "pagination"], ids=['p003_case_001', 'p003_case_002', 'p003_case_003'])
@pytest.mark.parametrize("schema", ["supported", "missing", "future"], ids=['p004_case_001', 'p004_case_002', 'p004_case_003'])
def test_each_incoming_view_route_preserves_the_schema_boundary(route, schema):
    _run_view_case(schema, "awaiting_confirmation", route)


@pytest.mark.parametrize("schema", ["invalid_type", "null", "array", "unreadable"], ids=['p005_case_001', 'p005_case_002', 'p005_case_003', 'p005_case_004'])
def test_malformed_view_cannot_expose_response_content_or_change_recovery(schema):
    _run_view_case(schema, "awaiting_confirmation")


@pytest.mark.parametrize("route", ["initial", "refresh"], ids=['p006_case_001', 'p006_case_002'])
def test_explicit_retry_can_recover_after_incompatible_view_without_new_operation(route):
    _run_view_case("future", "awaiting_confirmation", route, recover=True)


@pytest.mark.parametrize("body", ["json", "non_json"], ids=['p007_case_001', 'p007_case_002'])
def test_transient_view_503_keeps_backoff_and_automatically_recovers_without_parsing_body(body):
    _run_view_case("supported", "uncertain", route="poll", transient=body)

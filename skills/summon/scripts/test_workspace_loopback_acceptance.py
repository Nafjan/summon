"""Actual page-script reconnect acceptance; no browser geometry or provider claim."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _spawn import popen_flags
from _workspace_page import page_bytes


@pytest.mark.parametrize("scenario", [
    "offline_hint_initial_snapshot",
    "offline_hint_unreachable_server",
    "hidden_document",
    "expired_authentication",
    "missing_authentication",
    "bounded_backoff_and_announcement_coalescing",
], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004', 'p001_case_005', 'p001_case_006'])
def test_actual_page_loopback_reconnect(scenario):
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the actual page-script fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    harness = r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.dataset={};this.children=[];this.handlers={};this.hidden=false;this.disabled=false;this.scrollTop=0;this.textContent='';this.value='';}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=items}
 setAttribute(key,value){this[key]=value}
 addEventListener(key,handler){this.handlers[key]=handler}
 querySelectorAll(){return this.children}
 getBoundingClientRect(){return {top:0,bottom:30}}
 focus(){document.activeElement=this}
}
const nodes=new Map();
globalThis.document={hidden:false,activeElement:null,getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement(){return new Element()},addEventListener(){}};
globalThis.window={addEventListener(){}};
const stored=new Map();globalThis.sessionStorage={getItem(key){return stored.get(key)??null},setItem(key,value){stored.set(key,value)},removeItem(key){stored.delete(key)}};
Object.defineProperty(globalThis,'navigator',{value:{onLine:true},configurable:true});
const timers=new Map();let nextTimer=0;
globalThis.setTimeout=(callback,ms)=>{const id=++nextTimer;timers.set(id,{callback,ms});return id};
globalThis.clearTimeout=id=>timers.delete(id);
'''
    checks = r'''
(async()=>{
     const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'confirmed',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic acceptance',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive'};
 let requests=[];
 const response=(status,value)=>({status,ok:status===200,json:async()=>value});
 globalThis.fetch=async(path,options)=>{requests.push({path,options});return response(200,data)};
 if(scenario==='offline_hint_initial_snapshot'){
   navigator.onLine=false;
   globalThis.fetch=async(path,options)=>{requests.push({path,options});return response(200,path==='/api/bootstrap'?{bearer:'synthetic-only'}:data)};
   el('code').value='synthetic-bootstrap';
   const submit=new Element();
   await el('login').handlers.submit({preventDefault(){},submitter:submit});
   assert.equal(requests.length,2,'successful local bootstrap must fetch its first snapshot despite the browser offline hint');
   assert.equal(requests[0].path,'/api/bootstrap');
   assert.ok(requests[1].path.startsWith('/api/view'));
   assert.equal(current.snapshot,'confirmed');
   assert.equal(submit.disabled,false);
   assert.equal(timers.size,1,'reachable local host retains bounded refresh scheduling');
 }else if(scenario==='offline_hint_unreachable_server'){
   bearer='synthetic-only';render(data);navigator.onLine=false;
   globalThis.fetch=async(path,options)=>{requests.push({path,options});throw new Error('synthetic connection failure')};
   await poll();
   assert.equal(requests.length,1,'local connection availability is established by the request');
   assert.equal(current.snapshot,'confirmed','connection failure must preserve the confirmed snapshot');
   assert.equal(delay,4000);assert.equal(timers.size,1);
   assert.ok([...timers.values()][0].ms>=3200&&[...timers.values()][0].ms<=4800);
   assert.equal(loading,false);
 }else if(scenario==='bounded_backoff_and_announcement_coalescing'){
   bearer='synthetic-only';render(data);navigator.onLine=false;
   const notice=el('notice');let noticeText=notice.textContent,noticeWrites=0;
   Object.defineProperty(notice,'textContent',{get(){return noticeText},set(value){noticeText=value;noticeWrites++}});
   globalThis.fetch=async(path,options)=>{requests.push({path,options});throw new Error('synthetic connection failure')};
   const expectedDelays=[4000,8000,16000,30000,30000,30000,30000,30000];
   for(const expectedDelay of expectedDelays){
     await poll();
     assert.equal(delay,expectedDelay,'repeated failure must increase backoff only to its declared cap');
     assert.equal(timers.size,1,'repeated failures must not accumulate retry timers');
     const timer=[...timers.values()][0];
     assert.ok(timer.ms>=expectedDelay*.8&&timer.ms<=expectedDelay*1.2,'jitter stays inside the bounded interval');
     assert.equal(current.snapshot,'confirmed');
   }
   assert.equal(requests.length,8);assert.equal(noticeWrites,1,'unchanged failure announcements must be coalesced');
   for(let index=0;index<100;index++)schedule();
   assert.equal(timers.size,1,'schedule bursts must coalesce to one retry');
   document.hidden=true;schedule();assert.equal(timers.size,0);
   document.hidden=false;
   globalThis.fetch=async(path,options)=>{requests.push({path,options});return response(200,data)};
   await poll();
   assert.equal(delay,2000,'confirmed local recovery restores the normal polling interval');
   assert.equal(timers.size,1);assert.equal(current.snapshot,'confirmed');
 }else if(scenario==='hidden_document'){
   bearer='synthetic-only';document.hidden=true;schedule();
   assert.equal(timers.size,0);assert.equal(requests.length,0);
 }else if(scenario==='expired_authentication'){
   bearer='synthetic-only';render(data);
   globalThis.fetch=async(path,options)=>{requests.push({path,options});return response(401,{error:'authentication_required'})};
   await poll();
   assert.equal(requests.length,1);assert.equal(bearer,null);assert.equal(timers.size,0);
 }else{
   bearer=null;await poll();schedule();assert.equal(requests.length,0);assert.equal(timers.size,0);
 }
})().catch(error=>{console.error(error.message);process.exitCode=1});
'''
    result = subprocess.run(
        [executable], input=harness + script + "\nconst scenario=" + json.dumps(scenario) + ";\n" + checks,
        text=True, encoding="utf-8", capture_output=True, timeout=20,
        **popen_flags(),
    )
    assert result.returncode == 0, result.stderr

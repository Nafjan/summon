"""Page source boundary tests only; rendered accessibility is a separate gate."""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
import sys
import re
import shutil
import subprocess

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from _workspace_page import page_bytes
from _spawn import popen_flags


class Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_document_has_unique_ids_task_navigation_and_labelled_evidence_dialog():
    parser = Elements()
    parser.feed(page_bytes(nonce="n" * 24).decode())
    ids = [attrs["id"] for _, attrs in parser.tags if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert {"objective", "criteria", "tasks", "timeline", "delivery-list", "assessment-list", "hold-list"} <= set(ids)
    assert any(tag == "nav" and attrs.get("aria-label") == "Workspace tasks" for tag, attrs in parser.tags)
    assert any(attrs.get("role") == "dialog" and attrs.get("aria-labelledby") == "evidence-title" for _, attrs in parser.tags)


def test_dynamic_content_and_session_code_are_not_html_or_browser_storage():
    source = page_bytes(nonce="n" * 24).decode()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "localStorage",
                      "location.hash", "document.cookie", "window.open", "eval("):
        assert forbidden not in source
    assert "textContent" in source and "credentials:'omit'" in source
    assert "let bearer=null" in source and "el('code').value=''" in source
    assert "URLSearchParams" in source and "q.set('cursor'" in source


def test_scripts_and_styles_require_nonce_and_have_no_remote_resources():
    parser = Elements()
    nonce = "n" * 24
    parser.feed(page_bytes(nonce=nonce).decode())
    executable = [(tag, attrs) for tag, attrs in parser.tags if tag in {"script", "style"}]
    assert len(executable) == 2
    assert all(attrs.get("nonce") == nonce for _, attrs in executable)
    assert not any("src" in attrs or "href" in attrs and not attrs["href"].startswith("#") for _, attrs in parser.tags)


@pytest.mark.parametrize("nonce", ["", "short", "\" onload=\"bad", None], ids=['p001_case_001', 'p001_case_002', 'p001_case_003', 'p001_case_004'])
def test_nonce_injection_refused(nonce):
    with pytest.raises(ValueError):
        page_bytes(nonce=nonce)


def test_source_contains_explicit_read_only_lifetime_and_bounded_reconnect_policy():
    source = page_bytes(nonce="n" * 24).decode()
    assert "Host-scoped context actions" in source and "does not stop the local server" in source
    assert "Math.min(30000,delay*2)" in source and "Math.random()" in source
    assert "navigator.onLine" in source and "document.hidden" in source
    assert "prefers-reduced-motion:reduce" in source and "scrollbar-gutter:stable" in source
    assert "Automatic retries stopped" in source and "Your reading position is preserved" in source
    assert "b.disabled=true" in source


def test_detail_drawer_focus_cycle_preserves_all_interactive_controls():
    source = page_bytes(nonce="n" * 24).decode()
    assert 'items=Array.from(drawer.querySelectorAll("button,[href],input,select,textarea,[tabindex]"))' in source
    assert 'event.shiftKey&&(active===first||!items.includes(active))' in source
    assert '!event.shiftKey&&(active===last||!items.includes(active))' in source
    assert 'detailFocus.isConnected' in source and 'details-open' in source


def test_detail_drawer_owns_scrim_expiry_recovery_and_structured_body_rendering():
    source = page_bytes(nonce="n" * 24).decode()
    assert "function closeActiveOverlay()" in source
    assert "el('scrim').addEventListener('click',closeActiveOverlay)" in source
    assert "clearPrivilegedOverlays()" in source
    assert "detailEl(\"details-list\").replaceChildren()" in source
    assert "detailEl(\"details-body\").replaceChildren()" in source
    assert "completePending(data);completePendingMessage(data);completePendingDisposition(data);completePendingLinked(data);" in source
    assert "d.criteria" in source and "d.disposition" in source and "d.next_decision" in source and "d.decision" in source
    assert "body.disabled=true" in source and "pre[data-detail-body]" in source
    assert "detailGeneration" in source and "generation!==detailGeneration" in source
    assert "#details-body pre{white-space:pre-wrap" in source


def test_message_composer_has_encrypted_bounded_recovery_and_same_key_states():
    source = page_bytes(nonce="n" * 24).decode()
    for required in ("PENDING_MESSAGE_SLOT", "AES-GCM", "tagLength:128", "messageAad",
                     "messageRequestBytes", "max_record_bytes", "queueUnsentPersistence",
                     "messageSealGeneration", "messageAdmissionStarted", "draft_stale", "message-status",
                     "message-recovery", "autocomplete=\"off\"", "not_observed", "/api/messages/draft-key", "/api/messages/lookup",
                     "messageRestored=false", "The pending operation remains encrypted"):
        assert required in source
    assert "localStorage" not in source
    assert "document.cookie" not in source


def test_document_encoding_has_no_mojibake():
    source = page_bytes(nonce="n" * 24).decode("utf-8")
    assert "Task-first preview / scoped local workspace" in source.replace("  ", " ")
    assert not any(value in source for value in ("\u00c2", "\u00e2", "\ufffd"))


def _script_harness():
    """Reuse the existing small DOM model; this does not measure rendering."""
    return r'''
const assert=require('node:assert/strict');
class Element {
 constructor(){this.dataset={};this.children=[];this.handlers={};this.hidden=false;this.disabled=false;this.isConnected=true;this.scrollTop=42;this.textContent='';this.value='';}
 append(...items){this.children.push(...items)}
 replaceChildren(...items){this.children=items}
 setAttribute(key,value){this[key]=value}
 getAttribute(key){return this[key]??null}
 addEventListener(key,handler){this.handlers[key]=handler}
 querySelectorAll(){return this.children}
 querySelector(selector){if(selector==='pre[data-detail-body]')return this.children.find(item=>item?.tagName==='pre'&&item.dataset?.detailBody==='true')??null;return null}
 closest(){return null}
 getBoundingClientRect(){return {top:0,bottom:30}}
 focus(){document.activeElement=this}
}
const nodes=new Map();
 globalThis.document={hidden:false,activeElement:null,getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement(tag){const element=new Element();element.tagName=tag;return element},addEventListener(){}};
const windowHandlers={};globalThis.window={addEventListener(type,handler){windowHandlers[type]=handler}};const stored=new Map();globalThis.sessionStorage={getItem(key){return stored.get(key)??null},setItem(key,value){stored.set(key,value)},removeItem(key){stored.delete(key)}};
Object.defineProperty(globalThis,'navigator',{value:{onLine:true},configurable:true});
globalThis.setTimeout=()=>1;globalThis.clearTimeout=()=>{};
'''


def test_actual_script_serializes_intents_without_background_focus_loss_and_anchors_refresh():
    """Execute shipped JS against a small DOM model; geometry needs a browser."""
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    harness = _script_harness()
    checks = r'''
(async()=>{
 const task=id=>({id,label:id,is_priority:false,role:'main',status:'pending',attempt_count:0,claim_state:'none',text:null,agent:{logical_label:'Unspecified',worker_label:'Unavailable'}});
 const event={id:'event-later',anchor:'anchor-later',label:'Evidence',revision:205};
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[task('one'),task('two')],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[event],next_cursor:'second'},controls:[],queue_evaluation:'Passive'};
 const tick=()=>new Promise(resolve=>setImmediate(resolve));
 render(data);bearer='synthetic-only';
 const focused=el('tasks').children[0];focused.focus();
 let waiting=[];let paths=[];let options=[];
 globalThis.fetch=(path,option)=>{paths.push(path);options.push(option);return new Promise(resolve=>waiting.push(value=>resolve({status:200,ok:true,json:async()=>value})))};
 const background=poll();assert.equal(paths.length,1);assert.equal(focused.disabled,false);assert.equal(document.activeElement,focused);
 selectTask('one');selectTask('two');assert.equal(paths.length,1);assert.equal(queuedIntent.id,'two');
 waiting.shift()({...data,workspace_revision:999,snapshot:'old-poll'});await background;await tick();assert.equal(paths.length,2);assert.ok(paths[1].includes('task=two'));assert.equal(current.workspace_revision,2);
 waiting.shift()({...data,selected_task_id:'two'});await tick();await tick();assert.equal(current.selected_task_id,'two');assert.equal(document.activeElement.dataset.taskId,'two');assert.equal(queuedIntent,null);assert.equal(loading,false);
 const paging=el('next-page').handlers.click();assert.ok(paths[2].includes('cursor=second'));assert.ok(paths[2].includes('task=two'));
 waiting.shift()({...data,selected_task_id:'two',timeline:{items:[event],next_cursor:null}});await paging;assert.equal(current.timeline.next_cursor,null);assert.equal(el('next-page').hidden,true);assert.equal(document.activeElement,el('timeline'));assert.equal(el('timeline').scrollTop,0);
 el('timeline').scrollTop=42;el('refresh').focus();const latest=el('refresh').handlers.click();assert.ok(paths[3].includes('anchor=anchor-later'));assert.ok(paths[3].includes('task=two'));
 waiting.shift()({...data,selected_task_id:'two',workspace_revision:3,timeline:{items:[event],next_cursor:null}});await latest;assert.equal(el('event-list').children[0].dataset.eventId,'event-later');assert.equal(document.activeElement,el('timeline'));assert.equal(el('timeline').scrollTop,42);
 el('evidence-close').focus();el('drawer').hidden=false;el('evidence-body').append(node('p','private'));el('details-drawer').hidden=false;el('details-list').append(node('p','private'));el('details-body').append(node('p','private'));el('scrim').hidden=false;el('app').inert=true;expire();assert.equal(document.activeElement,el('code'));assert.equal(el('drawer').hidden,true);assert.equal(el('evidence-body').children.length,0);assert.equal(el('details-drawer').hidden,true);assert.equal(el('details-list').children.length,0);assert.equal(el('details-body').children.length,0);assert.equal(el('scrim').hidden,true);assert.equal(el('app').inert,false);assert.equal(el('app').hidden,true);assert.equal(bearer,null);assert.equal(queuedIntent,null);

 bearer='fresh-synthetic';const delivery={id:'delivery_'+('d'.repeat(32)),label:'Queued',acknowledgement:null,certainty:{contact:'not_occurred',spend:'not_incurred',cleanup:'complete'},sequence:1,turn_id:null,reason:null,inherited_uncertainty:[]};
 const actionable={...data,deliveries:[delivery],operator_commands:{available:true,actions_by_delivery:{['delivery_'+('d'.repeat(32))]:['cancel_queued_context']},reason:null}};
 render(actionable);Object.defineProperty(globalThis,'crypto',{value:{getRandomValues(bytes){bytes.fill(7);return bytes}},configurable:true});
 const action=beginCommand('cancel_queued_context','delivery_'+('d'.repeat(32)));const originalBody=options.at(-1).body;assert.equal(paths.at(-1),'/api/commands');assert.equal(JSON.parse(originalBody).operation_key.length,32);
 const count=paths.length;beginCommand('cancel_queued_context','delivery_'+('d'.repeat(32)));assert.equal(paths.length,count);assert.equal(pendingCommand.status,'sending');
 waiting.shift()({operation_key:pendingCommand.body.operation_key,action:pendingCommand.body.action,status:'uncertain',revision:2,retry_with_new_key:false});await action;assert.equal(pendingCommand.status,'uncertain');assert.equal(el('command-lookup').hidden,false);
 expire();assert.equal(pendingCommand.status,'uncertain');assert.equal(JSON.stringify(pendingCommand.body),originalBody);const descriptor=JSON.parse(stored.get(PENDING_SLOT));assert.deepEqual(Object.keys(descriptor).sort(),['body','workspace']);assert.equal(stored.get(PENDING_SLOT).includes('synthetic-only'),false);pendingCommand=null;restorePending();assert.equal(pendingCommand.status,'uncertain');assert.equal(JSON.stringify(pendingCommand.body),originalBody);assert.equal(document.activeElement,el('code'));
 bearer='independently-refreshed';render(actionable);const lookup=el('command-lookup').handlers.click();assert.equal(paths.at(-1),'/api/commands/lookup');assert.equal(options.at(-1).body,originalBody);
 waiting.shift()({operation_key:pendingCommand.body.operation_key,action:pendingCommand.body.action,status:'pending',revision:3,retry_with_new_key:false});await lookup;assert.equal(pendingCommand.status,'pending');assert.equal(el('command-continue').hidden,false);
 const continuation=el('command-continue').handlers.click();assert.equal(paths.at(-1),'/api/commands');assert.equal(options.at(-1).body,originalBody);
 waiting.shift()({operation_key:pendingCommand.body.operation_key,action:pendingCommand.body.action,status:'recorded',revision:4,retry_with_new_key:false});await tick();assert.ok(paths.at(-1).startsWith('/api/view'));assert.equal(pendingCommand.status,'recorded');
 waiting.shift()({...actionable,workspace_revision:4,operator_commands:{available:true,actions_by_delivery:{},reason:null}});await continuation;assert.equal(pendingCommand,null);assert.equal(document.activeElement,el('timeline'));assert.equal(loading,false);assert.equal(stored.has(PENDING_SLOT),false);stored.set(PENDING_SLOT,'unreadable');restorePending();assert.ok(storageProblem);const earlier=paths.length;beginCommand('cancel_queued_context','delivery_'+('d'.repeat(32)));assert.equal(paths.length,earlier);

 storageProblem=null;stored.set(PENDING_SLOT,JSON.stringify(descriptor));pendingCommand=null;restorePending();render({...actionable,workspace_id:'workspace_'+('f'.repeat(32))});const mismatchCount=paths.length;await el('command-lookup').handlers.click();assert.equal(paths.length,mismatchCount);assert.ok(el('command-status').textContent.includes('different workspace'));
 pendingCommand=null;storageProblem=null;stored.clear();render(actionable);const save=sessionStorage.setItem;sessionStorage.setItem=()=>{throw new Error('storage unavailable')};const unsentCount=paths.length;beginCommand('cancel_queued_context','delivery_'+('d'.repeat(32)));assert.equal(paths.length,unsentCount);assert.ok(storageProblem);assert.equal(pendingCommand.status,'uncertain');sessionStorage.setItem=save;
 storageProblem=null;pendingCommand={...descriptor,status:'recorded',revision:4};stored.set(PENDING_SLOT,JSON.stringify(descriptor));const remove=sessionStorage.removeItem;sessionStorage.removeItem=()=>{throw new Error('storage unavailable')};render({...actionable,workspace_revision:4});assert.equal(pendingCommand.status,'recorded');assert.ok(storageProblem);sessionStorage.removeItem=remove;
 const messageCap={available:true,targets:[{id:'operator_target_'+('t'.repeat(32)),task_id:'task_'+('t'.repeat(32)),label:'Task 1',available:true}],reason:null};const draftCap={available:true,operator_scope:'a'.repeat(64),key_epoch:'epoch-1',max_text_bytes:2048,max_record_bytes:4096,reason:null};render({...actionable,operator_messages:messageCap,operator_drafts:draftCap});pendingMessage={workspace:actionable.workspace_id,body:{operation_key:'e'.repeat(32),target:messageCap.targets[0].id,text:'old'},status:'uncertain',revision:null,error:'custom retained error'};renderMessageComposer();assert.equal(el('message-status').textContent,'custom retained error');pendingMessage={workspace:actionable.workspace_id,body:{operation_key:'e'.repeat(32),target:messageCap.targets[0].id,text:'old'},status:'queued',revision:4};stored.set(PENDING_MESSAGE_SLOT,'ciphertext-only');el('message-text').value='old';completePendingMessage({...actionable,workspace_revision:4});assert.equal(el('message-text').value,'');assert.equal(pendingMessage,null);
})().catch(()=>{process.exitCode=1});
'''
    result = subprocess.run([executable], input=harness + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, "Shipped script behavior fixture failed"


def test_actual_script_links_one_pending_replacement_with_same_key():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    checks = r'''
(async()=>{
 const parent='linked-parent_'+('p'.repeat(32)),recipient='linked-recipient_'+('r'.repeat(32));
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',operator_linked_replacements:{available:true,targets:[{parent_target:parent,recipient_target:recipient,available:true}],reason:null}};
 const paths=[];Object.defineProperty(globalThis,'crypto',{value:{getRandomValues(bytes){bytes.fill(7);return bytes}},configurable:true});
 globalThis.fetch=async(path,options={})=>{paths.push({path,body:options.body});if(path==='/api/linked-replacements')return {status:200,ok:true,json:async()=>({schema:'summon.workspace.linked-replacement-result/v1',status:'recorded',operation_key:JSON.parse(options.body).operation_key,parent_target:parent,recipient_target:recipient,child_delivery:'delivery_'+('d'.repeat(32)),revision:3,execution_authorized:false,retry_with_new_key:false})};if(path.startsWith('/api/view'))return {status:200,ok:true,json:async()=>({...data,workspace_revision:3,operator_linked_replacements:{available:false,targets:[],reason:'linked_scope_required'}})};throw new Error(path)};
 bearer='synthetic';render(data);assert.equal(el('linked-panel').hidden,false);assert.equal(el('linked-actions').children.length,1);el('linked-actions').children[0].handlers.click();for(let i=0;i<20&&pendingLinked;i++)await new Promise(resolve=>setImmediate(resolve));assert.equal(pendingLinked,null);assert.equal(paths[0].path,'/api/linked-replacements');assert.equal(JSON.parse(paths[0].body).operation_key,'07'.repeat(16));assert.equal(paths[1].path.startsWith('/api/view'),true);assert.equal(stored.has(PENDING_LINKED_SLOT),false);
 const retained={workspace:data.workspace_id,body:{operation_key:'ab'.repeat(16),parent_target:parent,recipient_target:recipient}};stored.set(PENDING_LINKED_SLOT,JSON.stringify(retained));pendingLinked=null;linkedStorageProblem=null;restorePendingLinked();render(data);assert.equal(pendingLinked.status,'uncertain');assert.equal(el('linked-panel').hidden,false);assert.equal(el('linked-lookup').hidden,false);assert.equal(el('linked-actions').children.length,1);assert.equal(el('linked-actions').children[0].disabled,true);assert.equal(paths.length,2);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=_script_harness() + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, result.stderr or "Linked replacement page behavior fixture failed"


def test_actual_script_details_scrim_body_and_render_recovery_are_idempotent():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    checks = r'''
(async()=>{
 const task='task_'+('t'.repeat(32)),target='detail_'+('d'.repeat(32));
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[{id:task,label:'Task',is_priority:false,role:'main',status:'pending',attempt_count:0,claim_state:'none',text:null,agent:{logical_label:'Agent',worker_label:'Worker'}}],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',details:{available:true,targets:[{id:target,task_id:task,source_kind:'workspace_assessment',label:'Review',body_available:true}],reason:null}};
 let bodyCalls=0;globalThis.fetch=async(path)=>{if(path.startsWith('/api/details?'))return {status:200,ok:true,json:async()=>({schema:'summon.workspace.detail/v1',status:'detail',id:target,task_id:task,title:'Review',summary:'Safe metadata',source_kind:'workspace_assessment',state:'completed',criteria:[{label:'scope',result:'pass'}],disposition:'accept',next_decision:'none',decision:'approved',body_available:true})};if(path==='/api/details/body'){bodyCalls++;return {status:200,ok:true,json:async()=>({schema:'summon.workspace.detail-body/v1',body:'safe body'})}}throw new Error(path)};
 bearer='synthetic';el('drawer').hidden=true;render(data);await el('details-open').handlers.click();assert.equal(el('details-drawer').hidden,false);assert.equal(el('app').inert,true);assert.equal(el('details-list').children.length,1);const review=el('details-list').children[0];await review.handlers.click();assert.equal(el('details-body').children.length>=6,true);const bodyButton=el('details-body').children.find(item=>item.handlers?.click);assert.ok(bodyButton);await bodyButton.handlers.click();assert.equal(bodyCalls,1);assert.equal(bodyButton.disabled,true);const bodyCount=el('details-body').children.length;await bodyButton.handlers.click();assert.equal(bodyCalls,1);assert.equal(el('details-body').children.length,bodyCount);assert.ok(el('details-body').querySelector('pre[data-detail-body]'));
 el('scrim').handlers.click();assert.equal(el('details-drawer').hidden,true);assert.equal(el('scrim').hidden,true);assert.equal(el('app').inert,false);
 stored.set(PENDING_LINKED_SLOT,JSON.stringify({workspace:data.workspace_id,body:{operation_key:'ab'.repeat(16),parent_target:'p',recipient_target:'r'}}));pendingLinked={workspace:data.workspace_id,body:{operation_key:'ab'.repeat(16),parent_target:'p',recipient_target:'r'},status:'recorded',revision:2};render({...data,workspace_revision:3});assert.equal(pendingLinked,null);assert.equal(stored.has(PENDING_LINKED_SLOT),false);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=_script_harness() + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, result.stderr or "Shipped detail drawer behavior fixture failed"


def test_actual_script_details_discards_late_body_after_expiry():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    checks = r'''
(async()=>{
 const task='task_'+('t'.repeat(32)),target='detail_'+('d'.repeat(32));
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[{id:task,label:'Task',is_priority:false,role:'main',status:'pending',attempt_count:0,claim_state:'none',text:null,agent:{logical_label:'Agent',worker_label:'Worker'}}],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',details:{available:true,targets:[{id:target,task_id:task,source_kind:'workspace_assessment',label:'Review',body_available:true}],reason:null}};
 let releaseBody;globalThis.fetch=async(path)=>{if(path.startsWith('/api/details?'))return {status:200,ok:true,json:async()=>({schema:'summon.workspace.detail/v1',status:'detail',id:target,task_id:task,title:'Review',summary:'Safe metadata',source_kind:'workspace_assessment',state:'completed',criteria:[],body_available:true})};if(path==='/api/details/body')return new Promise(resolve=>{releaseBody=()=>resolve({status:200,ok:true,json:async()=>({schema:'summon.workspace.detail-body/v1',body:'late private body'})})});throw new Error(path)};
 bearer='synthetic';el('drawer').hidden=true;render(data);await el('details-open').handlers.click();const review=el('details-list').children[0];await review.handlers.click();const bodyButton=el('details-body').children.find(item=>item.handlers?.click);assert.ok(bodyButton);const pending=bodyButton.handlers.click();assert.equal(bodyButton.disabled,true);expire();assert.equal(el('details-drawer').hidden,true);assert.equal(el('details-body').children.length,0);assert.equal(bearer,null);releaseBody();await pending;assert.equal(el('details-body').children.length,0);assert.equal(el('details-drawer').hidden,true);assert.equal(el('app').hidden,true);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=_script_harness() + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, result.stderr or "Late detail response repopulated expired private DOM"


def test_actual_script_owner_snapshots_reconnect_and_never_evaluate_or_launch():
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    checks = r'''
(async()=>{
 const guidance='Queue evaluation is unavailable on this surface. Queueing context or reopening this workspace does not start workers.';
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'base',selected_task_id:null,workspace_revision:2,freshness:'snapshot_only',mode:{mode:'passive',owner_state:'unknown',verified_active:false},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:guidance};
 const requests=[];
 globalThis.fetch=async(path,options={})=>{requests.push({path,method:options.method||'GET'});throw new Error('synthetic unavailable')};
 for(const [state,label] of [['unknown','unknown'],['not_running','not running'],['stale','stale'],['active','active']]){
  render({...data,mode:{mode:state==='active'?'supervised':'passive',owner_state:state,verified_active:state==='active'}});
  assert.equal(el('owner-state').textContent,'Supervisor owner: '+label+' / confirmed snapshot only');
  assert.equal(el('mode').textContent,(state==='active'?'Supervised':'Passive')+' / snapshot');
  assert.equal(el('queue-mode').textContent,guidance);
 }
 assert.equal(requests.length,0);
 render({...data,mode:{mode:'supervised',owner_state:'PRIVATE_OWNER_PATH',verified_active:true}});
 assert.equal(el('mode').textContent,'Passive / snapshot');
 assert.ok(el('owner-state').textContent.startsWith('Supervisor owner: unknown'));
 assert.equal(el('owner-state').textContent.includes('PRIVATE'),false);
 const supervised={...data,snapshot:'observed-active',mode:{mode:'supervised',owner_state:'active',verified_active:true}};
 render(supervised);bearer='synthetic-only';
 await poll();
 assert.equal(current,supervised);
 assert.equal(el('mode').textContent,'Supervised / snapshot');
 assert.equal(el('owner-state').textContent,'Supervisor owner: active / last confirmed snapshot / connection uncertain');
 windowHandlers.offline();
 assert.ok(el('owner-state').textContent.includes('network hint changed'));
 windowHandlers.online();
 assert.ok(el('owner-state').textContent.includes('reconnection pending'));
 assert.equal(current,supervised);
 let response=supervised;
 globalThis.fetch=async(path,options={})=>{requests.push({path,method:options.method||'GET'});return {status:200,ok:true,json:async()=>response}};
 await poll();
 assert.equal(el('owner-state').textContent,'Supervisor owner: active / confirmed snapshot only');
 response={...data,snapshot:'observed-inactive',workspace_revision:3,mode:{mode:'passive',owner_state:'not_running',verified_active:false}};
 await poll();
 assert.equal(current,supervised);
 assert.equal(el('refresh').hidden,false);
 assert.equal(el('owner-state').textContent,'Supervisor owner: active / older snapshot / newer snapshot available');
 assert.ok(el('connection').textContent.includes('displayed revision 2'));
 await el('refresh').handlers.click();
 assert.equal(current,response);
 assert.equal(el('mode').textContent,'Passive / snapshot');
 assert.equal(el('owner-state').textContent,'Supervisor owner: not running / confirmed snapshot only');
 assert.equal(el('queue-mode').textContent,guidance);
 assert.ok(requests.length>0);
 assert.ok(requests.every(request=>request.method==='GET'&&request.path.startsWith('/api/view')));
 expire();
 assert.equal(current,null);
 assert.equal(el('mode').textContent,'Passive');
 assert.equal(el('owner-state').textContent,'Supervisor owner: unknown / no confirmed snapshot');
 assert.equal(el('connection').textContent,'Session expired');
 windowHandlers.offline();windowHandlers.online();
 assert.equal(el('owner-state').textContent,'Supervisor owner: unknown / no confirmed snapshot');
 assert.equal(el('connection').textContent,'Authentication required / no confirmed snapshot');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=_script_harness() + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, "Shipped owner snapshot behavior fixture failed"


def test_message_composer_crypto_recovery_and_delivery_states():
    """Exercise the message composer with platform WebCrypto in a Node DOM model."""
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the message composer fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    harness = r'''
const assert=require('node:assert/strict');const nodeCrypto=require('node:crypto').webcrypto;
globalThis.crypto=nodeCrypto;globalThis.btoa=value=>Buffer.from(value,'binary').toString('base64');globalThis.atob=value=>Buffer.from(value,'base64').toString('binary');
class Element{constructor(){this.dataset={};this.children=[];this.handlers={};this.hidden=false;this.disabled=false;this.scrollTop=0;this.textContent='';this.value='';this.inert=false}append(...items){this.children.push(...items)}replaceChildren(...items){this.children=items}setAttribute(key,value){this[key]=value}addEventListener(key,handler){this.handlers[key]=handler}querySelectorAll(){return this.children}getBoundingClientRect(){return {top:0,bottom:30}}focus(){document.activeElement=this}}
const nodes=new Map();globalThis.document={hidden:false,activeElement:null,getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id)},createElement(){return new Element()},addEventListener(){}};globalThis.window={addEventListener(){}};
const stored=new Map();globalThis.sessionStorage={getItem(key){return stored.get(key)??null},setItem(key,value){stored.set(key,value)},removeItem(key){stored.delete(key)}};Object.defineProperty(globalThis,'navigator',{value:{onLine:true},configurable:true});
'''
    checks = r'''
(async()=>{
 const target='operator_target_'+('t'.repeat(32));const workspace='workspace_'+('w'.repeat(32));
 const task={id:'task_'+('t'.repeat(32)),label:'Task 1',is_priority:false,role:'main',status:'pending',attempt_count:0,claim_state:'none',text:null,agent:{logical_label:'Unspecified',worker_label:'Unavailable'}};
 const data={schema:'summon.workspace.view/v1',workspace_id:workspace,snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[task],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',inbox:{summary:{total:0,unsettled:0,unknown_exposure:0},endpoints:[],deliveries:[]},operator_messages:{available:true,targets:[{id:target,task_id:task.id,label:'Task 1',available:true}],reason:null},operator_drafts:{available:true,operator_scope:'a'.repeat(64),key_epoch:'epoch-1',max_text_bytes:2048,max_record_bytes:4096,reason:null}};
      let draftKeyCalls=0,messageCalls=0,lookupCalls=0,nextMessageStatus='not_observed',failDraftKey=false,draftKeyStatus=0;const rawKey=Buffer.alloc(32,7).toString('base64url');
      globalThis.fetch=async(path,options)=>{if(path==='/api/messages/draft-key'){draftKeyCalls++;if(failDraftKey)throw new Error('transport');if(draftKeyStatus)return {status:draftKeyStatus,ok:false,json:async()=>({error:draftKeyStatus===403?'message_retention_required':'session_expired'})};return {status:200,ok:true,json:async()=>({key_b64:rawKey,operator_scope:data.operator_drafts.operator_scope,key_epoch:'epoch-1',max_text_bytes:2048,max_record_bytes:4096})}}if(path==='/api/messages'){messageCalls++;return {status:200,ok:true,json:async()=>({status:nextMessageStatus,operation_key:JSON.parse(options.body).operation_key,retry_with_new_key:false,revision:3,...(nextMessageStatus==='queued'?{delivery_state:'queued',message_id:'opaque',delivery_id:'opaque',stream_sequence:1}: {})})}}if(path==='/api/messages/lookup'){lookupCalls++;nextMessageStatus='queued';return {status:200,ok:true,json:async()=>({status:'queued',operation_key:JSON.parse(options.body).operation_key,retry_with_new_key:false,revision:4,delivery_state:'queued',message_id:'opaque',delivery_id:'opaque',stream_sequence:1})}}if(path.startsWith('/api/view'))return {status:200,ok:true,json:async()=>({...data,workspace_revision:4})};throw new Error(path)};
 bearer='synthetic';render(data);const input=el('message-text'),select=el('message-target');select.value=target;input.value=String.fromCharCode(945)+'\\nline';await persistUnsentMessage();assert.ok(stored.has(PENDING_MESSAGE_SLOT));const sealed=stored.get(PENDING_MESSAGE_SLOT);assert.equal(sealed.includes(String.fromCharCode(945)),false);assert.equal(sealed.includes('line'),false);
 const firstCalls=draftKeyCalls;input.value='first';queueUnsentPersistence();input.value='second';queueUnsentPersistence();await new Promise(resolve=>setTimeout(resolve,250));assert.ok(draftKeyCalls<=firstCalls+1);assert.ok(pendingMessage);assert.equal(pendingMessage.body.text,'second');
  pendingMessage=null;retainedMessageRecord=null;messageRestored=false;restorePendingMessageRecord();render(data);for(let i=0;i<30&&!pendingMessage&&!messageStorageProblem;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(pendingMessage.body.text,'second');assert.equal(input.value,'second');
  const sendingBody={operation_key:'b'.repeat(32),target,text:'sending-state'};await sealMessageRecord(sendingBody,'sending',null);const sendingSealed=stored.get(PENDING_MESSAGE_SLOT);pendingMessage=null;retainedMessageRecord=null;messageRestored=false;messageStorageProblem=null;restorePendingMessageRecord();render(data);for(let i=0;i<30&&!pendingMessage&&!messageStorageProblem;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(pendingMessage.status,'uncertain');assert.equal(el('message-lookup').hidden,false);assert.equal(el('message-lookup').disabled,false);stored.set(PENDING_MESSAGE_SLOT,sealed);
  const tampered=JSON.parse(stored.get(PENDING_MESSAGE_SLOT));const tamperedBytes=fromB64url(tampered.ciphertext);tamperedBytes[0]^=1;tampered.ciphertext=b64url(tamperedBytes);stored.set(PENDING_MESSAGE_SLOT,JSON.stringify(tampered));pendingMessage=null;retainedMessageRecord=null;messageRestored=false;messageStorageProblem=null;restorePendingMessageRecord();render(data);for(let i=0;i<30&&!messageStorageProblem;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(pendingMessage,null);assert.equal(el('message-text').disabled,true);assert.match(el('message-status').textContent,/not verified|unresolved/);
  const unreadable='unreadable-record';stored.set(PENDING_MESSAGE_SLOT,unreadable);pendingMessage=null;retainedMessageRecord=null;messageRestored=false;messageStorageProblem=null;restorePendingMessageRecord();const unreadableBytes=stored.get(PENDING_MESSAGE_SLOT);const beforeUnreadableCalls=messageCalls;render({...data,operator_messages:{available:false,targets:[],reason:'message_scope_required'},operator_drafts:{available:false,operator_scope:null,key_epoch:null,max_text_bytes:2048,max_record_bytes:4096,reason:'message_retention_required'}});input.value='replacement';await persistUnsentMessage();assert.equal(stored.get(PENDING_MESSAGE_SLOT),unreadableBytes);assert.equal(messageCalls,beforeUnreadableCalls);assert.equal(el('message-recovery').hidden,false);assert.match(el('message-recovery').textContent,/unresolved/);
      stored.set(PENDING_MESSAGE_SLOT,sealed);pendingMessage=null;retainedMessageRecord=null;messageRestored=false;messageStorageProblem=null;failDraftKey=true;restorePendingMessageRecord();const retainedBytes=stored.get(PENDING_MESSAGE_SLOT);render(data);for(let i=0;i<30&&!messageStorageProblem;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.equal(stored.get(PENDING_MESSAGE_SLOT),retainedBytes);assert.equal(messageCalls,beforeUnreadableCalls);assert.equal(el('message-text').disabled,true);failDraftKey=false;
      stored.clear();pendingMessage=null;retainedMessageRecord=null;messageStorageProblem=null;messageRestored=true;input.value='authority failure';await persistUnsentMessage();const authorityBytes=stored.get(PENDING_MESSAGE_SLOT);draftKeyStatus=403;const beforeAuthorityCalls=messageCalls;await sendPendingMessage(false);assert.equal(messageCalls,beforeAuthorityCalls);assert.equal(stored.get(PENDING_MESSAGE_SLOT),authorityBytes);assert.match(el('message-status').textContent,/Host message authority is unavailable/);assert.equal(el('message-lookup').hidden,true);draftKeyStatus=0;
 bearer=null;stored.clear();pendingMessage=null;retainedMessageRecord=null;messageRestored=true;messageWireTooLarge=false;input.value='\n'.repeat(2048);select.value=target;const beforeMessageCalls=messageCalls;await sendPendingMessage(false);assert.equal(messageCalls,beforeMessageCalls);assert.equal(pendingMessage,null);
  const originalSetItem=sessionStorage.setItem;const priorDraft=stored.get(PENDING_MESSAGE_SLOT);pendingMessage={workspace,body:{operation_key:'d'.repeat(32),target,text:'storage-failure'},status:'draft',revision:null,error:null};input.value='storage-failure';messageStorageProblem=null;messageRestored=true;sessionStorage.setItem=()=>{throw new Error('storage unavailable')};const beforeStorageFailureCalls=messageCalls;await sendPendingMessage(false);assert.equal(messageCalls,beforeStorageFailureCalls);assert.equal(pendingMessage.status,'draft');assert.match(el('message-status').textContent,/not sent|storage|did not confirm/);assert.equal(input.value,'storage-failure');assert.equal(stored.get(PENDING_MESSAGE_SLOT),priorDraft);sessionStorage.setItem=originalSetItem;
  input.value='safe';messageStorageProblem=null;messageWireTooLarge=false;messageRestored=true;pendingMessage=null;stored.clear();await sendPendingMessage(false);assert.equal(messageCalls,beforeMessageCalls+1);assert.equal(pendingMessage.status,'uncertain');assert.equal(el('message-lookup').hidden,false);await sendPendingMessage(true);assert.equal(lookupCalls,1);assert.equal(messageCalls,beforeMessageCalls+1);assert.equal(pendingMessage,null);assert.equal(input.value,'');
  const exact={v:1,state:'unsent',target,operation_key:'f'.repeat(32),operator_scope:'a'.repeat(64),key_epoch:'epoch-1',epoch:'epoch-1',iv:'A'.repeat(16),ciphertext:'A'.repeat(16)};while(JSON.stringify(exact).length<4096)exact.ciphertext+='A';if(JSON.stringify(exact).length>4096)exact.ciphertext=exact.ciphertext.slice(0,-(JSON.stringify(exact).length-4096));assert.equal(JSON.stringify(exact).length,4096);writeMessageRecord(exact);assert.equal(stored.get(PENDING_MESSAGE_SLOT).length,4096);stored.delete(PENDING_MESSAGE_SLOT);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=harness + script + checks,
                            text=True, capture_output=True, timeout=20, **popen_flags())
    assert result.returncode == 0, result.stderr or "Message composer behavior fixture failed"


def test_actual_script_pending_detail_body_keeps_escape_and_focus_return():
    """A disabled request button must not strand keyboard focus outside its dialog."""
    executable = shutil.which("node")
    if not executable:
        pytest.skip("Node required for the script behavior fixture")
    script = re.search(r'<script nonce="[^"]+">(.*?)</script>',
                       page_bytes(nonce="n" * 24).decode(), re.S).group(1)
    checks = r'''
(async()=>{
 const task='task_'+('t'.repeat(32)),target='detail_'+('d'.repeat(32));
 const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:0,closed:false,ready:false},tasks:[{id:task,label:'Task',is_priority:false,role:'main',status:'pending',attempt_count:0,claim_state:'none',text:null,agent:{logical_label:'Agent',worker_label:'Worker'}}],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive',details:{available:true,targets:[{id:target,task_id:task,source_kind:'workspace_assessment',label:'Review',body_available:true}],reason:null}};
 // Model the browser's loss of focus when its active native button is disabled.
 document.body=new Element();
 Object.defineProperty(Element.prototype,'disabled',{configurable:true,get(){return this._disabled??false},set(value){this._disabled=value;if(value&&document.activeElement===this)document.activeElement=document.body}});
 let bodyCalls=0,releaseBody;
 globalThis.fetch=async(path)=>{
  if(path.startsWith('/api/details?'))return {status:200,ok:true,json:async()=>({schema:'summon.workspace.detail/v1',status:'detail',id:target,task_id:task,title:'Review',summary:'Safe metadata',source_kind:'workspace_assessment',state:'completed',criteria:[],body_available:true})};
  if(path==='/api/details/body'){bodyCalls++;return new Promise(resolve=>{releaseBody=()=>resolve({status:200,ok:true,json:async()=>({schema:'summon.workspace.detail-body/v1',body:'late synthetic body'})})})}
  throw new Error(path);
 };
 bearer='synthetic';el('drawer').hidden=true;render(data);el('details-open').focus();await el('details-open').handlers.click();
 await el('details-list').children[0].handlers.click();
 const bodyButton=el('details-body').children.find(item=>item.handlers?.click);assert.ok(bodyButton);
 bodyButton.focus();const pendingBody=bodyButton.handlers.click();
 assert.equal(bodyCalls,1);assert.equal(bodyButton.disabled,true);
 assert.equal(document.activeElement,el('details-close'),'pending request must retain a keyboard target in the dialog');
 assert.equal(el('details-drawer').hidden,false);assert.equal(el('app').inert,true);
 await bodyButton.handlers.click();assert.equal(bodyCalls,1,'pending body request must not be duplicated');
 let prevented=false;el('details-drawer').handlers.keydown({key:'Escape',preventDefault(){prevented=true}});
 assert.equal(prevented,true);assert.equal(el('details-drawer').hidden,true);
 assert.equal(el('scrim').hidden,true);assert.equal(el('app').inert,false);
 assert.equal(document.activeElement,el('details-open'));
 releaseBody();await pendingBody;
 assert.equal(bodyCalls,1);assert.equal(el('details-body').children.length,0);
 assert.equal(el('details-body').querySelector('pre[data-detail-body]'),null);
 assert.equal(document.activeElement,el('details-open'),'late response must not move restored focus');
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([executable], input=_script_harness() + script + checks,
                            text=True, capture_output=True, timeout=10, **popen_flags())
    assert result.returncode == 0, result.stderr or "Pending detail request lost keyboard dismissal"

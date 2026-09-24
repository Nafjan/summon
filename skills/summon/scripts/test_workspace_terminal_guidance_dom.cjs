// Standalone synthetic DOM check: node test_workspace_terminal_guidance_dom.cjs.
// No browser geometry, server, provider, imports of Python runtime, or subprocesses.
const fs = require('node:fs');
const path = require('node:path');
const page = fs.readFileSync(path.join(__dirname, '_workspace_page.py'), 'utf8');
const fixtures = fs.readFileSync(path.join(__dirname, 'test_workspace_page.py'), 'utf8');
const harness = fixtures.split('def _script_harness():')[1].match(/return r'''([\s\S]*?)'''/)[1];
const script = page.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];
const checks = String.raw`
const data={schema:'summon.workspace.view/v1',workspace_id:'workspace_'+('w'.repeat(32)),snapshot:'same',selected_task_id:null,workspace_revision:2,mode:{mode:'passive'},goal:{objective:'Synthetic',criteria:[]},closure:{state:'open',unsettled_delivery_count:1,closed:false,ready:false},tasks:[],deliveries:[],assessments:[],holds:[],decisions:[],timeline:{items:[],next_cursor:null},controls:[],queue_evaluation:'Passive'};
const text=n=>[n.textContent,...n.children.map(text)].join(' ');
let checks=0;
for(const state of ['rejected','expired','cancelled','dead_lettered','held_for_recovery']){
 const reason=state==='held_for_recovery'?'contact_uncertain':state+'_reason_unavailable';
 const d={id:'delivery_'+('d'.repeat(32)),state,label:state,acknowledgement:null,certainty:{contact:'unknown',spend:'unknown',cleanup:'unknown'},sequence:1,turn_id:null,reason,inherited_uncertainty:['contact','spend'],action_guidance:{actions:[],unavailable_reason:'no_delivery_action_in_current_scope'}};
 render({...data,deliveries:[d]});
 const content=text(el('delivery-list'));
 assert.ok(content.includes((state==='held_for_recovery'?'Hold: ':'Reason: ')+reason.replaceAll('_',' ')));checks++;
 if(state!=='held_for_recovery'){assert.ok(!content.includes('Hold: '));checks++}
 assert.ok(content.includes('Delivery actions unavailable:'));checks++;
 assert.ok(content.includes('contact: unknown')&&content.includes('spend: unknown')&&content.includes('Acknowledgement: none'));checks++;
 assert.ok(!content.includes('private-account-token-abc123'));checks++;
}
for(const action of ['cancel_queued_context','dispose_held_context','retain_held_context','propose_linked_replacement']){
 const value=deliveryGuidance({action_guidance:{actions:[action],unavailable_reason:null}});
 assert.ok(value.startsWith('Available in current scope: '));checks++;
 assert.ok(value.includes('Each action is checked by the host; none implies retry or resolves contact/spend uncertainty.'));checks++;
}
assert.ok(deliveryGuidance({action_guidance:{actions:[],unavailable_reason:'read_only_export'}}).includes('read-only export'));checks++;
assert.ok(!deliveryGuidance({action_guidance:{actions:['private-account-token-abc123']}}).includes('private-account-token-abc123'));checks++;
console.log(checks+' synthetic DOM/guidance assertions passed');
`;
new Function('require', harness + script + checks)(require);

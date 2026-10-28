import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runBatch,classifyResponse} from './supabase/functions/portal-email/core.mjs';
const job={id:'fixture',idempotency_key:'immutable-key',request:{from:'sender@example.invalid',to:['approved@example.invalid'],subject:'test',text:'saved prices'}};
function harness(send) {
 let done=false; const calls=[];
 return {calls,options:{sender:'sender@example.invalid',limit:1,send,getReceipt:async()=>({}),rpc:async(name,args)=>{calls.push([name,args]);if(name==='portal_email_claim')return done?null:structuredClone(job);if(name==='portal_email_finish'){done=args.p_status==='DELIVERED';return true;}return [];}}};
}
test('provider outage retries the identical body/key, acceptance stops the next attempt',async()=>{
 const bodies=[];let n=0;const h=harness(async(body,key)=>{bodies.push([body,key]);if(n++===0)throw Error('secret provider body');return {status:200,body:{id:'provider-1'}};});
 assert.equal((await runBatch(h.options)).retried,1);
 assert.equal((await runBatch(h.options)).accepted,1);
 assert.equal((await runBatch(h.options)).claimed,0);
 assert.deepEqual(bodies[0],bodies[1]);assert(!JSON.stringify(h.calls).includes('secret provider body'));
});
test('acknowledgement failure stops the batch without a second send',async()=>{
 let count=0;const h=harness(async()=>{count++;return {status:200,body:{id:'provider-1'}};});const original=h.options.rpc;
 h.options.rpc=async(n,a)=>n==='portal_email_finish'?false:original(n,a);
 await assert.rejects(runBatch({...h.options,limit:2}),/ACKNOWLEDGED/);assert.equal(count,1);
});
for(const status of [429,500,503,408])test(`safe same-key retry for ${status}`,()=>assert.equal(classifyResponse(status,{}).status,'RETRY'));
for(const status of [400,401,403,422])test(`permanent failure ${status}`,()=>assert.equal(classifyResponse(status,{}).status,'BLOCKED'));
test('accepted is distinct from delivered and provider receipt id must match',async()=>{
 const h=harness(async()=>({status:200,body:{id:'provider-1'}}));const original=h.options.rpc;
 h.options.rpc=async(n,a)=>n==='portal_email_receipts'?[{id:'fixture',provider_id:'provider-1'}]:original(n,a);
 h.options.getReceipt=async()=>({id:'wrong',last_event:'delivered'});assert.equal((await runBatch(h.options)).receipts,0);
 h.options.getReceipt=async()=>({id:'provider-1',last_event:'delivered'});assert.equal((await runBatch(h.options)).receipts,1);
});

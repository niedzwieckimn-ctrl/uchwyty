import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {randomBytes} from 'node:crypto';
import {handleWebhook} from './supabase/functions/portal-email-webhook/core.mjs';
const {Webhook}=createRequire(new URL('./supabase/package.json',import.meta.url))('svix');
const secret='whsec_'+randomBytes(32).toString('base64');
const wh=new Webhook(secret);
const payload={type:'email.delivered',created_at:new Date().toISOString(),data:{email_id:'00000000-0000-4000-a000-000000000027',to:['fixture@example.invalid'],subject:'PRIVATE CONTENT'}};
function request({event=payload,time=new Date(),id='msg_fixture',signedBody,body,signature,method='POST',headers={}}={}){
  const raw=JSON.stringify(event),content=body??raw;
  return new Request('https://test.invalid/webhook',{method,body:method==='POST'?content:undefined,headers:{'svix-id':id,'svix-timestamp':String(Math.floor(time.getTime()/1000)),'svix-signature':signature??wh.sign(id,time,signedBody??raw),...headers}});
}
function harness(rpc=async()=>true){const calls=[];return {calls,secret,verify:(b,h,s)=>new Webhook(s).verify(b,h),rpc:async(n,a)=>{calls.push([n,a]);return rpc(n,a);}};}
test('actual Svix verification commits only minimal receipt metadata',async()=>{
 const h=harness();assert.equal((await handleWebhook(request(),h)).status,200);
 assert.equal(h.calls[0][0],'portal_email_webhook');assert.equal(h.calls[0][1].p_event_id,'msg_fixture');
 assert(!JSON.stringify(h.calls).includes('PRIVATE'));assert(!JSON.stringify(h.calls).includes('@'));
});
test('modified raw body and wrong secret fail before RPC',async()=>{
 const h=harness();assert.equal((await handleWebhook(request({body:JSON.stringify({...payload,type:'email.bounced'})}),h)).status,401);
 assert.equal((await handleWebhook(request(),{...h,secret:'whsec_'+randomBytes(32).toString('base64')})).status,401);assert.equal(h.calls.length,0);
});
for(const age of [-601,601])test(`Svix rejects replay timestamp offset ${age}s`,async()=>{const h=harness();assert.equal((await handleWebhook(request({time:new Date(Date.now()+age*1000)}),h)).status,401);assert.equal(h.calls.length,0);});
test('missing configuration, missing headers and wrong method fail closed',async()=>{
 const h=harness();assert.equal((await handleWebhook(request(),{...h,secret:''})).status,503);
 assert.equal((await handleWebhook(request({headers:{'svix-signature':''}}),h)).status,401);
 assert.equal((await handleWebhook(request({method:'GET'}),h)).status,405);assert.equal(h.calls.length,0);
});
test('commit failure asks provider to retry without sending an email',async()=>{const h=harness(async()=>{throw Error('db secret');});const r=await handleWebhook(request(),h);assert.equal(r.status,503);assert(!(await r.text()).includes('secret'));assert.equal(h.calls.length,1);});
test('provider retry uses the same persisted event identity',async()=>{const h=harness();await handleWebhook(request(),h);await handleWebhook(request(),h);assert.deepEqual(h.calls[0],h.calls[1]);});
test('legit historic event accepts fresh signature; event date remains intact',async()=>{
 const h=harness();const event={...payload,created_at:'2026-01-01T00:00:00Z'};
 assert.equal((await handleWebhook(request({event}),h)).status,200);assert.equal(h.calls[0][1].p_occurred_at,'2026-01-01T00:00:00.000Z');
});
test('unsupported signed event ignored and invalid email identity rejected',async()=>{
 const h=harness();assert.equal((await handleWebhook(request({event:{...payload,type:'domain.created'}}),h)).status,200);
 assert.equal((await handleWebhook(request({event:{...payload,data:{email_id:'wrong'}}}),h)).status,400);assert.equal(h.calls.length,0);
});
test('oversize actual body rejected even when Content-Length lies',async()=>{const h=harness();assert.equal((await handleWebhook(request({body:'x'.repeat(65537),headers:{'content-length':'1'}}),h)).status,413);assert.equal(h.calls.length,0);});
for(const type of ['sent','delivered','delivery_delayed','bounced','failed','complained','suppressed','scheduled','opened','clicked'])test(`verified email.${type} event reaches durable receipt RPC`,async()=>{
 const h=harness();assert.equal((await handleWebhook(request({event:{...payload,type:'email.'+type}}),h)).status,200);assert.equal(h.calls[0][1].p_event_type,'email.'+type);
});

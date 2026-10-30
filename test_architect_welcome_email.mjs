import {test} from 'node:test';
import assert from 'node:assert/strict';
import {renderWelcomeEmail,prepareWelcomeEmail,WELCOME_SUBJECT} from './supabase/functions/portal-email/welcome-render.mjs';
import {runBatch} from './supabase/functions/portal-email/core.mjs';
test('welcome HTML and text preserve approved copy, original CID assets and configurable link',()=>{
 const mail=renderWelcomeEmail('https://architekci.niedzwieccy.com/');
 assert.equal(mail.subject,'Witamy w programie Niedźwieccy dla Architektów!');
 assert.match(mail.html,/max-width:600px/);
 assert.match(mail.html,/v:roundrect/);
 assert.doesNotMatch(mail.html,/<script|<svg|{{PANEL_URL}}/);
 assert.match(mail.text,/Twoje konto zostało zatwierdzone i jest już aktywne/);
 assert.match(mail.text,/https:\/\/architekci.niedzwieccy.com\//);
 assert.equal(mail.attachments.length,4);
 for(const image of mail.attachments)assert.ok(mail.html.includes('cid:'+image.content_id));
 assert.throws(()=>renderWelcomeEmail('javascript:alert(1)'));
 assert.throws(()=>renderWelcomeEmail('https://user:secret@example.invalid/'));
});
test('render is frozen before send, preserves existing order payloads and stops after acceptance',async()=>{
 let sent=false,frozen=null,attempts=0;const bodies=[];
 const rpc=async(name,args)=>{
   if(name==='portal_email_claim')return sent?null:{id:'fixture',idempotency_key:'fixed-key',request:frozen||{from:'sender@example.invalid',to:['test@example.invalid'],subject:WELCOME_SUBJECT,_template:'architect-approved-v1'}};
   if(name==='portal_email_freeze_welcome')return frozen={from:'sender@example.invalid',to:['test@example.invalid'],subject:WELCOME_SUBJECT,html:args.p_html,text:args.p_text,attachments:args.p_attachments};
   if(name==='portal_email_finish'){sent=args.p_status==='DELIVERED';return true;}
   if(name==='portal_email_receipts')return [];
   assert.fail(name);
 };
 const options={rpc,limit:1,sender:'sender@example.invalid',prepare:prepareWelcomeEmail(rpc),getReceipt:()=>assert.fail('No receipt'),send:async(body,key)=>{bodies.push([body,key]);if(attempts++===0)throw Error('Unknown provider outcome');return {status:200,body:{id:'provider-id'}};}};
 assert.equal((await runBatch(options)).retried,1);
 options.prepare=prepareWelcomeEmail(rpc,'https://changed.example.invalid/');
 assert.equal((await runBatch(options)).accepted,1);
 assert.equal((await runBatch(options)).claimed,0);
 assert.deepEqual(bodies[0],bodies[1]);
 const oldOrder={from:'sender@example.invalid',to:['test@example.invalid'],subject:'Original order',text:'Original order body'};
 assert.equal(await options.prepare({request:oldOrder},'fixture'),oldOrder);
});

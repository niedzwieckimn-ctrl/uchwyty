import {runBatch} from './core.mjs';
import {prepareWelcomeEmail,DEFAULT_PANEL_URL} from './welcome-render.mjs';
const env=(key:string)=>Deno.env.get(key)||'';
Deno.serve(async(req:Request)=>{
  // Supabase may inject an sb_secret_ key into the legacy-named runtime variable.
  // Keep the scheduler's legacy JWT explicit; gateway JWT verification remains enabled.
  const service=env('PORTAL_EMAIL_WORKER_BEARER');
  if(!service || req.headers.get('Authorization')!==`Bearer ${service}`) return new Response('Unauthorized',{status:401});
  if(req.method!=='POST') return new Response('Method not allowed',{status:405});
  const provider=env('RESEND_API_KEY'),sender=env('PORTAL_EMAIL_FROM'),mode=env('PORTAL_EMAIL_MODE');
  const recipient=env('PORTAL_EMAIL_TEST_RECIPIENT');
  if(!provider || !sender || !['staging','production'].includes(mode) || (mode==='staging'&&!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(recipient)))
    return Response.json({status:'awaiting_configuration'},{status:503});
  const rpc=async(name:string,data:unknown)=>{
    const r=await fetch(`${env('SUPABASE_URL')}/rest/v1/rpc/${name}`,{method:'POST',headers:{apikey:service,Authorization:`Bearer ${service}`,'Content-Type':'application/json'},body:JSON.stringify(data),signal:AbortSignal.timeout(10000)});
    if(!r.ok) throw new Error('RPC_UNAVAILABLE');
    return await r.json();
  };
  // Sequential provider calls stay below Resend's default two requests/second.
  let previous=0;
  const providerFetch=async(path:string,init:RequestInit={})=>{
    await new Promise(r=>setTimeout(r,Math.max(0,600-(Date.now()-previous)))); previous=Date.now();
    return fetch(`https://api.resend.com${path}`,{...init,headers:{Authorization:`Bearer ${provider}`,'Content-Type':'application/json',...init.headers},signal:AbortSignal.timeout(10000)});
  };
  try {
    // Resolve receipts that arrived before the sender persisted its provider ID.
    await rpc('portal_email_sync_events',{});
    const summary=await runBatch({rpc,sender,testRecipient:mode==='staging'?recipient:'',
      prepare:prepareWelcomeEmail(rpc,env('ARCHITECT_PANEL_URL')||DEFAULT_PANEL_URL),
      send:async(body:unknown,key:string)=>{const r=await providerFetch('/emails',{method:'POST',headers:{'Idempotency-Key':key},body:JSON.stringify(body)});return {status:r.status,body:await r.json().catch(()=>({}))};},
      getReceipt:async(id:string)=>{const r=await providerFetch(`/emails/${encodeURIComponent(id)}`);if(!r.ok)throw new Error('RECEIPT_UNAVAILABLE');return r.json();}
    });
    await rpc('portal_email_sync_events',{});
    return Response.json(summary);
  }catch{ return Response.json({status:'worker_error'},{status:503}); }
});

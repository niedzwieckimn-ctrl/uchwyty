import { Webhook } from 'npm:svix@1.76.1';
import { handleWebhook } from './core.mjs';
const env=(name:string)=>Deno.env.get(name)||'';
Deno.serve((req:Request)=>handleWebhook(req,{
  secret:env('RESEND_WEBHOOK_SECRET'),
  verify:(body:string,headers:Record<string,string>,secret:string)=>new Webhook(secret).verify(body,headers),
  rpc:async(name:string,data:unknown)=>{
    const service=env('PORTAL_EMAIL_WORKER_BEARER');
    if(!service)throw new Error('Missing server configuration');
    const r=await fetch(`${env('SUPABASE_URL')}/rest/v1/rpc/${name}`,{
      method:'POST',headers:{apikey:service,Authorization:`Bearer ${service}`,'Content-Type':'application/json'},
      body:JSON.stringify(data),signal:AbortSignal.timeout(10000)
    });
    if(!r.ok)throw new Error('Receipt not committed');
    return r.json();
  }
}));

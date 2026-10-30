const TYPES = new Set(['email.sent','email.delivered','email.delivery_delayed','email.bounced','email.failed','email.complained','email.suppressed','email.scheduled','email.opened','email.clicked']);
const MAX_BYTES = 65536;

export async function handleWebhook(req, {secret, verify, rpc}) {
  if (req.method !== 'POST') return new Response('Method not allowed', {status:405});
  if (!secret) return new Response('Webhook not configured', {status:503});
  const headers = Object.fromEntries(['svix-id','svix-timestamp','svix-signature'].map(k=>[k,req.headers.get(k)||'']));
  if (Object.values(headers).some(v=>!v) || headers['svix-id'].length>128) return new Response('Invalid signature',{status:401});
  if (Number(req.headers.get('content-length'))>MAX_BYTES) return new Response('Too large',{status:413});
  let body;
  try {
    const reader=req.body?.getReader(); const parts=[]; let size=0;
    if (!reader) return new Response('Invalid body',{status:400});
    while(true) {
      const {done,value}=await reader.read(); if(done)break;
      size+=value.length;
      if(size>MAX_BYTES) {await reader.cancel();return new Response('Too large',{status:413});}
      parts.push(value);
    }
    const raw=new Uint8Array(size); let pos=0;
    for(const part of parts){raw.set(part,pos);pos+=part.length;}
    body=new TextDecoder('utf-8',{fatal:true}).decode(raw);
  } catch {return new Response('Invalid body',{status:400});}
  let event;
  try {event=verify(body,headers,secret);} catch {return new Response('Invalid signature',{status:401});}
  if (!TYPES.has(event?.type)) return Response.json({ok:true,ignored:true});
  const id=event?.data?.email_id, date=Date.parse(event?.created_at);
  if (typeof id!=='string' || !/^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/i.test(id) || !Number.isFinite(date) || date>Date.now()+300000)
    return new Response('Invalid event',{status:400});
  try {
    // No address, subject or email content is stored. Never invokes the sender.
    await rpc('portal_email_webhook',{p_event_id:headers['svix-id'],p_provider_id:id,p_event_type:event.type,p_occurred_at:new Date(date).toISOString()});
    return Response.json({ok:true});
  } catch {return new Response('Retry later',{status:503});}
}

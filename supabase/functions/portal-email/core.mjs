export function classifyResponse(status, body) {
  if (status >= 200 && status < 300 && typeof body?.id === 'string' && body.id.length < 129) return {status:'DELIVERED',code:'',id:body.id};
  if (status === 429 || status >= 500 || status === 408 || (status === 409 && body?.name === 'concurrent_idempotent_requests')) return {status:'RETRY',code:status===429?'PROVIDER_RATE_LIMITED':'PROVIDER_TRANSIENT',id:''};
  return {status:'BLOCKED',code:'PROVIDER_REJECTED',id:''};
}
export async function runBatch({rpc,send,getReceipt,sender,testRecipient='',limit=2,prepare}) {
  const summary={claimed:0,accepted:0,retried:0,receipts:0};
  for(let i=0;i<Math.min(2,Math.max(0,limit));i++) {
    const owner=crypto.randomUUID();
    const job=await rpc('portal_email_claim',{p_owner:owner,p_sender:sender,p_test_recipient:testRecipient});
    if(!job) break;
    summary.claimed++;
    // Persist rendered account mail before contacting the provider. Retries use
    // that exact snapshot even after template or environment changes.
    if(prepare)job.request=await prepare(job,owner);
    let result;
    try { const response=await send(job.request,job.idempotency_key); result=classifyResponse(response.status,response.body); }
    catch { result={status:'RETRY',code:'PROVIDER_OUTCOME_UNKNOWN_RETRY_SAME_KEY',id:''}; }
    const ack=await rpc('portal_email_finish',{p_id:job.id,p_owner:owner,p_status:result.status,p_error_code:result.code,p_provider_id:result.id});
    if(ack!==true) throw new Error('OUTCOME_NOT_ACKNOWLEDGED');
    if(result.status==='DELIVERED') summary.accepted++;
    if(result.status==='RETRY') summary.retried++;
  }
  for(const job of await rpc('portal_email_receipts',{})) {
    try {
      const receipt=await getReceipt(job.provider_id);
      if(receipt.id===job.provider_id && ['sent','delivered','delivery_delayed','bounced','complained','failed','suppressed'].includes(receipt.last_event)) {
        await rpc('portal_email_receipt',{p_id:job.id,p_provider_id:job.provider_id,p_status:receipt.last_event});
        summary.receipts++;
      }
    } catch { /* Read-only provider failure: preserve the last confirmed state. */ }
  }
  return summary;
}

"""Shared KSeF attempt references; no network work at import time."""
def attempt(b,iid,session_ref=None,invoice_ref=None,claim=False):
    if b.supabase_enabled():
        if claim:
            b.sync_local_rows_to_supabase('invoices','id',[iid])
            return b.supabase_request('/rest/v1/rpc/claim_ksef_attempt',method='POST',payload={'p_invoice':iid,'p_created':b.now_iso()}) or {}
        if session_ref is not None:
            b.supabase_request('/rest/v1/ksef_attempts',method='PATCH',params={'invoice_id':'eq.'+str(iid)},payload={'session_ref':session_ref,'invoice_ref':invoice_ref or ''})
        rows=b.supabase_request('/rest/v1/ksef_attempts',params={'invoice_id':'eq.'+str(iid),'select':'*'}) or []
        return rows[0] if rows else {}
    c=b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT * FROM ksef_attempts WHERE invoice_id=?',(iid,)).fetchone()
        if claim and old:return dict(old)
        if claim:c.execute('INSERT INTO ksef_attempts VALUES(?,?,?,?)',(iid,'','',b.now_iso()))
        if session_ref is not None:c.execute('UPDATE ksef_attempts SET session_ref=?,invoice_ref=? WHERE invoice_id=?',(session_ref,invoice_ref or '',iid))
        c.commit();return dict(old) if old else {}
    finally:c.close()

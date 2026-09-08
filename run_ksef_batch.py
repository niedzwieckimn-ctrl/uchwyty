"""Render entry point. --send is required to enable real KSeF/email calls."""
import os
os.environ['INPOST_PICKUP_WORKER']='0'
os.environ['SUPABASE_AUTO_SYNC_ON_WRITE']='0'
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import app as b

def candidates(now):
    start=os.environ.get('KSEF_AUTOMATION_START_DATE','').strip()
    if not start:raise ValueError('Ustaw KSEF_AUTOMATION_START_DATE na dzień rozpoczęcia automatu (YYYY-MM-DD).')
    datetime.strptime(start,'%Y-%m-%d')
    c=b.conn()
    try:
        rows=[dict(x) for x in c.execute('''SELECT i.id,k.ksef_number,k.status,COALESCE(m.sent_to_client,0) mailed FROM invoices i
            LEFT JOIN ksef_documents k ON k.invoice_id=i.id LEFT JOIN invoice_meta m ON m.invoice_id=i.id
            WHERE i.publication_state='complete' AND substr(i.created_at,1,10)>=? ORDER BY i.id''',(start,))]
    finally:c.close()
    return [x['id'] for x in rows if not (x['ksef_number'] and x['mailed']) and
            (now.hour==17 or x['ksef_number'] or x['status'] in ('sending','processing','unknown'))]

def main():
    now=datetime.now(ZoneInfo('Europe/Warsaw'))
    if b.supabase_enabled():b.pull_shared_tables_from_supabase(force=True)
    ids=candidates(now)
    if '--send' not in sys.argv:
        print(json.dumps({'dry_run':True,'local_time':now.isoformat(),'invoice_ids':ids}));return 0
    results=[]
    for iid in ids:
        try:
            with b.app.test_request_context('/invoices/'+str(iid)+'/ksef/send',method='POST'):
                b._refresh_domain_route_context()
                response=b.app.view_functions['invoice_ksef_send'](iid)
                if isinstance(response,tuple) and int(response[1])>=400:
                    raise RuntimeError(str(response[0]))
                doc=b.load_ksef_doc(iid);meta=b.load_invoice_meta(iid) or {}
                results.append({'invoice_id':iid,'status':doc.get('status'),'number_received':bool(doc.get('ksef_number')),'mailed':bool(meta.get('sent_to_client'))})
        except Exception as exc:
            b.app.logger.exception('KSeF: faktura %s',iid)
            results.append({'invoice_id':iid,'error':str(exc)[:250]})
    print(json.dumps({'local_time':now.isoformat(),'results':results},ensure_ascii=False))
    return 1 if any(x.get('error') or x.get('status') in ('error','unknown','rejected') or (x.get('number_received') and not x.get('mailed')) for x in results) else 0

if __name__=='__main__':raise SystemExit(main())

"""Recoverable invoice publication; file upload precedes the final database commit."""
import json

def initialize(c):
    cols={r[1] for r in c.execute('PRAGMA table_info(invoices)')}
    if 'publication_state' not in cols:
        c.execute("ALTER TABLE invoices ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'complete'")
    c.execute('''CREATE TABLE IF NOT EXISTS invoice_jobs(invoice_id INTEGER PRIMARY KEY REFERENCES invoices(id),
                items_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'preparing',error TEXT,updated_at TEXT NOT NULL)''')
    c.commit()

def stage(cur, invoice_id, items, now):
    cur.execute("UPDATE invoices SET publication_state='preparing' WHERE id=?",(invoice_id,))
    cur.execute('''INSERT INTO invoice_jobs(invoice_id,items_json,state,updated_at) VALUES(?,?,'preparing',?)
          ON CONFLICT(invoice_id) DO UPDATE SET items_json=excluded.items_json,state='preparing',error=NULL,updated_at=excluded.updated_at''',
                (invoice_id,json.dumps(items,ensure_ascii=False),now))

def finish(b, invoice_id):
    import invoice_stock
    c=b.conn()
    try:
        job=c.execute('SELECT * FROM invoice_jobs WHERE invoice_id=?',(invoice_id,)).fetchone()
        inv=c.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
        if not job or not inv:
            raise ValueError('Brak zapisanego zadania faktury')
        if job['state']=='complete':
            return
        items=json.loads(job['items_json']); inv=dict(inv)
        order=c.execute('SELECT * FROM orders WHERE id=?',(inv['order_id'],)).fetchone()
    finally:
        c.close()
    try:
        pdf,net,gross=b.generate_order_invoice_pdf(order,items,b.invoice_meta_payload(inv))
        packing=b.generate_invoice_packing_list_pdf(order,items,b.invoice_meta_payload(inv),pdf)
        path=b.upload_invoice_pdfs_to_supabase(invoice_id,inv['invoice_no'],pdf,packing)
        if not path:
            raise ValueError('Brak potwierdzenia zapisu PDF')
        remote_stock=invoice_stock.apply_remote(b,invoice_id,items)
        c=b.conn()
        try:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT items_json FROM invoice_jobs WHERE invoice_id=?',(invoice_id,)).fetchone()
            if current[0]!=job['items_json']:
                raise ValueError('Faktura została zmieniona podczas przygotowania PDF')
            invoice_stock.apply_local(c,invoice_id,items,remote_stock)
            c.execute('DELETE FROM invoice_allocations WHERE invoice_id=?',(invoice_id,))
            for it in items:
                item_id=int(it.get('order_item_id') or it.get('id') or 0)
                oid=int(it.get('source_order_id') or it.get('order_id') or 0)
                qty=int(it.get('qty') or 0)
                source=c.execute('SELECT qty,product_id,sku FROM order_items WHERE id=? AND order_id=?',(item_id,oid)).fetchone()
                allocated=c.execute('SELECT COALESCE(SUM(qty),0) FROM invoice_allocations WHERE order_item_id=?',(item_id,)).fetchone()[0]
                if not source or qty<=0 or allocated+qty>source['qty']:
                    raise ValueError('Ilość faktury przekracza niezafakturowaną część zamówienia')
                c.execute('''INSERT INTO invoice_allocations(invoice_id,order_id,order_item_id,product_id,sku,qty,created_at)
                             VALUES(?,?,?,?,?,?,?)''',(invoice_id,oid,item_id,source['product_id'],source['sku'],qty,b.now_iso()))
            c.execute('''INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,sent_to_client,seen_by_client,payment_reminder,paid,updated_at)
                         VALUES(?,?,?,0,0,0,0,?) ON CONFLICT(invoice_id) DO UPDATE SET pdf_path=excluded.pdf_path,
                         invoice_items_json=excluded.invoice_items_json,updated_at=excluded.updated_at''',
                      (invoice_id,path,job['items_json'],b.now_iso()))
            c.execute("UPDATE invoices SET total_net=?,total_gross=?,publication_state='preparing' WHERE id=?",(net,gross,invoice_id))
            c.execute("UPDATE invoice_jobs SET state='ready',error=NULL,updated_at=? WHERE invoice_id=?",(b.now_iso(),invoice_id))
            c.commit()
        except Exception:
            c.rollback();raise
        finally:
            c.close()
        if b.supabase_enabled():
            c=b.conn()
            ids=[x[0] for x in c.execute("SELECT id FROM invoice_allocations WHERE invoice_id=?",(invoice_id,))]
            c.close()
            b.supabase_delete_rows("invoice_allocations", {"invoice_id":invoice_id})
            b.sync_local_rows_to_supabase("invoice_allocations","id",ids)
            b.sync_invoice_meta_to_supabase(invoice_id)
            b.sync_local_rows_to_supabase("invoices","id",[invoice_id])
            b.supabase_update_rows("invoices", {"publication_state":"complete"}, {"id":invoice_id})
        c=b.conn()
        try:
            c.execute("UPDATE invoices SET publication_state='complete' WHERE id=?",(invoice_id,))
            c.execute("UPDATE invoice_jobs SET state='complete',error=NULL WHERE invoice_id=?",(invoice_id,))
            c.commit()
        finally:c.close()
    except Exception as exc:
        c=b.conn()
        try:
            c.execute('UPDATE invoice_jobs SET error=?,updated_at=? WHERE invoice_id=?',(str(exc),b.now_iso(),invoice_id));c.commit()
        finally:
            c.close()
        raise

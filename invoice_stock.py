"""Account only invoice quantities; never issue on a courier scan."""
import json

SCHEMA='''CREATE TABLE IF NOT EXISTS invoice_stock_applied(
 invoice_id INTEGER NOT NULL,order_item_id INTEGER NOT NULL,product_id INTEGER NOT NULL,
 qty INTEGER NOT NULL CHECK(qty>=0),PRIMARY KEY(invoice_id,order_item_id))'''

def initialize(c):
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='invoice_stock_applied'").fetchone()
    c.execute(SCHEMA)
    if not exists:
        # Preserve historical physical balances, including manual corrections.
        c.execute('''INSERT INTO invoice_stock_applied SELECT a.invoice_id,a.order_item_id,i.product_id,SUM(a.qty)
            FROM invoice_allocations a JOIN order_items i ON i.id=a.order_item_id GROUP BY a.invoice_id,a.order_item_id,i.product_id''')
    c.execute('''CREATE TABLE IF NOT EXISTS ksef_attempts(invoice_id INTEGER PRIMARY KEY,session_ref TEXT,invoice_ref TEXT,created_at TEXT NOT NULL)''')
    c.commit()

def desired(cur,items):
    out={}
    for item in items:
        iid=int(item.get('order_item_id') or item.get('id') or 0)
        qty=int(item.get('qty') or 0)
        if iid<=0 or qty<=0 or iid in out:raise ValueError('Nieprawidłowe lub powtórzone pozycje faktury')
        row=cur.execute('SELECT product_id,qty FROM order_items WHERE id=?',(iid,)).fetchone()
        if not row or qty>row['qty']:raise ValueError('Ilość faktury przekracza zamówienie')
        out[iid]=(int(row['product_id']),qty)
    return out

def apply_local(cur,invoice_id,items,remote_stock=None):
    wanted=desired(cur,items)
    prior={int(x['order_item_id']):(int(x['product_id']),int(x['qty'])) for x in cur.execute('SELECT * FROM invoice_stock_applied WHERE invoice_id=?',(invoice_id,))}
    deltas={}
    for iid in sorted(set(wanted)|set(prior)):
        pid,qty=wanted.get(iid,(prior.get(iid,(0,0))[0],0))
        old=prior.get(iid,(pid,0))[1]
        deltas[pid]=deltas.get(pid,0)+qty-old
    if remote_stock is not None:
        for pid in remote_stock:deltas.setdefault(int(pid),0)
    for pid,delta in deltas.items():
        if remote_stock is not None:
            if str(pid) not in remote_stock:raise ValueError('Brak potwierdzonego stanu produktu')
            cur.execute('INSERT INTO stock(product_id,qty) VALUES(?,?) ON CONFLICT(product_id) DO UPDATE SET qty=excluded.qty',(pid,int(remote_stock[str(pid)])))
        elif delta:
            changed=cur.execute('UPDATE stock SET qty=qty-? WHERE product_id=? AND qty>=?',(delta,pid,max(0,delta)))
            if changed.rowcount!=1:raise ValueError('Brak wystarczającego stanu magazynowego')
    for iid in set(wanted)|set(prior):
        pid,qty=wanted.get(iid,(prior.get(iid,(0,0))[0],0))
        cur.execute('INSERT INTO invoice_stock_applied VALUES(?,?,?,?) ON CONFLICT(invoice_id,order_item_id) DO UPDATE SET qty=excluded.qty,product_id=excluded.product_id',(invoice_id,iid,pid,qty))
    return list(deltas)

def apply_remote(b,invoice_id,items):
    if not b.supabase_enabled():return None
    b.sync_local_rows_to_supabase('invoices','id',[invoice_id])
    rows=[{'order_item_id':int(x.get('order_item_id') or x.get('id') or 0),'qty':int(x['qty'])} for x in items]
    result=b.supabase_request('/rest/v1/rpc/apply_invoice_stock',method='POST',payload={'p_invoice':int(invoice_id),'p_items':rows})
    if not isinstance(result,dict) or not isinstance(result.get('stock'),dict):raise ValueError('Brak potwierdzenia odjęcia stanu w Supabase')
    return result['stock']


def clear(b,invoice_id):
    remote=apply_remote(b,invoice_id,[])
    c=b.conn()
    try:
        c.execute('BEGIN IMMEDIATE')
        apply_local(c,invoice_id,[],remote)
        c.execute('DELETE FROM invoice_jobs WHERE invoice_id=?',(invoice_id,))
        c.commit()
    except Exception:
        c.rollback();raise
    finally:c.close()

"""Keep prior labels when a partly fulfilled order is packed again."""
import json
SCHEMA='''CREATE TABLE IF NOT EXISTS inpost_shipment_history(
 order_id INTEGER NOT NULL,shipment_id TEXT NOT NULL,snapshot_json TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(order_id,shipment_id))'''
COLLECTED={'collected_from_sender','taken_by_courier_from_pok','collected_by_courier','taken_by_courier','adopted_at_source_branch','sent_from_source_branch','adopted_at_sorting_center','sent_from_sorting_center','adopted_at_target_branch','out_for_delivery_to_address','out_for_delivery','ready_to_pickup','pickup_reminder_sent','delivered','returned_to_sender'}

def prepare_next(b,order_ids):
    c=b.conn()
    try:rows=[dict(x) for x in c.execute('SELECT * FROM orders WHERE id IN ('+','.join('?' for _ in order_ids)+')',tuple(order_ids))]
    finally:c.close()
    existing=[x for x in rows if x.get('inpost_shipment_id')]
    for row in existing:
        shipment=b.inpost_get_shipment(str(row['inpost_shipment_id']))
        if str(shipment.get('status') or '').lower() not in COLLECTED:
            raise ValueError('To zamówienie ma aktywną przesyłkę InPost. Kolejną paczkę przygotuj po potwierdzeniu odbioru poprzedniej przez kuriera.')
    for row in existing:
        history={'order_id':row['id'],'shipment_id':str(row['inpost_shipment_id']),'snapshot_json':json.dumps(row,ensure_ascii=False),'created_at':b.now_iso()}
        cleared={'inpost_shipment_id':'','tracking_no':'','inpost_label_format':'','shipped_at':''}
        if b.supabase_enabled():
            b.supabase_request('/rest/v1/inpost_shipment_history',method='POST',params={'on_conflict':'order_id,shipment_id'},payload=history,prefer='resolution=ignore-duplicates')
            b.supabase_update_rows('orders',cleared,{'id':row['id']})
        c=b.conn()
        try:
            c.execute('INSERT OR IGNORE INTO inpost_shipment_history VALUES(?,?,?,?)',tuple(history.values()))
            c.execute("UPDATE orders SET inpost_shipment_id='',tracking_no='',inpost_label_format='',shipped_at='' WHERE id=? AND inpost_shipment_id=?",(row['id'],str(row['inpost_shipment_id'])))
            c.commit()
        finally:c.close()

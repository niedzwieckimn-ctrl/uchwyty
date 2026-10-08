"""Release deleted invoice bindings while retaining packing/shipping history."""
import json


def detach(b, db, invoice):
    invoice_id = int(invoice['id'])
    lists = [dict(r) for r in db.execute('SELECT * FROM packing_lists WHERE invoice_id=?', (invoice_id,))]
    batches = [dict(r) for r in db.execute('SELECT * FROM packing_batches WHERE invoice_id=?', (invoice_id,))]
    if not lists and not batches:
        return []
    members = {int(invoice['order_id'])}
    for batch in batches:
        members.add(int(batch['root_order_id']))
        members.update(int(r[0]) for r in db.execute('SELECT order_id FROM packing_allocations WHERE batch_id=?', (batch['id'],)))
    # This durable receipt preserves the old invoice identity even if its number
    # or local SQLite id is later reused. Allocations, files and shipments stay.
    receipt = json.dumps({'invoice_id': invoice_id, 'invoice_no': invoice['invoice_no'],
                         'removed_at': b.now_iso(), 'packing_lists': lists,
                         'packing_batches': batches}, ensure_ascii=False)
    for oid in sorted(members):
        db.execute('INSERT OR REPLACE INTO fulfillment_verifications VALUES(?,?,?)',
                   (oid, 'invoice_removed:' + str(invoice_id), receipt))
    db.execute('UPDATE packing_batches SET invoice_id=NULL WHERE invoice_id=?', (invoice_id,))
    db.execute('UPDATE packing_lists SET invoice_id=NULL,revision=revision+1 WHERE invoice_id=?', (invoice_id,))
    db.execute("DELETE FROM fulfillment_documents WHERE kind='invoice' AND document_id=?", (invoice_id,))
    import packing_versions
    packing_versions.stage_evidence(b, db, sorted(members))
    return sorted(members)

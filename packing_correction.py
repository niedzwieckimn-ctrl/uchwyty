"""Explicit correction mail for a verified current packing version.

Receipts use existing durable fulfillment verifications, with the existing CAS
publication before any external send. Unknown outcomes never authorize a retry.
"""
import hashlib
import html
import json
import uuid
from pathlib import Path

import packing_versions
import reconciliation_store


class CorrectionConflict(ValueError):
    pass


def current(b, order_id, file_hash):
    db = b.conn()
    try:
        package = packing_versions.current_for_order(db, order_id)
        if not package or package['shipment_confirmed']:
            raise CorrectionConflict('Nie ma aktualnej, niewysłanej listy pakowej dla tego zamówienia.')
        document = db.execute("""SELECT d.*,pb.root_order_id FROM fulfillment_document_history d
            JOIN packing_batches pb ON pb.id=d.document_id
            WHERE d.order_id=? AND d.kind='packing_list' AND d.document_id=? AND d.file_hash=?""",
            (order_id, package['batch_id'], file_hash)).fetchone()
        if not document:
            raise CorrectionConflict('Lista zmieniła się w innej karcie. Odśwież zamówienie i sprawdź aktualną wersję.')
        content = Path(document['path']).read_bytes()
        if hashlib.sha256(content).hexdigest() != file_hash:
            raise CorrectionConflict('Nie można potwierdzić zapisanego PDF listy pakowej.')
        root_id = int(document['root_order_id'])
        root = dict(db.execute('SELECT * FROM orders WHERE id=?', (root_id,)).fetchone())
        recipient = b._email_key(package['customer']['email'])
        recipients = {b._email_key(r[0]) for r in db.execute('''SELECT DISTINCT customer_email_snapshot
            FROM packing_allocations WHERE batch_id=?''', (package['batch_id'],))}
        if not recipient or '@' not in recipient or recipients != {recipient}:
            raise CorrectionConflict('Lista nie ma jednego potwierdzonego adresu e-mail klienta.')
        identity = hashlib.sha256((package['packing_list_key'] + ':' + file_hash).encode()).hexdigest()
        kind = 'packing_correction:' + identity
        row = db.execute('SELECT payload FROM fulfillment_verifications WHERE order_id=? AND kind=?',
                         (root_id, kind)).fetchone()
        receipt = json.loads(row[0]) if row else None
        return dict(package=package, root=root, root_id=root_id, recipient=recipient,
                    file_hash=file_hash, kind=kind, receipt=receipt)
    finally:
        db.close()


def print_current(b, info):
    """A new layout of the same saved quantities; historical originals stay intact."""
    package = info['package']
    meta = dict(invoice_no=info['root']['order_no'], document_label_key='order',
                buyer_name=package['customer']['name'], buyer_email=info['recipient'],
                packing_document_token='print-v2-' + info['file_hash'])
    return b.generate_invoice_packing_list_pdf(info['root'], packing_versions.pdf_items(package), meta)


def _persist(b, info, receipt):
    db = b.conn()
    try:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR REPLACE INTO fulfillment_verifications VALUES(?,?,?)',
                   (info['root_id'], info['kind'], json.dumps(receipt, ensure_ascii=False)))
        reconciliation_store.stage(b, info['root_id'], connection=db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    reconciliation_store.publish(b, info['root_id'])


def send(b, order_id, file_hash):
    from fulfillment_operations import ui_write
    # Read only to choose the canonical lock shared by all members of this list.
    initial = current(b, order_id, file_hash)
    with ui_write(initial['root_id']):
        packing_versions.prepare_write_evidence(b, [order_id, initial['root_id']])
        info = current(b, order_id, file_hash)
        old = info['receipt']
        if old:
            if old['status'] == 'accepted':
                return dict(ok=True, duplicate=True, message='Ta poprawiona lista została już przekazana do wysyłki.')
            if old['status'] != 'rejected':
                raise CorrectionConflict('Poprzednia próba wysyłki wymaga sprawdzenia. Kolejny e-mail nie został wysłany.')
        path = print_current(b, info)
        content = Path(path).read_bytes()
        numbers = ', '.join(item['order_number'] for item in info['package']['orders'])
        receipt = dict(status='running', file_hash=file_hash, recipient=info['recipient'],
                       attempt_id=str(uuid.uuid4()),
                       attachment_hash=hashlib.sha256(content).hexdigest(),
                       total_qty=info['package']['total_qty'], order_numbers=numbers,
                       attempted_at=b.now_iso())
        # A crash/timeout after this point leaves a durable stop, never permission to resend.
        _persist(b, info, receipt)
        subject = 'Poprawiona lista pakowa - ' + numbers
        text = ('W załączniku przesyłamy poprawioną listę pakową. '
                'Prosimy traktować ją jako aktualną wersję zamiast poprzedniej listy dla tej paczki.\n'
                f'Zamówienia: {numbers}\nŁącznie: {info["package"]["total_qty"]} szt.\nZespół Niedźwieccy')
        try:
            result = b.send_email(info['recipient'], subject,
                '<p>' + html.escape(text).replace('\n', '<br>') + '</p>', text,
                attachments=[dict(filename='poprawiona-lista-pakowa.pdf', content=content)],
                idempotency_key='packing-correction/' + info['kind'].split(':')[1] + '/' + receipt['attempt_id'])
        except Exception as exc:
            result = dict(ok=False, delivery_outcome='unknown', error=type(exc).__name__)
        result = result if isinstance(result, dict) else {}
        accepted = bool(result.get('ok'))
        receipt.update(status='accepted' if accepted else
                       'rejected' if result.get('delivery_outcome') == 'rejected' or result.get('skipped') else 'unknown',
                       completed_at=b.now_iso(), provider_id=(result.get('body') if isinstance(result.get('body'), dict) else {}).get('id', ''))
        try:
            _persist(b, info, receipt)
        except Exception:
            b.app.logger.exception('PACKING_CORRECTION_RECEIPT order_id=%s', info['root_id'])
            if accepted:
                return dict(ok=True, message='Dostawca przyjął poprawioną listę. Zapis potwierdzenia czeka na synchronizację; nie wysyłaj ponownie.')
            raise CorrectionConflict('Wynik wysyłki wymaga sprawdzenia. Nie wysyłaj ponownie.')
        if not accepted:
            raise CorrectionConflict('Dostawca odrzucił e-mail. Sprawdź konfigurację przed ponowieniem.'
                                     if receipt['status'] == 'rejected' else
                                     'Nie potwierdzono wyniku wysyłki. Nie wysyłaj ponownie przed sprawdzeniem u dostawcy.')
        return dict(ok=True, message='Poprawiona lista pakowa została przekazana do wysyłki do klienta.')


def form_version(db, order_id):
    """Stable browser revision includes other lists and billing for this customer."""
    rows = db.execute('''SELECT o.id,o.status,d.file_hash,d.document_id,
        (SELECT GROUP_CONCAT(ia.id || ':' || ia.qty) FROM invoice_allocations ia WHERE ia.order_id=o.id)
        FROM orders o LEFT JOIN fulfillment_documents d ON d.order_id=o.id AND d.kind='packing_list'
        WHERE o.id=? OR (TRIM(COALESCE(o.customer_email,''))<>'' AND
          LOWER(TRIM(o.customer_email))=(SELECT LOWER(TRIM(customer_email)) FROM orders WHERE id=?))
        ORDER BY o.id''', (order_id, order_id)).fetchall()
    return hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()


def withdraw(b, order_id, file_hash):
    """Withdraw an unbilled parcel; retain its immutable PDF/allocations as history."""
    from fulfillment_operations import ui_write
    initial = current(b, order_id, file_hash)
    with ui_write(initial['root_id']):
        packing_versions.prepare_write_evidence(b, [order_id, initial['root_id']])
        info = current(b, order_id, file_hash)
        package = info['package']
        db = b.conn()
        try:
            db.execute('BEGIN IMMEDIATE')
            logical = db.execute('SELECT * FROM packing_lists WHERE packing_list_id=?',
                                 (package['packing_list_key'],)).fetchone()
            if not logical or logical['current_batch_id'] != package['batch_id']:
                raise CorrectionConflict('Lista zmieniła się. Odśwież zamówienie.')
            members = package['order_ids']
            marks = ','.join('?' for _ in members)
            pending = db.execute(f'''SELECT 1 FROM invoices i WHERE COALESCE(i.publication_state,'complete')<>'complete'
                AND (i.order_id IN ({marks}) OR EXISTS(SELECT 1 FROM invoice_allocations a
                     WHERE a.invoice_id=i.id AND a.order_id IN ({marks})))''', (*members, *members)).fetchone()
            if package['invoice_id'] or logical['invoice_id'] or pending:
                raise CorrectionConflict('Lista jest powiązana z fakturą lub jej niedokończonym zapisem. Nie można jej wycofać.')
            if db.execute('SELECT 1 FROM packing_shipments WHERE packing_list_id=?',
                          (package['packing_list_key'],)).fetchone():
                raise CorrectionConflict('Paczka została już wysłana. Nie można wycofać listy.')
            if db.execute(f'''SELECT 1 FROM orders WHERE id IN ({marks}) AND
                (COALESCE(inpost_shipment_id,'')<>'' OR COALESCE(tracking_no,'')<>'' OR COALESCE(warehouse_issued,0)=1)''', members).fetchone():
                raise CorrectionConflict('Lista ma już nadanie lub wydanie magazynowe. Najpierw sprawdź realizację.')
            for member in packing_versions.evidence_members(db, members):
                db.execute('''INSERT INTO fulfillment_verifications VALUES(?,?,?)''',
                           (member, 'packing_cancel:' + package['packing_list_key'],
                            json.dumps(dict(file_hash=file_hash, cancelled_at=b.now_iso()))))
            # 0 means no current version; all historical batches and PDFs remain.
            db.execute('UPDATE packing_lists SET current_batch_id=0,revision=revision+1 WHERE packing_list_id=?',
                       (package['packing_list_key'],))
            db.execute("DELETE FROM fulfillment_documents WHERE kind='packing_list' AND document_id=?", (package['batch_id'],))
            packing_versions.stage_evidence(b, db, members)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        packing_versions.sync_evidence(b, members)
        return 'Lista została wycofana z bieżącego pakowania. Zachowano ją w historii. Możesz przygotować poprawną listę.'

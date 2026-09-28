"""Standard-library fixtures: SQLite persistence plus a fake Supabase boundary."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

import invoice_payment_sync as sync


class Backend:
    def __init__(self,path):
        self.path = path
        self.enabled = True
        self.remote = {'invoice_meta':{},'orders':{}}
        self.calls = []
        self.missing_schema = False
        self.drop_paid = False
        self.after_patch = None

    def conn(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def supabase_enabled(self):
        return self.enabled

    def supabase_compatible_rows(self,table,rows):
        return copy.deepcopy(rows)

    def supabase_select_rows(self,table,order_by='id',page_size=1000,extra_params=None):
        self.calls.append(('GET',table))
        if self.missing_schema:
            raise RuntimeError('PGRST204 column paid unavailable in schema cache')
        key = int(extra_params[sync.TABLE_KEYS[table]].split('.')[1])
        row = self.remote[table].get(key)
        return [copy.deepcopy(row)] if row else []

    def supabase_upsert_rows(self,table,rows,on_conflict):
        self.calls.append(('POST',table))
        for row in rows:
            self.remote[table][row[on_conflict]] = copy.deepcopy(row)

    def supabase_request(self,path,method,params,payload):
        table = path.rsplit('/',1)[-1]
        key = int(params[sync.TABLE_KEYS[table]].split('.')[1])
        self.calls.append((method,table))
        values = copy.deepcopy(payload)
        if self.drop_paid:
            values.pop('paid',None)
        self.remote[table][key].update(values)
        callback,self.after_patch = self.after_patch,None
        if callback:
            callback()


class PaymentSyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='payment-outbox-')
        self.addCleanup(self.directory.cleanup)
        self.backend = Backend(str(Path(self.directory.name)/'payments.db'))
        db = self.backend.conn()
        db.executescript('''
            CREATE TABLE invoice_meta(invoice_id INTEGER PRIMARY KEY,paid INTEGER,paid_at TEXT,
                payment_reminder INTEGER,seen_by_client INTEGER,seen_at TEXT,updated_at TEXT,pdf_path TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY,status TEXT,tracking_no TEXT,customer_name TEXT);
            CREATE TABLE invoices(id INTEGER PRIMARY KEY,order_id INTEGER);
            INSERT INTO invoice_meta VALUES(1,0,NULL,1,0,NULL,'2026-09-01 10:00:00','local.pdf');
            INSERT INTO orders VALUES(10,'packed','TRACK','Customer');
            INSERT INTO invoices VALUES(1,10);
        ''')
        sync.initialize(db)
        db.commit()
        for table,key in sync.TABLE_KEYS.items():
            self.backend.remote[table] = {row[key]:dict(row) for row in db.execute('SELECT * FROM '+table)}
        db.close()

    def write(self,paid=1,invoice_id=1):
        db = self.backend.conn()
        db.execute('BEGIN IMMEDIATE')
        db.execute('''UPDATE invoice_meta SET paid=?,paid_at=?,payment_reminder=0,seen_by_client=1,
            seen_at='2026-09-28 10:00:00',updated_at=? WHERE invoice_id=?''',
            (paid,'2026-09-28 10:00:00' if paid else None,
             '2026-09-28 10:00:0'+str(paid),invoice_id))
        db.execute('UPDATE orders SET status=? WHERE id=10',('completed' if paid else 'packed',))
        sync.stage(db,invoice_id,[10])
        db.commit()
        db.close()

    def test_business_rollback_also_rolls_back_outbox(self):
        db = self.backend.conn()
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE invoice_meta SET paid=1 WHERE invoice_id=1')
        sync.stage(db,1,[10])
        db.rollback()
        self.assertEqual(db.execute('SELECT paid FROM invoice_meta').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM invoice_payment_sync_outbox').fetchone()[0],0)
        db.close()

    def test_schema_failure_keeps_pending_and_protects_pull_and_delete(self):
        self.write()
        self.backend.missing_schema = True
        outcome = sync.flush_pending(self.backend,1)
        self.assertEqual(outcome['sync_status'],'ERROR')
        self.assertIn('REMOTE_SCHEMA_INCOMPATIBLE',outcome['error_codes'])
        self.assertFalse(any(method in {'POST','PATCH'} for method,_ in self.backend.calls))
        db = self.backend.conn()
        db.execute('BEGIN IMMEDIATE')
        incoming = {**self.backend.remote['invoice_meta'][1],'pdf_path':'new-remote.pdf'}
        protected = sync.protect_incoming(db,'invoice_meta',[incoming])[0]
        self.assertEqual(protected['paid'],1)
        self.assertEqual(protected['payment_reminder'],0)
        self.assertEqual(protected['pdf_path'],'new-remote.pdf')
        self.assertEqual(sync.protect_incoming(db,'orders',[self.backend.remote['orders'][10]])[0]['status'],'completed')
        self.assertEqual(sync.protected_keys(db,'invoice_meta'),{'1'})
        self.assertEqual(sync.protected_keys(db,'invoices'),{'1'})
        self.assertEqual(sync.protected_keys(db,'orders'),{'10'})
        db.close()

    def test_verified_patch_preserves_unrelated_remote_fields(self):
        self.write()
        self.backend.remote['invoice_meta'][1]['pdf_path'] = 'fresher-cloud.pdf'
        self.backend.remote['orders'][10]['tracking_no'] = 'NEW-TRACK'
        outcome = sync.flush_pending(self.backend,1)
        self.assertEqual(outcome['sync_status'],'SYNCED')
        self.assertTrue(outcome['cloud_synced'])
        self.assertEqual(self.backend.remote['invoice_meta'][1]['paid'],1)
        self.assertEqual(self.backend.remote['invoice_meta'][1]['pdf_path'],'fresher-cloud.pdf')
        self.assertEqual(self.backend.remote['orders'][10]['tracking_no'],'NEW-TRACK')
        self.assertEqual(self.backend.remote['orders'][10]['status'],'completed')

    def test_silent_remote_field_drop_is_not_acknowledged(self):
        self.write()
        self.backend.drop_paid = True
        outcome = sync.flush_pending(self.backend,1)
        self.assertFalse(outcome['cloud_synced'])
        self.assertIn('REMOTE_WRITE_UNVERIFIED',outcome['error_codes'])
        self.assertEqual(self.backend.remote['invoice_meta'][1]['paid'],0)

    def test_postgrest_timestamp_serialization_keeps_verified_ack(self):
        self.write()
        def serialize_timestamps():
            row = self.backend.remote['invoice_meta'][1]
            for field in ('paid_at','seen_at','updated_at'):
                row[field] = row[field].replace(' ','T')+'+00:00'
        self.backend.after_patch = serialize_timestamps
        self.assertEqual(sync.flush_pending(self.backend,1)['sync_status'],'SYNCED')

    def test_old_acknowledgement_cannot_clear_new_revision(self):
        self.write(1)
        self.backend.after_patch = lambda:self.write(0)
        first = sync.flush_pending(self.backend,1)
        self.assertTrue(first['pending'])
        db = self.backend.conn()
        row = db.execute("SELECT revision,state,payload FROM invoice_payment_sync_outbox WHERE table_name='invoice_meta'").fetchone()
        self.assertEqual(row['revision'],2)
        self.assertEqual(row['state'],'PENDING')
        db.close()
        second = sync.flush_pending(self.backend,1)
        self.assertEqual(second['sync_status'],'SYNCED')
        self.assertEqual(self.backend.remote['invoice_meta'][1]['paid'],0)
        self.assertEqual(self.backend.remote['orders'][10]['status'],'packed')

    def test_stale_claim_does_not_publish_when_newer_local_revision_already_exists(self):
        self.write(1)
        claim = sync._claim(self.backend,'invoice_meta',1)
        self.write(0)
        with self.assertRaisesRegex(sync.SyncVerificationError,'REVISION_SUPERSEDED'):
            sync._publish(self.backend,claim)
        sync._complete_claim(self.backend,claim,'REVISION_SUPERSEDED')
        self.assertFalse(any(method in {'POST','PATCH'} for method,_ in self.backend.calls))
        self.assertTrue(sync.status(self.backend,1)['pending'])
        self.assertEqual(sync.flush_pending(self.backend,1)['sync_status'],'SYNCED')

    def test_restart_resumes_durable_pending_without_resending_mail(self):
        self.write()
        restored = Backend(self.backend.path)
        restored.remote = self.backend.remote
        outcome = sync.flush_pending(restored,1)
        self.assertEqual(outcome['sync_status'],'SYNCED')
        self.assertEqual(restored.remote['invoice_meta'][1]['paid'],1)

    def test_shared_order_uses_latest_revision_across_invoice_jobs(self):
        self.write(1)
        db = self.backend.conn()
        db.execute("INSERT INTO invoice_meta VALUES(2,0,NULL,0,0,NULL,'2026-09-01','two.pdf')")
        db.execute('INSERT INTO invoices VALUES(2,10)')
        db.commit()
        db.close()
        self.write(0,invoice_id=2)
        sync.flush_pending(self.backend,1)
        self.assertEqual(self.backend.remote['orders'][10]['status'],'packed')
        sync.flush_pending(self.backend,2)
        self.assertEqual(sync.status(self.backend,2)['sync_status'],'SYNCED')

    def test_disabled_cloud_is_explicit_local_only(self):
        self.backend.enabled = False
        self.write()
        self.assertEqual(sync.flush_pending(self.backend,1)['sync_status'],'LOCAL_ONLY')
        result = sync.status(self.backend,1)
        self.assertTrue(result['local_saved'])
        self.assertIsNone(result['cloud_synced'])
        self.assertEqual(self.backend.calls,[])

    def test_claim_serializes_same_record_and_requires_transaction_to_stage(self):
        db = self.backend.conn()
        with self.assertRaises(ValueError):
            sync.stage(db,1,[10])
        db.close()
        self.write()
        claim = sync._claim(self.backend,'invoice_meta',1)
        self.assertIsNotNone(claim)
        self.assertIsNone(sync._claim(self.backend,'invoice_meta',1))
        sync._complete_claim(self.backend,claim,'REMOTE_SYNC_FAILED')
        self.assertIsNotNone(sync._claim(self.backend,'invoice_meta',1))


if __name__ == '__main__':
    unittest.main()

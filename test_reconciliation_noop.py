"""Offline contract tests: real SQLite, controlled cloud/CAS, no app or network."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import reconciliation_store as store


class ReconciliationNoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='sqlite-', dir=Path(__file__).parent)
        self.db_path = Path(self.tmp.name) / 'test.db'
        c = self.conn()
        c.executescript('''
            CREATE TABLE fulfillment_reconciliation_versions(order_id INTEGER PRIMARY KEY,revision INTEGER NOT NULL);
            CREATE TABLE fulfillment_reconciliation_pending(order_id INTEGER PRIMARY KEY,expected_revision INTEGER NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE packing_batches(id INTEGER PRIMARY KEY,root_order_id INTEGER,created_at TEXT,packing_list_id TEXT);
            CREATE TABLE packing_allocations(id INTEGER PRIMARY KEY,batch_id INTEGER,order_id INTEGER,order_item_id INTEGER,qty INTEGER,created_at TEXT);
        ''')
        c.close()
        self.payload = {
            'packing_batches': [{'id': 6, 'root_order_id': 136, 'created_at': '2026-10-05 18:52:02', 'packing_list_id': 'list-key'}],
            'packing_allocations': [
                {'id': 1, 'batch_id': 6, 'order_id': 136, 'order_item_id': 522, 'qty': 2, 'created_at': '2026-10-05'},
                {'id': 2, 'batch_id': 6, 'order_id': 122, 'order_item_id': 499, 'qty': 2, 'created_at': '2026-10-05'},
            ],
            'packing_lists': [{'packing_list_id': 'list-key', 'current_batch_id': 6, 'invoice_id': 101}],
            'packing_shipments': [{'shipment_key': 'external:saved', 'final_batch_id': 6, 'tracking': 'saved-tracking'}],
            'fulfillment_documents': [{'order_id': 136, 'kind': 'packing_list', 'document_id': 6, 'file_hash': 'saved-hash', 'pdf_base64': 'JVBERi0x'}],
            'fulfillment_document_history': [],
            'fulfillment_verifications': [{'order_id': 136, 'kind': 'proof', 'payload': '{"confirmed":true}'}],
            'inpost_notifications': [{'shipment_id': 'saved', 'state': 'unknown', 'updated_at': '2026-10-06 12:51:00'}],
        }
        self.remote = {'revision': 7, 'payload': copy.deepcopy(self.payload)}
        self.calls = []
        self.on_read = None
        self.read_error = None
        self.backend = SimpleNamespace(conn=self.conn, supabase_request=self.request,
            supabase_enabled=lambda: True, app=SimpleNamespace(logger=Mock()))
        self.queue(self.payload, 7)

    def tearDown(self):
        self.tmp.cleanup()

    def conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def queue(self, payload, revision):
        c = self.conn()
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_pending VALUES(136,?,?)', (revision, json.dumps(payload)))
        c.execute('INSERT OR REPLACE INTO fulfillment_reconciliation_versions VALUES(136,?)', (revision,))
        c.commit()
        c.close()

    def pending(self):
        c = self.conn()
        row = c.execute('SELECT expected_revision,payload FROM fulfillment_reconciliation_pending WHERE order_id=136').fetchone()
        c.close()
        return (row[0], json.loads(row[1])) if row else None

    def version(self):
        c = self.conn()
        revision = c.execute('SELECT revision FROM fulfillment_reconciliation_versions WHERE order_id=136').fetchone()[0]
        c.close()
        return revision

    def request(self, path, method='GET', params=None, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == 'GET':
            self.assertEqual(path, '/rest/v1/fulfillment_reconciliation')
            self.assertEqual(params, {'order_id': 'eq.136', 'select': 'revision,payload'})
            if self.read_error:
                raise self.read_error
            result = [] if self.remote is None else [copy.deepcopy(self.remote)]
            if self.on_read:
                self.on_read()
            return result
        self.assertEqual((method, path), ('POST', '/rest/v1/rpc/save_fulfillment_reconciliation'))
        self.assertEqual(payload['p_order_id'], 136)
        actual = self.remote['revision'] if self.remote else 0
        if actual != payload['p_expected_revision']:
            return {'saved': False, 'revision': actual}
        self.remote = {'revision': actual + 1, 'payload': copy.deepcopy(payload['p_payload'])}
        return {'saved': True, 'revision': actual + 1}

    def saves(self):
        return [call for call in self.calls if call[0] == 'POST']

    def test_publish_acknowledges_identical_pending_without_writing_cloud(self):
        store.publish(self.backend, 136)
        self.assertEqual(self.saves(), [])
        self.assertIsNone(self.pending())
        self.assertEqual(self.version(), 7)
        self.assertEqual(self.remote['revision'], 7)
        self.backend.app.logger.info.assert_called_once_with(
            'RECONCILIATION_SAVE_SKIPPED order_id=%s revision=%s skipped=true', 136, 7)

    def test_equal_payload_after_lost_ack_uses_confirmed_newer_revision(self):
        self.remote['revision'] = 9
        store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(self.saves(), [])
        self.assertIsNone(self.pending())
        self.assertEqual(self.version(), 9)

    def test_row_and_object_key_order_do_not_create_a_write(self):
        self.remote['payload']['packing_allocations'].reverse()
        self.remote['payload'] = dict(reversed(list(self.remote['payload'].items())))
        store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(self.saves(), [])

    def test_every_real_evidence_change_still_requires_the_original_cas(self):
        mutations = [
            lambda p: p['packing_allocations'][0].update(qty=3),
            lambda p: p['packing_lists'][0].update(invoice_id=102),
            lambda p: p['packing_shipments'][0].update(tracking='different'),
            lambda p: p['fulfillment_verifications'][0].update(payload='{"confirmed":false}'),
            lambda p: p['fulfillment_documents'][0].update(pdf_base64='JVBERi0y'),
            lambda p: p['fulfillment_documents'][0].update(file_hash='different'),
            lambda p: p['inpost_notifications'][0].update(state='accepted'),
            lambda p: p['packing_allocations'].append(copy.deepcopy(p['packing_allocations'][0])),
            lambda p: p.pop('fulfillment_document_history'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.calls.clear()
                self.remote = {'revision': 7, 'payload': copy.deepcopy(self.payload)}
                changed = copy.deepcopy(self.payload)
                mutate(changed)
                self.queue(changed, 7)
                store._save(self.backend, 136, 7, changed)
                self.assertEqual(len(self.saves()), 1)
                sent = self.saves()[0][2]
                self.assertEqual(sent['p_expected_revision'], 7)
                self.assertEqual(sent['p_payload'], changed)
                self.assertEqual(self.remote, {'revision': 8, 'payload': changed})
                self.assertIsNone(self.pending())

    def test_different_newer_cloud_payload_remains_a_conflict(self):
        self.remote['revision'] = 8
        self.remote['payload']['packing_lists'][0]['invoice_id'] = 102
        with self.assertRaises(ValueError):
            store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(len(self.saves()), 1)
        self.assertEqual(self.pending(), (7, self.payload))
        self.assertEqual(self.version(), 7)
        self.assertEqual(self.remote['payload']['packing_lists'][0]['invoice_id'], 102)

    def test_old_remote_revision_cannot_acknowledge_identical_payload(self):
        self.remote['revision'] = 6
        with self.assertRaises(ValueError):
            store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(len(self.saves()), 1)
        self.assertEqual(self.pending(), (7, self.payload))

    def test_missing_remote_record_never_acknowledges_old_pending(self):
        self.remote = None
        with self.assertRaises(ValueError):
            store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(len(self.saves()), 1)
        self.assertEqual(self.pending(), (7, self.payload))

    def test_read_failure_falls_back_to_cas_without_fabricating_ack(self):
        self.read_error = TimeoutError('offline read failure')
        self.remote['revision'] = 8
        with self.assertRaises(ValueError):
            store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(len(self.saves()), 1)
        self.assertEqual(self.pending(), (7, self.payload))
        self.assertEqual(self.version(), 7)

    def test_missing_record_for_new_evidence_still_uses_cas(self):
        self.remote = None
        self.queue(self.payload, 0)
        store._save(self.backend, 136, 0, self.payload)
        self.assertEqual(len(self.saves()), 1)
        self.assertEqual(self.remote, {'revision': 1, 'payload': self.payload})
        self.assertIsNone(self.pending())

    def test_unusable_remote_read_cannot_bypass_cas_failure(self):
        for response in (None, {}, [], [{'revision': True, 'payload': self.payload}],
                         [{'revision': 7, 'payload': None}]):
            with self.subTest(response_type=type(response).__name__):
                self.queue(self.payload, 7)
                attempted = []
                def unavailable(path, method='GET', **kwargs):
                    if method == 'GET':
                        return response
                    attempted.append(kwargs['payload'])
                    raise TimeoutError('offline CAS failure')
                self.backend.supabase_request = unavailable
                with self.assertRaises(TimeoutError):
                    store._save(self.backend, 136, 7, self.payload)
                self.assertEqual(len(attempted), 1)
                self.assertEqual(self.pending(), (7, self.payload))
                self.assertEqual(self.version(), 7)

    def test_ack_does_not_remove_changed_pending_payload_or_revision(self):
        for newer_revision in (7, 8):
            with self.subTest(newer_revision=newer_revision):
                self.calls.clear()
                changed = copy.deepcopy(self.payload)
                changed['packing_lists'][0]['invoice_id'] = 102
                self.queue(self.payload, 7)
                self.on_read = lambda: self.queue(changed, newer_revision)
                store._save(self.backend, 136, 7, self.payload)
                self.assertEqual(self.pending(), (newer_revision, changed))
                self.assertEqual(self.version(), max(7, newer_revision))
                self.assertEqual(self.saves(), [])

    def test_concurrent_later_cloud_write_is_never_overwritten(self):
        later = copy.deepcopy(self.payload)
        later['inpost_notifications'][0]['state'] = 'accepted'
        self.on_read = lambda: setattr(self, 'remote', {'revision': 8, 'payload': later})
        store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(self.saves(), [])
        self.assertEqual(self.remote, {'revision': 8, 'payload': later})
        self.assertEqual(self.version(), 7)

    def test_identical_newer_pending_revision_is_not_deleted(self):
        self.on_read = lambda: self.queue(self.payload, 8)
        store._save(self.backend, 136, 7, self.payload)
        self.assertEqual(self.pending(), (8, self.payload))
        self.assertEqual(self.version(), 8)
        self.assertEqual(self.saves(), [])

    def test_existing_id_collision_mapping_preserves_full_evidence(self):
        c = self.conn()
        c.executescript('''
            INSERT INTO packing_batches VALUES(6,999,'unrelated','other');
            INSERT INTO packing_batches VALUES(11,136,'2026-10-05 18:52:02','list-key');
            INSERT INTO packing_allocations VALUES(1,6,999,999,1,'unrelated');
            INSERT INTO packing_allocations VALUES(2,6,999,998,1,'unrelated');
            INSERT INTO packing_allocations VALUES(21,11,136,522,2,'2026-10-05');
            INSERT INTO packing_allocations VALUES(22,11,122,499,2,'2026-10-05');
        ''')
        c.close()
        local = copy.deepcopy(self.payload)
        local['packing_batches'][0]['id'] = 11
        for i, row in enumerate(local['packing_allocations']):
            row.update(id=21+i, batch_id=11)
        local['packing_lists'][0]['current_batch_id'] = 11
        local['packing_shipments'][0]['final_batch_id'] = 11
        local['fulfillment_documents'][0]['document_id'] = 11
        self.queue(local, 7)
        store._save(self.backend, 136, 7, local)
        self.assertEqual(self.saves(), [])
        self.assertIsNone(self.pending())
        self.assertEqual(self.remote['payload'], self.payload)
        c = self.conn()
        # The comparison must not persist its speculative identity mapping.
        self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='fulfillment_packing_id_map'").fetchone())
        c.close()

        for section, field, value in [('packing_lists','invoice_id',102),
                                       ('packing_allocations','qty',3)]:
            with self.subTest(mapped_change=field):
                self.calls.clear()
                self.remote = {'revision':7, 'payload':copy.deepcopy(self.payload)}
                changed = copy.deepcopy(local)
                changed[section][0][field] = value
                self.queue(changed, 7)
                store._save(self.backend, 136, 7, changed)
                self.assertEqual(len(self.saves()), 1)
                self.assertEqual(self.saves()[0][2]['p_expected_revision'], 7)
                self.assertEqual(self.saves()[0][2]['p_payload'], changed)


if __name__ == '__main__':
    unittest.main(verbosity=2)

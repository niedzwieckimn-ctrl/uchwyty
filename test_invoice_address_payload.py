"""Exercise the real helper AST without importing app or touching its database."""
import ast
from datetime import datetime
from pathlib import Path
import unittest

SOURCE = Path(__file__).parent / 'app.py'
tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == 'invoice_meta_payload')
namespace = {'app_now': lambda: datetime(2026, 10, 6), 'load_ksef_doc': lambda _: {}}
exec(compile(ast.Module(body=[helper], type_ignores=[]), str(SOURCE), 'exec'), namespace)
invoice_meta_payload = namespace['invoice_meta_payload']


class InvoiceAddressPayloadTests(unittest.TestCase):
    def test_saved_buyer_address_reaches_pdf_generator_fields(self):
        row = {'id': 101, 'invoice_no': 'FVAT 2/10/2026',
               'buyer_street': 'Przykladowa 12',
               'buyer_post_code': '00-001', 'buyer_city': 'Warszawa, Polska'}
        original = dict(row)
        payload = invoice_meta_payload(row)
        for key in ('buyer_street', 'buyer_post_code', 'buyer_city'):
            self.assertEqual(payload[key], row[key])
        self.assertEqual(payload['buyer_address'], 'Przykladowa 12\n00-001 Warszawa, Polska')
        self.assertEqual(payload['invoice_no'], 'FVAT 2/10/2026')
        self.assertEqual(row, original)

    def test_missing_address_fields_are_empty_strings(self):
        payload = invoice_meta_payload({'buyer_street': None, 'buyer_city': ''})
        for key in ('buyer_street', 'buyer_post_code', 'buyer_city', 'buyer_address'):
            self.assertEqual(payload[key], '')


if __name__ == '__main__':
    unittest.main(verbosity=2)


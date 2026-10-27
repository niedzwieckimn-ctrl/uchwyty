from decimal import Decimal
import unittest

from architect_pricing import ProgramRules, price_line


RULES = ProgramRules(1, Decimal('0.10'), Decimal('0.50'), Decimal('0.07'))


class ArchitectPricingTests(unittest.TestCase):
    def test_user_example(self):
        line = price_line(wholesale_unit_net='1121.39', retail_unit_gross='2000',
                          vat_rate='0.23', qty=1, rules=RULES)
        self.assertEqual(line.retail_unit_gross_after, Decimal('1800.00'))
        self.assertEqual(line.retail_unit_net_after, Decimal('1463.41'))
        self.assertEqual(line.manufacturer_discount_unit_net, Decimal('81.30'))
        self.assertEqual(line.wholesale_unit_after, Decimal('1040.09'))
        self.assertEqual(line.commission_estimate, Decimal('102.44'))

    def test_quantity_uses_unit_snapshot_and_commission_scales(self):
        line = price_line(wholesale_unit_net='1121.39', retail_unit_gross='2000',
                          vat_rate='0.23', qty=3, rules=RULES)
        self.assertEqual(line.wholesale_net_after, Decimal('3120.27'))
        self.assertEqual(line.commission_estimate, Decimal('307.32'))

    def test_negative_wholesale_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'manual review'):
            price_line(wholesale_unit_net='1', retail_unit_gross='2000',
                       vat_rate='0.23', qty=1, rules=RULES)

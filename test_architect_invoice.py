from decimal import Decimal

import pytest

from architect_invoice import expected_promo_invoice_totals


ITEM = {
    "source_order_id": 11, "order_item_id": 27, "qty": 2, "ordered_qty": 3,
    "vat_rate": 23, "unit_net_price": 1040.09, "net_price": 1040.09,
    "line_value_net": 2080.18,
}
SNAPSHOT = {
    "order_item_id": 27, "qty": 3, "vat_rate": "0.23",
    "wholesale_net_after": "3120.27",
}


def test_partial_invoice_uses_order_snapshot():
    assert expected_promo_invoice_totals(
        [ITEM], [SNAPSHOT], 11, invoice_type="domestic", currency="PLN",
        discount_percent="0",
    ) == (Decimal("2080.18"), Decimal("2558.62"))


@pytest.mark.parametrize("change", [
    {"net_price": 1121.39}, {"line_value_net": 2242.78},
    {"source_order_id": 12}, {"qty": 4}, {"vat_rate": 0},
])
def test_price_quantity_and_origin_drift_are_blocked(change):
    with pytest.raises(ValueError):
        expected_promo_invoice_totals(
            [{**ITEM, **change}], [SNAPSHOT], 11,
            invoice_type="domestic", currency="PLN", discount_percent="0",
        )


def test_second_discount_is_blocked():
    with pytest.raises(ValueError):
        expected_promo_invoice_totals(
            [ITEM], [SNAPSHOT], 11, invoice_type="domestic", currency="PLN",
            discount_percent="10",
        )

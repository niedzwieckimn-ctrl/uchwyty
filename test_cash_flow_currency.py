import json

from cash_flow_module import EUR_TO_PLN_CASH_FLOW_RATE, invoice_cash_flow_context


def test_eur_invoice_uses_fixed_cash_flow_rate():
    items, currency, rate = invoice_cash_flow_context(
        "PLN", json.dumps([{"currency": "EUR", "qty": 2}])
    )
    assert items[0]["qty"] == 2
    assert currency == "EUR"
    assert rate == EUR_TO_PLN_CASH_FLOW_RATE == 4.30


def test_order_currency_is_used_for_older_invoice_without_item_currency():
    _items, currency, rate = invoice_cash_flow_context("EUR", "[]")
    assert currency == "EUR"
    assert rate == 4.30


def test_pln_invoice_is_not_converted():
    _items, currency, rate = invoice_cash_flow_context("PLN", "not-json")
    assert currency == "PLN"
    assert rate == 1.0

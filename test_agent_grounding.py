import logging

import pytest

import agent_runtime as runtime


def allowed(*, user="", tool=None):
    facts = runtime._text_grounding_facts(user)
    if tool is not None:
        facts.merge(runtime._payload_grounding_facts(tool))
    return facts


def assert_grounded(answer, facts):
    _observed, missing = runtime._missing_grounding(answer, facts)
    assert not missing.numeric_values
    assert not missing.dates
    assert not missing.identifiers


def assert_rejected(answer, facts, *, missing_number):
    _observed, missing = runtime._missing_grounding(answer, facts)
    assert missing_number in missing.numeric_values


def test_a_amount_in_polish_format_is_equivalent():
    assert_grounded("2 040,91 PLN", allowed(tool={"amount": 2040.91}))


@pytest.mark.parametrize("rendered", ["01.09.2026", "1.09.2026", "01/09/2026", "1/09/2026"])
def test_b_date_formats_are_canonicalized(rendered):
    assert_grounded(rendered, allowed(tool={"issue_date": "2026-09-01"}))


def test_c_invoice_number_is_one_identifier_not_three_numbers():
    facts = allowed(tool={"invoice_number": "FVAT 1/09/2026"})
    observed, missing = runtime._missing_grounding("FVAT 1/09/2026", facts)
    assert observed.numeric_values == set()
    assert_grounded("FVAT 1/09/2026", facts)


@pytest.mark.parametrize("identifier", ["CH101-BLK", "ORD-1234", "PO-2026-091", "SKU-160-AB"])
def test_d_e_identifiers_are_compared_as_complete_tokens(identifier):
    assert_grounded(identifier, allowed(tool={"sku": identifier}))


def test_f_hallucinated_stock_is_rejected():
    assert_rejected("Na magazynie jest 47 szt.", allowed(tool={"stock": 42}), missing_number="47")


def test_g_grounding_does_not_calculate_difference():
    assert_rejected("Pozostało 935", allowed(tool={"total": 1055, "planned": 120}), missing_number="935")


def test_h_number_from_current_user_message_is_allowed():
    assert_grounded("Sprawdzam zamówienie 1234", allowed(user="sprawdź zamówienie 1234"))


def invoice_results():
    return {"invoices": [
        {"invoice_number": "FVAT 1/09/2026", "amount": 2040.91, "currency": "PLN"},
        {"invoice_number": "FVAT 2/09/2026", "amount": 757.78, "currency": "PLN"},
    ]}


def test_i_two_invoices_and_naturally_formatted_amounts_pass():
    assert_grounded(
        "FVAT 1/09/2026 — 2 040,91 PLN\nFVAT 2/09/2026 — 757,78 PLN",
        allowed(tool=invoice_results()),
    )


def test_j_hallucinated_sum_fails_unless_tool_returns_exact_total():
    assert_rejected("Łącznie 3000 PLN", allowed(tool=invoice_results()), missing_number="3000")
    with_total = invoice_results()
    with_total["total"] = 3000
    assert_grounded("Łącznie 3000 PLN", allowed(tool=with_total))


def test_ambiguous_short_year_date_is_not_interpreted_as_a_date():
    facts = runtime._text_grounding_facts("01/02/03")
    assert facts.dates == set()


def test_runtime_rejection_logs_safe_structured_diagnostics(monkeypatch, caplog):
    class Actor:
        pass

    # The full runtime path and HTTP mapping are covered in test_agent_runtime;
    # this assertion keeps the diagnostic contract focused and secret-free.
    metadata = runtime._identifier_diagnostics({"FVAT SECRET/123"})
    assert metadata[0]["type"] == "identifier"
    assert metadata[0]["length"] == len("FVAT SECRET/123")
    assert len(metadata[0]["sha256_prefix"]) == 12
    assert "FVAT" not in str(metadata)


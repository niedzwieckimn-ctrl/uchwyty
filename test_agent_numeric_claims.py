import pytest

import agent_runtime as runtime


def facts(tool):
    return runtime._payload_grounding_facts(tool)


def claims(*items):
    return runtime._validate_numeric_claims(list(items))


def assert_valid(message, declared, allowed):
    normalized = claims(*declared)
    assert runtime._rejected_numeric_claims(normalized, allowed) == []
    assert runtime._undeclared_business_mentions(message, normalized) == []


def test_a_invoice_identifier_without_business_numeric_needs_no_claim():
    assert_valid("FVAT 1/09/2026", [], facts({"invoice_number": "FVAT 1/09/2026"}))


def test_b_c_invoice_money_claim_passes_without_date_digits_becoming_claims():
    allowed = facts({"invoice_number": "FVAT 1/09/2026", "gross": 2040.91, "currency": "PLN"})
    declared = [{"kind": "money", "value": "2040.910", "currency": "PLN"}]
    assert_valid("FVAT 1/09/2026 ma wartość 2 040,91 PLN.", declared, allowed)
    assert [claim["value"] for claim in claims(*declared)] == ["2040.91"]


@pytest.mark.parametrize(("message", "identifier", "kind", "value"), [
    ("CH034-BB-128160 — 17 szt.", "CH034-BB-128160", "stock", 17),
    ("ORD-1234 — 4 pozycje", "ORD-1234", "count", 4),
])
def test_d_e_identifiers_do_not_become_claims_but_business_values_do(message, identifier, kind, value):
    assert_valid(message, [{"kind": kind, "value": value}], facts({"identifier": identifier, "value": value}))


@pytest.mark.parametrize(("tool,claim_value"), [({"stock": 17}, 18), ({"first": 1055, "second": 120}, 935)])
def test_f_g_ungrounded_claims_are_rejected(tool, claim_value):
    rejected = runtime._rejected_numeric_claims(
        claims({"kind": "other_business_numeric", "value": claim_value}), facts(tool),
    )
    assert rejected[0]["normalized_value"] == str(claim_value)


def test_h_message_business_value_without_claim_is_declaration_mismatch():
    assert runtime._undeclared_business_mentions("Na stanie jest 17 szt.", []) == [{"value": "17", "currency": ""}]


def test_i_identifier_without_claim_has_no_declaration_mismatch():
    assert runtime._undeclared_business_mentions("FVAT 1/09/2026", []) == []


def test_j_k_days_must_be_declared():
    declared = claims({"kind": "days", "value": 2})
    assert runtime._undeclared_business_mentions("2 dni po terminie", declared) == []
    assert runtime._undeclared_business_mentions("2 dni po terminie", []) == [{"value": "2", "currency": ""}]


def test_l_currency_is_checked_when_tool_result_exposes_currency():
    allowed = facts({"rows": [
        {"amount": 100, "currency": "PLN"}, {"amount": 200, "currency": "EUR"},
    ]})
    assert runtime._rejected_numeric_claims(claims({"kind": "money", "value": 100, "currency": "PLN"}), allowed) == []
    rejected = runtime._rejected_numeric_claims(claims({"kind": "money", "value": 100, "currency": "EUR"}), allowed)
    assert rejected == [{"kind": "money", "normalized_value": "100", "currency": "EUR"}]


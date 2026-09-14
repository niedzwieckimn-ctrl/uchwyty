"""Compact finance state composed from existing controlled finance reads."""

from __future__ import annotations


def _payment(item: dict, *, overdue: bool) -> dict:
    return {
        "entity_type": "invoice",
        "entity_id": int(item["id"]),
        "human_label": str(item.get("invoice_number") or item.get("invoice_no") or "").strip(),
        "customer_name": str(item.get("buyer_name") or "").strip(),
        "amount_outstanding": round(float(item.get("amount_outstanding", item.get("total_gross")) or 0), 2),
        "currency": str(item.get("currency") or "PLN").upper(),
        "due_date": str(item.get("due_date") or item.get("payment_to") or "").strip(),
        "overdue_days": int(item.get("overdue_days") or 0),
        "action_required": (
            "Skontaktuj się z klientem w sprawie zaległej płatności."
            if overdue else "Monitoruj termin bieżącej należności."
        ),
        "urgency": "high" if overdue else "low",
    }


def compose_finance_operational_state(
    overdue_result: dict, unpaid_result: dict, sales_result: dict,
) -> dict:
    overdue = [_payment(item, overdue=True) for item in overdue_result.get("results") or []]
    current = [
        _payment(item, overdue=False)
        for item in unpaid_result.get("results") or []
        if item.get("payment_status") == "unpaid"
    ]
    sales = {key: value for key, value in sales_result.items() if key != "ok"}
    return {
        "overdue_payments": overdue,
        "current_receivables": current,
        "sales_summary": sales,
    }

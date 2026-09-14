"""Application service composing the existing read-only operational states."""

from __future__ import annotations

from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from cash_flow_module import cash_flow_overdue_invoices
from deliveries_operational_state import build_deliveries_operational_state
from orders_operational_state import build_orders_operational_state


STATE_KEYS = (
    "ready_to_ship",
    "overdue_payments",
    "uncovered_order_shortages",
    "deliveries_requiring_attention",
    "other_urgent_exceptions",
)


def _text(value) -> str:
    return str(value or "").strip()


def _read_overdue(connection_factory: Callable, current_time: datetime):
    db = connection_factory()
    try:
        return cash_flow_overdue_invoices(db, current_time=current_time)
    finally:
        db.close()


def build_daily_operational_state(
    connection_factory: Callable, *, current_time: datetime | None = None,
) -> dict:
    """Compose only the existing business states that require action today."""
    now = current_time or datetime.now(ZoneInfo("Europe/Warsaw"))
    state = {key: [] for key in STATE_KEYS}
    orders = build_orders_operational_state(connection_factory, current_time=now)
    deliveries = build_deliveries_operational_state(connection_factory, current_time=now)
    overdue = _read_overdue(connection_factory, now)
    state["ready_to_ship"] = orders["ready_to_ship"]
    state["uncovered_order_shortages"] = [
        item
        for order in orders["blocked"]
        for item in order.get("missing_items") or []
        if not item["source_state"]["covered_by_stock_and_confirmed_incoming"]
    ]
    state["deliveries_requiring_attention"] = deliveries["requiring_attention"]

    for invoice in overdue:
        invoice_id = int(invoice["id"])
        currency = _text(invoice.get("currency") or "PLN").upper()
        customer_name = _text(invoice.get("buyer_name") or invoice.get("order_customer_name"))
        state["overdue_payments"].append({
            "entity_type": "invoice", "entity_id": invoice_id,
            "human_label": _text(invoice.get("invoice_no")),
            "customer_name": customer_name, "quantity": None,
            "amount_outstanding": round(float(invoice.get("total_gross") or 0), 2),
            "currency": currency,
            "action_required": "Skontaktuj się z klientem w sprawie zaległej płatności.",
            "urgency": "high",
            "source_state": {
                "payment_status": "overdue",
                "due_date": _text(invoice.get("payment_to")),
                "overdue_days": int(invoice.get("overdue_days") or 0),
            },
        })

    return state

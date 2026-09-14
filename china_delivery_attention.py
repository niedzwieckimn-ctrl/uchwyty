"""Shared, read-only presentation of China P/O attention states."""

from __future__ import annotations

from datetime import datetime


PROBLEM_TRACKING_STATUSES = frozenset({
    "deliveryfailure", "exception", "expired", "failure",
})


def _text(value) -> str:
    return str(value or "").strip()


def delivery_has_problem(package: dict) -> bool:
    """Return the existing `/china` problem predicate for one package."""
    return (
        _text(package.get("status")).lower() == "problem"
        or bool(_text(package.get("tracking_error")))
        or _text(package.get("tracking_status")).lower() in PROBLEM_TRACKING_STATUSES
    )


def delivery_attention_states(package: dict, *, current_time: datetime) -> list[dict]:
    """Return the existing `/china` alert states with presentation actions."""
    try:
        age_days = (
            current_time.date()
            - datetime.fromisoformat(_text(package.get("created_at")).replace("Z", "+00:00")).date()
        ).days
    except Exception:
        age_days = 0

    status = _text(package.get("status")).lower()
    tracking = _text(package.get("tracking"))
    states: list[dict] = []

    def add(code: str, label: str, action: str, urgency: str, metric: str | None = None) -> None:
        states.append({
            "code": code, "label": label, "action_required": action,
            "urgency": urgency, "metric": metric,
        })

    if not tracking and age_days > 5:
        add("missing_tracking", "Brak trackingu od ponad 5 dni",
            "Sprawdź i uzupełnij tracking dostawy.", "high", "missing_tracking")
    if status == "shipped" and not tracking:
        add("shipped_without_tracking", "Wysłana, ale bez trackingu",
            "Sprawdź i uzupełnij tracking wysłanej dostawy.", "high")
    if status == "shipped" and age_days > 20:
        add("long_transit", f"W drodze co najmniej {age_days} dni",
            "Sprawdź opóźnioną dostawę u przewoźnika lub dostawcy.", "high", "long_transit")
    if float(package.get("cost_amount") or 0) <= 0:
        add("missing_cost", "Brak kosztu", "Uzupełnij koszt dostawy.", "medium", "missing_cost")
    if tracking and _text(package.get("tracking_synced_at")):
        try:
            stale_days = (
                current_time.date()
                - datetime.fromisoformat(_text(package.get("tracking_synced_at"))[:10]).date()
            ).days
        except Exception:
            stale_days = 0
        if stale_days > 10:
            add("stale_tracking", f"Tracking bez aktualizacji od {stale_days} dni",
                "Sprawdź brak aktualizacji trackingu.", "high", "stale_tracking")
    if delivery_has_problem(package):
        add("tracking_problem", _text(package.get("tracking_error")) or "Problem trackingowy",
            "Wyjaśnij problem z dostawą lub trackingiem.", "high")
    return states

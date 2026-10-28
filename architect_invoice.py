"""Fail-closed reconciliation of a discounted order with its invoice lines."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")


def require_no_second_discount(value):
    try:
        discount = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Nieprawidłowa wartość rabatu") from exc
    if not discount.is_finite() or discount != 0:
        raise ValueError("Rabat architekta jest już zapisany w cenach pozycji zamówienia. Usuń drugi rabat z formularza.")


def amount(value):
    try:
        result = Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Brak poprawnej kwoty w zapisanej pozycji") from exc
    if not result.is_finite():
        raise ValueError("Nieprawidłowa kwota w zapisanej pozycji")
    return result


def expected_promo_invoice_totals(items, snapshots, order_id, *, invoice_type,
                                  currency, discount_percent):
    """Return the selected partial invoice total, or reject any price drift.

    `snapshots` are the immutable architect_sale_lines fetched from Supabase.
    The input rows come from the current IKUGO order and invoice selection.
    """
    require_no_second_discount(discount_percent)
    if invoice_type != "domestic" or currency != "PLN":
        raise ValueError("Faktura programu wymaga PLN, VAT krajowego i bez drugiego rabatu")
    if not items or not snapshots:
        raise ValueError("Brak pozycji programu do faktury")
    by_item = {int(row["order_item_id"]): row for row in snapshots}
    if len(by_item) != len(snapshots):
        raise ValueError("Powielone ceny zapisane w programie")
    selected = set()
    total_net = Decimal("0.00")
    for item in items:
        source_order = int(item.get("source_order_id") or item.get("order_id") or 0)
        item_id = int(item.get("order_item_id") or item.get("id") or 0)
        if source_order != int(order_id) or item_id in selected or item_id not in by_item:
            raise ValueError("Faktura programu nie może łączyć innych zamówień lub pozycji")
        selected.add(item_id)
        snapshot = by_item[item_id]
        qty = int(item.get("qty") or 0)
        ordered_qty = int(item.get("ordered_qty") or 0)
        if qty <= 0 or ordered_qty != int(snapshot["qty"]) or qty > ordered_qty:
            raise ValueError("Ilość faktury jest niezgodna z zamówieniem programu")
        if amount(snapshot["vat_rate"]) != Decimal("0.23") or int(item.get("vat_rate") or 0) != 23:
            raise ValueError("Stawka VAT pozycji programu wymaga weryfikacji")
        unit = amount(item.get("unit_net_price"))
        if unit <= 0 or amount(item.get("net_price")) != unit:
            raise ValueError("Cena faktury różni się od ceny zapisanej w zamówieniu")
        if amount(unit * ordered_qty) != amount(snapshot["wholesale_net_after"]):
            raise ValueError("Cena zamówienia różni się od zatwierdzonej migawki rabatu")
        selected_net = amount(unit * qty)
        if amount(item.get("line_value_net")) != selected_net:
            raise ValueError("Wartość pozycji faktury różni się od zamówienia")
        total_net += selected_net
    total_net = amount(total_net)
    return total_net, amount(total_net + amount(total_net * Decimal("0.23")))


def expected_combined_invoice_totals(items, snapshots_by_order, *, invoice_type,
                                     currency, discount_percent):
    """Reconcile each promoted order, then round VAT on the combined document.

    Ordinary lines keep the existing saved prices. Allocations, rather than
    the invoice's root order, identify which order each invoice line covers.
    """
    require_no_second_discount(discount_percent)
    if not items or not snapshots_by_order:
        raise ValueError("Brak pozycji programu do faktury")
    for order_id, snapshots in snapshots_by_order.items():
        selected = [item for item in items if
                    int(item.get('source_order_id') or item.get('order_id') or 0) == order_id]
        expected_promo_invoice_totals(selected, snapshots, order_id,
                                     invoice_type=invoice_type, currency=currency,
                                     discount_percent=discount_percent)
    total = amount(sum((amount(item['line_value_net']) for item in items), Decimal(0)))
    return total, amount(total + amount(total * Decimal('0.23')))

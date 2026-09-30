"""Explicit invoice review for marketplace imports; no invoice is issued here."""
from orderchamp_client import OrderchampError
from decimal import Decimal


def context_for_order(order,request_fn):
    if not str(order['order_no'] or '').startswith('OC-'): return None
    rows=request_fn('/rest/v1/orderchamp_order_links',params={
        'select':'payload','order_id':'eq.'+str(int(order['id'])),'limit':'1'},timeout=8)
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0].get('payload'),dict):
        raise OrderchampError('ORDERCHAMP_BILLING_CONTEXT_MISSING')
    return rows[0]['payload']


def reviewed_form(form,context):
    if form.get('oc_billing_reviewed')!='1': return False
    return (form.get('invoice_type') in ('domestic','wdt','export')
        and form.get('currency')==context['currency']
        and bool(str(form.get('buyer_name') or '').strip())
        and bool(str(form.get('buyer_country') or '').strip()))


def needs_price_review(context):
    # Legacy order_items columns store only numeric(12,2). Keep exact source totals
    # in the integration payload and stop invoice creation if rounding loses value.
    return any(Decimal(line[key])!=Decimal(line[key]).quantize(Decimal('0.01'))
        for line in context['items'] for key in ('unit_net','unit_gross'))


def saved_tax_context(order,invoice,fallback,buyer_tax_no=None,buyer_country=None):
    """Editing/regenerating an OC invoice keeps the explicitly reviewed tax type."""
    if str((order or {}).get('order_no') or '').startswith('OC-'):
        kind,currency=invoice.get('invoice_type'),invoice.get('currency')
        if kind not in ('domestic','wdt','export') or currency not in ('PLN','EUR'):
            raise OrderchampError('ORDERCHAMP_INVOICE_REVIEW_REQUIRED')
        return kind,currency,buyer_country or invoice.get('buyer_country') or ''
    return fallback(order or {},buyer_tax_no if buyer_tax_no is not None else invoice.get('buyer_tax_no'),
                    buyer_country if buyer_country is not None else invoice.get('buyer_country'))

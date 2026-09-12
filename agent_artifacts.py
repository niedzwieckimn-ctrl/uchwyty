"""Small trusted presentation mapping for successful Business Operation results."""

from __future__ import annotations

from typing import Any, Callable, Mapping


ARTIFACT_TYPES = frozenset({
    'invoice_card', 'order_card', 'product_card', 'china_order_card',
    'document_link', 'product_image',
})


def _clean(value):
    if value is None or value == '':
        return None
    return value


def _fields(source: Mapping[str, Any], names) -> dict[str, Any]:
    return {name: source[name] for name in names if name in source and _clean(source[name]) is not None}


def _items(record: Mapping[str, Any], names) -> list[dict[str, Any]]:
    rows = record.get('items')
    if not isinstance(rows, list):
        return []
    return [_fields(row, names) for row in rows[:20] if isinstance(row, Mapping)]


def _record(operation: str, result: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if operation == 'inventory.product.get' and result.get('id') is not None:
        return result
    record = result.get('record')
    if isinstance(record, Mapping):
        return record
    results = result.get('candidates') if operation == 'inventory.product.search' else result.get('results')
    if operation.endswith('.search') and result.get('count') == 1 and isinstance(results, list):
        candidate = results[0] if results else None
        return candidate if isinstance(candidate, Mapping) else None
    return None


def build_artifact_sources(
    operation: str,
    result: Mapping[str, Any],
    conversation_id: str,
    source_turn_id: str,
) -> list[dict[str, Any]]:
    """Build bounded evidence that a later model turn may select and re-read."""
    if not isinstance(result, Mapping) or result.get('ok') is not True:
        return []
    entity_types = {
        'inventory.product.get': 'product', 'inventory.product.search': 'product',
        'orders.get': 'order', 'orders.search': 'order',
        'invoices.get': 'invoice', 'invoices.search': 'invoice',
        'china.orders.get': 'china_order', 'china.orders.search': 'china_order',
    }
    entity_type = entity_types.get(operation)
    if not entity_type:
        return []
    if operation == 'inventory.product.get':
        records = [result]
    elif operation == 'inventory.product.search':
        records = result.get('candidates')
    elif isinstance(result.get('record'), Mapping):
        records = [result['record']]
    else:
        records = result.get('results')
    if not isinstance(records, list):
        return []
    allowed = {
        'product': ('id', 'sku', 'model', 'name', 'stock'),
        'order': ('id', 'order_number', 'customer_name', 'created_at', 'status'),
        'invoice': ('id', 'invoice_number', 'buyer_name', 'issue_date'),
        'china_order': ('id', 'po_number', 'supplier', 'order_status', 'delivery_stage'),
    }[entity_type]
    sources = []
    for record in records[:10]:
        if not isinstance(record, Mapping):
            continue
        try:
            entity_id = int(record.get('id') or 0)
        except (TypeError, ValueError):
            continue
        if entity_id <= 0:
            continue
        sources.append({
            'operation': operation,
            'entity_type': entity_type,
            'entity_id': entity_id,
            'trusted_result_subset': _fields(record, allowed),
            'conversation_id': conversation_id,
            'source_turn_id': source_turn_id,
        })
    return sources


def _safe_url(value: Any, prefixes: tuple[str, ...]) -> str:
    url = str(value or '')
    if not url.startswith('/') or url.startswith('//') or '\\' in url or any(ord(ch) < 32 for ch in url):
        return ''
    return url if url.startswith(prefixes) else ''


def build_artifacts(
    operation: str,
    result: Mapping[str, Any],
    resolve_links: Callable[[str, Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Map only fields already returned by a trusted operation into UI metadata."""
    if not isinstance(result, Mapping) or result.get('ok') is not True:
        return []
    record = _record(operation, result)
    if record is None:
        return []
    links = dict(resolve_links(operation, record) or {}) if resolve_links else {}
    artifacts: list[dict[str, Any]] = []

    if operation in {'invoices.get', 'invoices.search'}:
        card = {'type': 'invoice_card', **_fields(record, (
            'id', 'invoice_number', 'buyer_name', 'issue_date', 'due_date',
            'currency', 'total_net', 'total_gross', 'payment_status', 'paid',
            'amount_outstanding', 'buyer_tax_no', 'payment_type',
        ))}
        card['items'] = _items(record, (
            'sku', 'model', 'name', 'qty', 'quantity', 'unit_net_price',
            'unit_gross_price', 'currency',
        ))
        detail_url = _safe_url(links.get('detail_url'), ('/invoices/',))
        if detail_url:
            card['detail_url'] = detail_url
        artifacts.append(card)
        document_url = _safe_url(links.get('document_url'), ('/invoices/',))
        if document_url:
            artifacts.append({
                'type': 'document_link', 'document_type': 'invoice_pdf',
                'name': 'Faktura PDF', 'url': document_url,
            })

    elif operation in {'orders.get', 'orders.search'}:
        card = {'type': 'order_card', **_fields(record, (
            'id', 'order_number', 'customer_name', 'created_at', 'status',
            'currency', 'tracking_number', 'carrier', 'totals',
        ))}
        card['items'] = _items(record, (
            'product_id', 'sku', 'model', 'name', 'qty', 'unit_net_price',
            'unit_gross_price', 'currency',
        ))
        card['item_count'] = len(card['items']) if card['items'] else int(record.get('item_lines') or 0)
        card['total_units'] = sum(int(item.get('qty') or 0) for item in card['items']) if card['items'] else int(record.get('item_qty') or 0)
        detail_url = _safe_url(links.get('detail_url'), ('/orders/',))
        if detail_url:
            card['detail_url'] = detail_url
        artifacts.append(card)
        packing_url = _safe_url(
            links.get('packing_list_url'),
            ('/api/internal/ai/documents/packing-lists/',),
        )
        if packing_url:
            artifacts.append({
                'type': 'document_link', 'document_type': 'packing_list',
                'label': 'Lista pakowa', 'url': packing_url,
                'order_id': record.get('id'),
                'invoice_id': links.get('packing_invoice_id'),
            })

    elif operation in {'inventory.product.get', 'inventory.product.search'}:
        card = {'type': 'product_card', **_fields(record, (
            'id', 'sku', 'model', 'ean', 'name', 'stock', 'physical_stock',
            'available', 'available_stock', 'reserved', 'incoming', 'variant',
            'color', 'spacing',
        ))}
        detail_url = _safe_url(links.get('detail_url'), ('/api/stock/products/', '/api/product/'))
        if detail_url:
            card['detail_url'] = detail_url
        artifacts.append(card)
        image_url = _safe_url(links.get('image_url'), ('/stock/images/',))
        if image_url:
            artifacts.append({
                'type': 'product_image', 'product_id': record.get('id'),
                'url': image_url, 'alt': record.get('name') or record.get('model') or record.get('sku') or 'Produkt',
            })

    elif operation in {'china.orders.get', 'china.orders.search'}:
        card = {'type': 'china_order_card', **_fields(record, (
            'id', 'po_number', 'supplier', 'order_status', 'delivery_stage',
            'delivery_substatus', 'tracking_eta', 'carrier', 'tracking_number',
            'shipping_method', 'ordered_at', 'shipped_at', 'arrived_at',
            'created_at', 'item_count', 'total_units',
        ))}
        card['items'] = _items(record, (
            'product_id', 'sku', 'model', 'name', 'quantity', 'created_at',
        ))
        detail_url = _safe_url(links.get('detail_url'), ('/china/',))
        if detail_url:
            card['detail_url'] = detail_url
        artifacts.append(card)

    return [artifact for artifact in artifacts if artifact.get('type') in ARTIFACT_TYPES]

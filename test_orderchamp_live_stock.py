"""Offline proof that the live path only SETs a verified, unreserved SKU."""
import requests
import pytest

import orderchamp_stock_sync as sync
from orderchamp_client import OrderchampClient, OrderchampError, STOCK_SET_MUTATION
from test_orderchamp_stock import Response, Session, TOKEN, variant


def _resolved(sku='A', quantity=7):
    item = variant(sku, quantity=quantity, available=quantity)
    return {'id': item['id'], 'sku': sku, 'inventory_quantity': quantity,
            'inventory_policy': item['inventoryPolicy'],
            'levels': [{'id': 'level-' + sku, 'quantity': quantity,
                        'available_quantity': quantity,
                        'updated_at': '2026-09-29T08:00:00Z',
                        'location_id': 'warehouse-1', 'is_primary': True}],
            'levels_complete': True}


def test_push_uses_exact_set_mutation_and_verifies_result(monkeypatch):
    monkeypatch.setattr(sync, 'read_local_availability',
                        lambda path: [{'id': 1, 'sku': 'A', 'available_qty': 5}])
    session = Session([Response({'data': {'inventoryLevelBulkAdjust': {
        'clientMutationId': 'ack', 'inventoryLevels': [{'id': 'level-A', 'quantity': 5,
            'availableQuantity': 5, 'updatedAt': '2026-09-29T08:01:00Z'}], 'userErrors': []}}})])
    client = OrderchampClient(TOKEN, session=session, sleep=lambda seconds: None,
                             monotonic=lambda: 100.0)
    before, after = _resolved(), _resolved(quantity=5)
    client.resolve_variant_by_sku = lambda sku: before if len(session.calls) == 0 else after
    result = sync.push_one_stock(client, 'unused.db', sku='A', expected_local=5,
                                 expected_remote_updated_at='2026-09-29T08:00:00Z')
    assert result['status'] == 'VERIFIED' and result['wrote'] is True
    query = session.calls[0][1]['json']
    assert query['query'] == STOCK_SET_MUTATION
    assert query['variables']['input']['inventoryLevels'] == [{
        'inventoryLevelId': 'level-A', 'action': 'SET', 'adjustment': 5}]
    assert len(session.calls) == 1


@pytest.mark.parametrize('remote_change', [
    {'levels': [{**_resolved()['levels'][0], 'available_quantity': 6}]},
    {'levels': [{**_resolved()['levels'][0], 'updated_at': 'later'}]},
    {'inventory_policy': 'CONTINUE'},
])
def test_push_rejects_changed_or_unsafe_remote_without_mutation(monkeypatch, remote_change):
    monkeypatch.setattr(sync, 'read_local_availability',
                        lambda path: [{'id': 1, 'sku': 'A', 'available_qty': 5}])
    session = Session([])
    client = OrderchampClient(TOKEN, session=session)
    client.resolve_variant_by_sku = lambda sku: {**_resolved(), **remote_change}
    with pytest.raises(OrderchampError):
        sync.push_one_stock(client, 'unused.db', sku='A', expected_local=5,
                            expected_remote_updated_at='2026-09-29T08:00:00Z')
    assert not session.calls


def test_mutation_timeout_is_not_retried():
    session = Session([requests.Timeout()])
    client = OrderchampClient(TOKEN, session=session, sleep=lambda seconds: None,
                             monotonic=lambda: 100.0)
    with pytest.raises(OrderchampError, match='MUTATION_OUTCOME_UNKNOWN'):
        client.set_inventory_level('level-A', 5)
    assert len(session.calls) == 1


def test_unreconciled_remote_sale_cannot_be_overwritten(monkeypatch):
    monkeypatch.setattr(sync, 'read_local_availability',
                        lambda path: [{'id': 1, 'sku': 'A', 'available_qty': 24}])
    client = OrderchampClient(TOKEN, session=Session([]))
    client.resolve_variant_by_sku = lambda sku: _resolved(quantity=21)
    with pytest.raises(OrderchampError, match='REMOTE_BELOW_LOCAL_ORDER_RECONCILIATION_REQUIRED'):
        sync.push_one_stock(client, 'unused.db', sku='A', expected_local=24,
                            expected_remote_updated_at='2026-09-29T08:00:00Z')
    assert not client._session.calls

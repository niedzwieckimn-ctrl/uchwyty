"""One ambiguous parcel must not turn a successful read into an empty page."""
import copy
import json

import pytest

import app as backend
import shipment_amendments
import shipment_overview
import shipments_page
from packing_versions import PackingConflict
from test_shipments_page import ready, packing_cloud, multi_order_flow
from test_shipment_amendment_views import amended_parcel


def parcel(key, batch_id, order_id, selection='selection'):
    return dict(order_id=order_id, revision=1,
        packing_lists=[dict(packing_list_id=key, root_order_id=order_id, current_batch_id=batch_id)],
        packing_batches=[dict(id=batch_id, packing_list_id=key, selection_hash=selection,
                              created_at='2026-11-02 12:00:00')],
        packing_allocations=[dict(batch_id=batch_id, order_id=order_id, order_item_id=order_id,
                                 qty=2, customer_name_snapshot='Klient', order_number_snapshot=f'ZAM-{order_id}')],
        packing_shipments=[dict(shipment_key='sent:' + key, packing_list_id=key, final_batch_id=batch_id,
                                carrier='inpost', tracking='123456', confirmed_at='2026-11-02 12:30:00')])


def inconsistent_records():
    record = parcel('ambiguous', 1, 10)
    healthy = parcel('healthy', 2, 20)
    for section in ('packing_lists', 'packing_batches', 'packing_allocations', 'packing_shipments'):
        record[section].extend(healthy[section])
    # Same logical shipment, another order snapshot, remapped local batch ID,
    # different selection: exactly the production _graph line 93 conflict.
    replica = parcel('ambiguous', 999, 10, 'conflicting-selection')
    replica.update(order_id=11, revision=900)
    return [record, replica]


def test_duplicate_scope_conflict_is_isolated_even_inside_shared_snapshot():
    rows = inconsistent_records()
    before = copy.deepcopy(rows)
    with pytest.raises(PackingConflict):
        shipment_amendments.effective_shipments_from_records(rows)
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['healthy']
    assert conflicts == [{'list_ids': ['ambiguous'], 'order_ids': [10, 11]}]
    assert rows == before
    # Reordering snapshots must not choose an arbitrary supposedly good replica.
    assert shipment_overview.read(rows[::-1], shipments_page.cards_from_records) == (cards, conflicts)
    with pytest.raises(PackingConflict):
        shipment_amendments.effective_shipments_from_records(rows)


def test_identical_replicas_with_remapped_ids_are_not_conflicts():
    rows = [parcel('same', 1, 10), parcel('same', 99, 10)]
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert len(cards) == 1 and cards[0]['total'] == 2
    assert not conflicts


def test_reported_tracking_collision_across_two_lists_and_six_snapshots():
    first = parcel('498c42ae-e525-4cdd-ae56-5c315e36f8af', 23, 132, 'first-selection')
    first['packing_shipments'][0].update(shipment_key='inpost:520000014900716060164536',
        tracking='520000014900716060164536', confirmed_at='2026-10-29 12:27:23')
    rows = []
    for order_id, revision in [(132, 201), (138, 12), (141, 11), (142, 7), (143, 9)]:
        rows.append(dict(copy.deepcopy(first), order_id=order_id, revision=revision))
    second = parcel('4200f268-4455-44ab-b0d2-9194f7a4e4bb', 25, 147, 'second-selection')
    second['packing_shipments'][0].update(shipment_key='inpost:520000014900716060164536',
        tracking='520000014900716060164536', confirmed_at='2026-10-29 15:39:08')
    rows += [second, parcel('unrelated', 30, 150)]
    with pytest.raises(PackingConflict):
        shipments_page.cards_from_records(rows)
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['unrelated']
    assert len(conflicts) == 1
    assert conflicts[0]['order_ids'] == [132, 138, 141, 142, 143, 147]
    assert set(conflicts[0]['list_ids']) == {first['packing_lists'][0]['packing_list_id'],
                                          second['packing_lists'][0]['packing_list_id']}


@pytest.mark.parametrize('batch_list', ['another-list', None])
def test_wrong_batch_list_reference_cannot_hide_scope_conflict(batch_list):
    rows = inconsistent_records()
    rows[1]['packing_batches'][0]['packing_list_id'] = batch_list
    with pytest.raises(PackingConflict):
        shipments_page.cards_from_records(rows)
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['healthy']
    assert 'ambiguous' in conflicts[0]['list_ids']


def test_joined_parcel_with_one_conflict_cannot_be_shown_as_partial_total():
    rows = inconsistent_records()
    joined = parcel('joined', 3, 30)
    joined['packing_shipments'][0]['shipment_key'] = 'join:proof'
    joined['fulfillment_verifications'] = [dict(kind='join:proof', payload=json.dumps(
        {'packing_list': 'joined', 'parents': ['sent:ambiguous']}))]
    cards, conflicts = shipment_overview.read(rows + [joined], shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['healthy']
    assert conflicts[0]['list_ids'] == ['ambiguous', 'joined']


def test_valid_amendment_remains_one_parcel_and_conflict_blocks_whole_chain(amended_parcel):
    amended_parcel()
    rows, _ = shipments_page.records(backend)
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert not conflicts and len(cards) == 1 and cards[0]['total'] == 50
    replica = copy.deepcopy(rows[0])
    replica['packing_batches'][0]['selection_hash'] = 'conflicting-selection'
    healthy = parcel('unrelated', 900, 900)
    cards, conflicts = shipment_overview.read(rows + [replica, healthy], shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['unrelated']
    assert conflicts[0]['list_ids'] == ['original-list', 'replacement-list']


def test_malformed_amendment_proof_is_not_discarded():
    damaged = parcel('damaged', 1, 1)
    damaged['fulfillment_verifications'] = [dict(kind='shipment_amendment:invalid', payload='{')]
    cards, conflicts = shipment_overview.read([damaged, parcel('healthy', 2, 2)], shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['healthy']
    assert conflicts[0]['list_ids'] == ['damaged']


def test_incomplete_cross_snapshot_proof_keeps_predecessor_blocked():
    proof_only = dict(order_id=10, revision=1, fulfillment_verifications=[dict(
        kind='shipment_amendment:invalid', payload=json.dumps({'original_shipment_key': 'sent:damaged'}))])
    rows = [proof_only, parcel('damaged', 1, 1), parcel('healthy', 2, 2)]
    cards, conflicts = shipment_overview.read(rows, shipments_page.cards_from_records)
    assert [c['key'] for c in cards] == ['healthy']
    assert conflicts[0]['list_ids'] == ['damaged']


def test_page_warns_and_blocks_only_conflicted_list_actions(ready, monkeypatch):
    client, _, _, _ = ready
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    local, _ = shipments_page.records(backend)
    good_key = shipments_page.cards_from_records(local)[0]['key']
    rows = local + inconsistent_records()
    before = copy.deepcopy(rows)
    monkeypatch.setattr(shipments_page, 'records', lambda _: (rows, False))
    response = client.get('/shipments')
    assert response.status_code == 200
    assert 'data-shipment="healthy"' in response.text and f'data-shipment="{good_key}"' in response.text
    assert response.text.count('data-shipment-conflict') == 1
    assert 'Wszystkie przygotowane paczki zostały wysłane' not in response.text
    assert '/orders/10' in response.text and '/orders/11' in response.text
    for action in ('continue', 'packing-list'):
        assert client.get(f'/shipments/ambiguous/{action}').status_code == 409
        assert client.get(f'/shipments/{good_key}/{action}').status_code == 302
    assert rows == before


def test_only_conflicts_is_warning_not_false_empty_success(ready, monkeypatch):
    client, _, _, _ = ready
    monkeypatch.setattr(backend, 'supabase_enabled', lambda: False)
    rows = [parcel('bad', 1, 1), parcel('bad', 2, 1, 'different')]
    monkeypatch.setattr(shipments_page, 'records', lambda _: (rows, False))
    response = client.get('/shipments?q=missing')
    assert response.status_code == 200
    assert 'data-shipment-conflict' in response.text
    assert 'Brak przygotowywanych przesyłek' not in response.text
    assert 'Wszystkie przygotowane paczki zostały wysłane' not in response.text

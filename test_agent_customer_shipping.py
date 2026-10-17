"""Customer shipping answers must use exact SKU stock, not model-level guesses."""

import agent_runtime as runtime
import app as backend
import business_operations as operations
from test_agent_runtime import isolated, owner, respond, tool


def _seed_customer_orders():
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(4,'CH-WIN-1','Winsor','Winsor',?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(4,5)")
    for order_id, number in ((20, 'ZAM-20'), (21, 'ZAM-21')):
        db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,status,created_at,currency,price_list) VALUES(?,?,10,'AM Interiors','confirmed',?,'PLN','pln')", (order_id, number, now))
    for item_id, order_id, qty in ((201, 20, 4), (202, 21, 3)):
        db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) VALUES(?,?,?,?,?,10,'PLN',?)", (item_id, order_id, 4, 'CH-WIN-1', qty, now))
    db.commit()
    db.close()


def test_customer_shipping_snapshot_allocates_same_sku_once_and_never_writes_stock():
    _seed_customer_orders()
    actor = owner()
    db = backend.conn()
    try:
        before = db.execute('SELECT qty FROM stock WHERE product_id=4').fetchone()[0]
    finally:
        db.close()
    result = operations.execute_business_operation(actor, 'orders.customer_shipping_options',
        {'customer': 'AM Interiors'})
    assert result.status == 'SUCCESS', (result.error_code, result.safe_error_message)
    assert [order['potential_units_now'] for order in result.data['orders']] == [4, 1]
    assert [order['remaining_units'] for order in result.data['orders']] == [4, 3]
    assert result.data['orders'][1]['items'][0]['on_hand_qty'] == 5
    db = backend.conn()
    try:
        assert db.execute('SELECT qty FROM stock WHERE product_id=4').fetchone()[0] == before
    finally:
        db.close()


def test_customer_shipping_does_not_merge_distinct_skus_with_same_model():
    _seed_customer_orders()
    db = backend.conn()
    try:
        now = backend.now_iso()
        db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(5,'CH-WIN-2','Winsor','Winsor',?)", (now,))
        db.execute("INSERT INTO stock(product_id,qty) VALUES(5,0)")
        db.execute("INSERT INTO order_items(id,order_id,product_id,sku,qty,unit_net_price,currency,created_at) VALUES(203,20,5,'CH-WIN-2',2,10,'PLN',?)", (now,))
        db.commit()
    finally:
        db.close()
    result = operations.execute_business_operation(owner(), 'orders.customer_shipping_options',
        {'customer': 'AM Interiors'})
    assert result.status == 'SUCCESS'
    lines = {item['sku']: item for item in result.data['orders'][0]['items']}
    assert lines['CH-WIN-1']['potential_now_qty'] == 4
    assert lines['CH-WIN-2']['potential_now_qty'] == 0


def test_customer_shipping_resolves_inflected_polish_customer_name():
    _seed_customer_orders()
    db = backend.conn()
    try:
        db.execute("UPDATE orders SET customer_name='ARTYSTYCZNA MANUFAKTURA AGNIESZKA WOJEWÓDKA'")
        db.commit()
    finally:
        db.close()
    result = operations.execute_business_operation(owner(), 'orders.customer_shipping_options',
        {'customer': 'artystycznej manufaktury'})
    assert result.status == 'SUCCESS'
    assert result.data['customer'] == 'ARTYSTYCZNA MANUFAKTURA AGNIESZKA WOJEWÓDKA'


def test_customer_shipping_agent_uses_one_verified_read_and_plain_answer():
    _seed_customer_orders()
    provider = runtime.FakeModelProvider([
        tool('orders.customer_shipping_options', {'customer': 'AM Interiors'}),
    ])
    result = runtime.run_agent_turn(owner(), 'Jakie uchwyty mogę wysłać do AM Interiors?', provider)
    assert result['status'] == 'SUCCESS' and result['tool_calls'] == 1, result
    assert len(provider.calls) == 1
    assert {item['name'] for item in provider.calls[0]['tools']} == {'orders.customer_shipping_options'}
    assert 'ZAM-20: CH-WIN-1 — 4 z 4 szt.' in result['message']
    assert 'ZAM-21: CH-WIN-1 — 1 z 3 szt.' in result['message']
    assert 'nie rezerwacja' in result['message']


def test_textual_tool_call_cannot_become_a_stock_answer():
    answer = ('Pobieram stan.\nto=functions.businessinventorystate code:\n'
              '{"product_ids":[4]}\nWinsor — można wysłać 9 sztuk.')
    result = runtime.run_agent_turn(owner(), 'Pokaż dane firmy',
        runtime.FakeModelProvider([respond(answer)]))
    assert result['status'] == 'FAILED'
    assert '9 sztuk' not in result['message']
    assert 'to=functions' not in result['message']


def test_customer_shipping_followup_uses_same_bounded_read_catalog():
    question = ('Sprawdź ilości w zamówieniach oraz jakie są dostępne ilości. '
                'Przecież te dostępne ilości mogę już wysłać.')
    assert runtime._detect_read_intent(question) == 'customer_partial_shipping'
    assert runtime._detect_read_intent('Co mogę dziś wysłać?') == 'order_readiness'


def test_customer_shipping_routes_natural_customer_phrasings():
    for question in (
        'Ile mogę wysłać klientowi AM Interiors?',
        'Ile mogę wysłać AM Interiors?',
        'Co mogę nadać do AM Interiors?',
    ):
        assert runtime._detect_read_intent(question) == 'customer_partial_shipping'


def test_customer_shipping_does_not_treat_products_documents_or_destinations_as_customers():
    for question in (
        'Ile mogę wysłać Winsor BB?',
        'Czy mogę wysłać klientowi fakturę?',
        'Co mogę nadać do paczkomatu InPost?',
    ):
        assert runtime._detect_read_intent(question) != 'customer_partial_shipping'

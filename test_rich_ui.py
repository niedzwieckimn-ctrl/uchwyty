import json

import agent_artifacts
import agent_runtime as runtime
import app as backend
import business_operations as operations
from test_business_operations import _owner
from test_first_supervised_write import ai, isolated, order, status


def _artifact(items, kind):
    return next(item for item in items if item['type'] == kind)


def _invoice(order, pdf_path=''):
    db = backend.conn()
    now = backend.now_iso()
    db.execute(
        """INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,
               buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency)
           VALUES(801,?,'FV/801','2026-09-01','2026-09-01','transfer','2026-09-15',
                  'Kraft','1234567890',100,123,?,'PLN')""",
        (order, now),
    )
    db.execute(
        """INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,paid,updated_at)
           VALUES(801,?,'[{"sku":"SKU-1","name":"Uchwyt","qty":2,"unit_net_price":50}]',0,?)""",
        (pdf_path, now),
    )
    db.commit()
    db.close()


def test_a_invoice_result_builds_card_and_existing_pdf_link(order, tmp_path):
    pdf = tmp_path / 'invoice.pdf'
    pdf.write_bytes(b'%PDF-1.4\n%%EOF')
    _invoice(order, str(pdf))
    result = operations.execute_business_operation(ai(), 'invoices.get', {'id': 801})
    artifacts = backend.build_business_artifacts('invoices.get', result.data)
    card = _artifact(artifacts, 'invoice_card')
    document = _artifact(artifacts, 'document_link')
    assert card['id'] == 801 and card['invoice_number'] == 'FV/801'
    assert card['buyer_name'] == 'Kraft' and card['total_gross'] == 123.0
    assert document == {
        'type': 'document_link', 'document_type': 'invoice_pdf',
        'name': 'Faktura PDF', 'url': '/invoices/801/download',
    }


def test_b_missing_pdf_does_not_create_false_document_link(order, tmp_path):
    _invoice(order, str(tmp_path / 'missing.pdf'))
    result = operations.execute_business_operation(ai(), 'invoices.get', {'id': 801})
    artifacts = backend.build_business_artifacts('invoices.get', result.data)
    assert _artifact(artifacts, 'invoice_card')['id'] == 801
    assert not any(item['type'] == 'document_link' for item in artifacts)


def test_c_order_result_builds_order_card_from_returned_fields(order):
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(701,'SKU-701','Avery 160','Avery',?)", (now,))
    db.execute("INSERT INTO order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at) VALUES(?,701,'SKU-701',3,10,12.3,'PLN',?)", (order, now))
    db.commit()
    db.close()
    result = operations.execute_business_operation(ai(), 'orders.get', {'id': order})
    card = _artifact(backend.build_business_artifacts('orders.get', result.data), 'order_card')
    assert card['id'] == order and card['status'] == 'new'
    assert card['item_count'] == 1 and card['total_units'] == 3
    assert card['detail_url'] == f'/orders/{order}'


def _product(image_path=None):
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,ean,name,created_at) VALUES(702,'CH034-BB-160','Avery 160','EAN702','Avery',?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(702,12)")
    if image_path is not None:
        cursor = db.execute("INSERT INTO product_images(stored_path,filename,created_at) VALUES(?,'avery.png',?)", (str(image_path), now))
        db.execute("INSERT INTO product_image_assignments(product_id,image_id,created_at) VALUES(702,?,?)", (cursor.lastrowid, now))
    db.commit()
    db.close()


def test_d_product_with_image_builds_card_and_product_image(order, tmp_path):
    image = tmp_path / 'avery.png'
    image.write_bytes(b'not-decoded-by-artifact-builder')
    _product(image)
    result = operations.execute_business_operation(ai(), 'inventory.product.get', {'product_id': 702})
    artifacts = backend.build_business_artifacts('inventory.product.get', result.data)
    card = _artifact(artifacts, 'product_card')
    picture = _artifact(artifacts, 'product_image')
    assert card['id'] == 702 and card['stock'] == 12
    assert picture['product_id'] == 702 and picture['url'].startswith('/stock/images/')


def test_e_product_without_image_keeps_product_card(order):
    _product()
    result = operations.execute_business_operation(ai(), 'inventory.product.get', {'product_id': 702})
    artifacts = backend.build_business_artifacts('inventory.product.get', result.data)
    assert _artifact(artifacts, 'product_card')['sku'] == 'CH034-BB-160'
    assert not any(item['type'] == 'product_image' for item in artifacts)


def test_f_china_order_keeps_status_stage_and_eta_separate(order):
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(703,'CN-703','China model','China item',?)", (now,))
    db.execute(
        """INSERT INTO china_packages(id,package_no,supplier,status,tracking_status,tracking_eta,
               tracking_carrier,tracking,created_at)
           VALUES(703,'PO-703','Factory','ordered','in_transit','2026-10-01','DHL','TRACK703',?)""",
        (now,),
    )
    db.execute("INSERT INTO china_items(package_id,product_id,sku,qty,created_at) VALUES(703,703,'CN-703',20,?)", (now,))
    db.commit()
    db.close()
    result = operations.execute_business_operation(ai(), 'china.orders.get', {'id': 703})
    card = _artifact(backend.build_business_artifacts('china.orders.get', result.data), 'china_order_card')
    assert card['order_status'] == 'ordered'
    assert card['delivery_stage'] == 'in_transit'
    assert card['tracking_eta'] == '2026-10-01'
    assert card['item_count'] == 1 and card['total_units'] == 20


def test_g_artifact_cannot_add_fields_absent_from_operation_result():
    returned = {'ok': True, 'record': {'id': 9, 'sku': 'SAFE', 'stock': 2, 'secret_cost': 999}}
    artifacts = agent_artifacts.build_artifacts(
        'inventory.product.get', returned,
        lambda *_: {'image_url': '/stock/images/7', 'invented': 'hidden'},
    )
    encoded = json.dumps(artifacts)
    assert 'secret_cost' not in encoded and 'invented' not in encoded


def test_h_llm_text_and_url_alone_cannot_create_artifact(order):
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text='Otwórz https://evil.test/invoice.pdf dla invoice_id=999.', model='fake')
    ])
    result = runtime.run_agent_turn(_owner(), 'Pokaż dokument.', provider)
    assert result['status'] == 'SUCCESS' and result['artifacts'] == []


def test_runtime_attaches_trusted_product_artifacts_to_natural_answer(order):
    _product()
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            'product-call', 'inventory.product.get', json.dumps({'product_id': 702}),
        ),), model='fake'),
        runtime.ProviderResponse(text='Avery 160 ma 12 sztuk.', model='fake'),
    ])
    result = runtime.run_agent_turn(_owner(), 'Pokaż Avery 160.', provider)
    assert result['message'] == 'Avery 160 ma 12 sztuk.'
    assert _artifact(result['artifacts'], 'product_card')['id'] == 702


def test_i_approval_cards_remain_independent_of_artifacts(order):
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall('order-read', 'orders.get', json.dumps({'id': order})),
            runtime.ToolCall(
                'status-call', 'orders.status.transition',
                json.dumps({'order_id': order, 'target_status': 'confirmed',
                            'expected_version': 0, 'idempotency_key': 'rich-ui-status'}),
            ),
        ), model='fake'),
        runtime.ProviderResponse(text='Zmiana wymaga zatwierdzenia.', model='fake'),
    ])
    result = runtime.run_agent_turn(_owner(), 'Potwierdź zamówienie.', provider)
    assert result['status'] == 'SUCCESS'
    assert result['approvals'] == result['pending_approvals']
    assert result['approvals'][0]['order_id'] == order
    assert _artifact(result['artifacts'], 'order_card')['id'] == order

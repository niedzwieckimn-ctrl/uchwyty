import hashlib
import json
from pathlib import Path
import uuid

import pytest
from reportlab.pdfgen import canvas

import app as backend
import agent_runtime as runtime
import business_operations as operations
import internal_rbac as rbac
import internal_approval
import internal_audit
import packing_history
from test_agent_runtime import isolated, owner, tool


@pytest.fixture
def historical_batch(tmp_path):
    now = "2026-09-15T12:00:00+02:00"
    db = backend.conn()
    db.execute(
        "INSERT INTO customers(id,name,address,phone,email,nip,language,price_list,created_at) "
        "VALUES(20,'Artystyczna Manufaktura','','','art@example.test','','pl','pln',?)",
        (now,),
    )
    products = (
        (30, "CH030-BB-N25", "Tom", "Tom"),
        (32, "CH032-BB-N25", "Leo", "Leo"),
        (10, "CH010-BB-192232", "Winsor", "Winsor"),
        (36, "CH036-BN-192240", "Sam", "Sam"),
        (11, "CH010-AB-320360", "Winsor 320", "Winsor 320"),
    )
    for product_id, sku, model, name in products:
        db.execute(
            "INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)",
            (product_id, sku, model, name, now),
        )
    orders = (
        (151, "ZAM-2609151"),
        (141, "ZAM-2609141"),
        (121, "ZAM-2609121"),
        (8271, "ZAM-2608271"),
    )
    for order_id, order_number in orders:
        db.execute(
            "INSERT INTO orders(id,order_no,customer_id,customer_name,customer_email,status,created_at,currency,price_list) "
            "VALUES(?,?,?,?,?,'packed',?,'PLN','pln')",
            (order_id, order_number, 20, "Artystyczna Manufaktura", "art@example.test", now),
        )
    # Current order state deliberately totals 49 and matches the bad production answer.
    current_items = (
        (1511, 151, 30, "CH030-BB-N25", 2),
        (1512, 151, 32, "CH032-BB-N25", 2),
        (1411, 141, 10, "CH010-BB-192232", 32),
        (1211, 121, 36, "CH036-BN-192240", 6),
        (82711, 8271, 11, "CH010-AB-320360", 7),
    )
    for item_id, order_id, product_id, sku, qty in current_items:
        db.execute(
            "INSERT INTO order_items(id,order_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, order_id, product_id, sku, qty, now),
        )
    db.execute(
        """INSERT INTO invoices(
               id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,
               buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency
           ) VALUES(770,151,'FV-HISTORY-770','2026-09-15','2026-09-15','transfer',NULL,
                    'Artystyczna Manufaktura','',0,0,?,'PLN')""",
        (now,),
    )
    db.execute(
        "INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at) VALUES(77,151,NULL,?)",
        (now,),
    )
    historical_items = (
        (1511, 151, "ZAM-2609151", "CH030-BB-N25", "Tom", 2),
        (1512, 151, "ZAM-2609151", "CH032-BB-N25", "Leo", 2),
        (1411, 141, "ZAM-2609141", "CH010-BB-192232", "Winsor", 2),
        (1211, 121, "ZAM-2609121", "CH036-BN-192240", "Sam", 1),
        (82711, 8271, "ZAM-2608271", "CH010-AB-320360", "Winsor", 7),
    )
    product_ids = {item_id: product_id for item_id, _order_id, product_id, _sku, _qty in current_items}
    pdf_items = []
    invoice_items = []
    for allocation_id, (item_id, order_id, order_number, sku, model, qty) in enumerate(
            historical_items, start=1):
        db.execute(
            """INSERT INTO packing_allocations(
                   id,batch_id,order_id,order_item_id,qty,created_at,
                   order_number_snapshot,sku_snapshot,model_name_snapshot,note_snapshot,
                   customer_id_snapshot,customer_name_snapshot,customer_email_snapshot
               ) VALUES(?,77,?,?,?,?,?,?,?,?,20,'Artystyczna Manufaktura','art@example.test')""",
            (allocation_id, order_id, item_id, qty, now, order_number, sku, model, ""),
        )
        db.execute(
            """INSERT INTO invoice_allocations(
                   invoice_id,order_id,order_item_id,product_id,sku,qty,created_at
               ) VALUES(770,?,?,?,?,?,?)""",
            (order_id, item_id, product_ids[item_id], sku, qty, now),
        )
        pdf_items.append({
            "source_order_no": order_number,
            "source_order_note": "",
            "sku": sku,
            "model": model,
            "qty": qty,
        })
        invoice_items.append({
            "id": item_id,
            "order_item_id": item_id,
            "order_id": order_id,
            "source_order_id": order_id,
            "source_order_no": order_number,
            "source_order_note": "",
            "sku": sku,
            "model": model,
            "name": model,
            "qty": qty,
        })
    db.execute(
        "INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,updated_at) VALUES(770,'',?,?)",
        (json.dumps(invoice_items, ensure_ascii=False), now),
    )
    db.commit()

    pdf_path = backend.generate_invoice_packing_list_pdf(
        {"order_no": "ZAM-2609151", "customer_name": "Artystyczna Manufaktura", "created_at": now},
        pdf_items,
        {
            "invoice_no": "PACKING-BATCH-77",
            "buyer_name": "Artystyczna Manufaktura",
            "language": "pl",
            "document_label_key": "order",
        },
        str(tmp_path / "batch-77.pdf"),
    )
    file_hash = hashlib.sha256(open(pdf_path, "rb").read()).hexdigest()
    db.executemany(
        "INSERT INTO fulfillment_documents(order_id,kind,document_id,content_hash,path,created_at,file_hash) "
        "VALUES(?,'packing_list',77,'fixture-history',?,?,?)",
        [(order_id, pdf_path, now, file_hash) for order_id, _number in orders],
    )
    db.commit()
    current_total = db.execute(
        "SELECT SUM(oi.qty) FROM order_items oi JOIN orders o ON o.id=oi.order_id WHERE o.customer_id=20"
    ).fetchone()[0]
    db.close()
    assert current_total == 49
    return {"batch_id": 77, "pdf_path": pdf_path}


def _insert_history_batch(tmp_path, *, batch_id, invoice_id, rows, pdf_rows=None):
    """Create a complete immutable snapshot; ``pdf_rows`` may use another order."""
    now = "2026-09-15T13:00:00+02:00"
    db = backend.conn()
    seen_orders = set()
    for row in rows:
        if row["order_id"] not in seen_orders:
            db.execute(
                """INSERT INTO orders(
                       id,order_no,customer_id,customer_name,customer_email,status,created_at,currency,price_list
                   ) VALUES(?,?,20,'Artystyczna Manufaktura','art@example.test','packed',?,'PLN','pln')""",
                (row["order_id"], row["order_number"], now),
            )
            seen_orders.add(row["order_id"])
        db.execute(
            "INSERT INTO order_items(id,order_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
            (row["item_id"], row["order_id"], row["product_id"], row["sku"], row["qty"], now),
        )
    root_order_id = rows[0]["order_id"]
    db.execute(
        """INSERT INTO invoices(
               id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,
               buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency
           ) VALUES(?,?,?,'2026-09-15','2026-09-15','transfer',NULL,
                    'Artystyczna Manufaktura','',0,0,?,'PLN')""",
        (invoice_id, root_order_id, f"FV-HISTORY-{invoice_id}", now),
    )
    db.execute(
        "INSERT INTO packing_batches(id,root_order_id,invoice_id,created_at) VALUES(?,?,NULL,?)",
        (batch_id, root_order_id, now),
    )
    invoice_items = []
    for row in rows:
        db.execute(
            """INSERT INTO packing_allocations(
                   batch_id,order_id,order_item_id,qty,created_at,
                   order_number_snapshot,sku_snapshot,model_name_snapshot,note_snapshot,
                   customer_id_snapshot,customer_name_snapshot,customer_email_snapshot
               ) VALUES(?,?,?,?,?,?,?,?,?,20,'Artystyczna Manufaktura','art@example.test')""",
            (batch_id, row["order_id"], row["item_id"], row["qty"], now,
             row["order_number"], row["sku"], row["model"], row.get("note", "")),
        )
        db.execute(
            """INSERT INTO invoice_allocations(
                   invoice_id,order_id,order_item_id,product_id,sku,qty,created_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (invoice_id, row["order_id"], row["item_id"], row["product_id"],
             row["sku"], row["qty"], now),
        )
        invoice_items.append({
            "id": row["item_id"],
            "order_item_id": row["item_id"],
            "order_id": row["order_id"],
            "source_order_id": row["order_id"],
            "source_order_no": row["order_number"],
            "source_order_note": row.get("note", ""),
            "sku": row["sku"],
            "model": row["model"],
            "name": row["model"],
            "qty": row["qty"],
        })
    db.execute(
        "INSERT INTO invoice_meta(invoice_id,pdf_path,invoice_items_json,updated_at) VALUES(?,'',?,?)",
        (invoice_id, json.dumps(invoice_items, ensure_ascii=False), now),
    )
    db.commit()

    document_rows = list(pdf_rows if pdf_rows is not None else rows)
    pdf_path = backend.generate_invoice_packing_list_pdf(
        {"order_no": rows[0]["order_number"], "customer_name": "Artystyczna Manufaktura", "created_at": now},
        [{
            "source_order_no": row["order_number"],
            "source_order_note": row.get("note", ""),
            "sku": row["sku"],
            "model": row["model"],
            "qty": row["qty"],
        } for row in document_rows],
        {
            "invoice_no": f"PACKING-BATCH-{batch_id}",
            "buyer_name": "Artystyczna Manufaktura",
            "language": "pl",
            "document_label_key": "order",
        },
        str(tmp_path / f"batch-{batch_id}.pdf"),
    )
    file_hash = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    db.executemany(
        """INSERT INTO fulfillment_documents(
               order_id,kind,document_id,content_hash,path,created_at,file_hash
           ) VALUES(?,'packing_list',?,'fixture-history',?,?,?)""",
        [(order_id, batch_id, pdf_path, now, file_hash) for order_id in seen_orders],
    )
    db.commit()
    db.close()
    return pdf_path


def _write_parser_fixture_pdf(path, rows, *, include_total_lines=True, include_total_qty=True):
    pdf = canvas.Canvas(str(path))
    y = 800
    pdf.drawString(40, y, "QTY")
    y -= 14
    for line_number, row in enumerate(rows, start=1):
        source = row["order_number"]
        if row.get("note"):
            source += f" · {row['note']}"
        for value in (line_number, row["sku"], row["model"], source, row["qty"]):
            pdf.drawString(40, y, str(value))
            y -= 14
    if include_total_lines:
        pdf.drawString(40, y, f"Items: {len(rows)}")
        y -= 14
    if include_total_qty:
        pdf.drawString(40, y, f"Total quantity: {sum(row['qty'] for row in rows)}")
    pdf.save()
    return str(path)


def _replace_batch_document(batch_id, pdf_path):
    file_hash = hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest()
    db = backend.conn()
    db.execute(
        "UPDATE fulfillment_documents SET path=?,file_hash=? WHERE document_id=? AND kind='packing_list'",
        (str(pdf_path), file_hash, batch_id),
    )
    db.commit()
    db.close()


def _ai():
    return rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, request_id="packing-history-test")


@pytest.mark.parametrize("selector", (
    {"batch_id": 77},
    {"order_id": 141},
    {"customer_id": 20},
    {"customer": "Artystyczna Manufaktura"},
    {"latest": True},
))
def test_history_read_selectors_return_saved_14_not_current_49(historical_batch, selector):
    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", selector, correlation_id="packing-history-read",
    )

    assert result.status == "SUCCESS"
    assert result.data["batch_id"] == 77
    assert result.data["order_ids"] == [121, 141, 151, 8271]
    assert result.data["total_lines"] == 5
    assert result.data["total_qty"] == 14
    assert [(row["order_number"], row["sku"], row["model_name"], row["packed_qty"])
            for row in result.data["allocations"]] == [
        ("ZAM-2609151", "CH030-BB-N25", "Tom", 2),
        ("ZAM-2609151", "CH032-BB-N25", "Leo", 2),
        ("ZAM-2609141", "CH010-BB-192232", "Winsor", 2),
        ("ZAM-2609121", "CH036-BN-192240", "Sam", 1),
        ("ZAM-2608271", "CH010-AB-320360", "Winsor", 7),
    ]


def test_result_packed_qty_is_materialized_from_allocations_not_pdf(historical_batch, monkeypatch):
    original_document_rows = packing_history._document_rows

    class PdfQuantity(int):
        pass

    def tagged_pdf_quantities(content):
        rows, total_lines, total_qty = original_document_rows(content)
        for row in rows:
            row["packed_qty"] = PdfQuantity(row["packed_qty"])
        return rows, total_lines, total_qty

    monkeypatch.setattr(packing_history, "_document_rows", tagged_pdf_quantities)
    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="allocation-qty-source",
    )

    assert result.status == "SUCCESS"
    assert result.data["total_qty"] == 14
    assert [row["packed_qty"] for row in result.data["allocations"]] == [2, 2, 2, 1, 7]
    assert all(type(row["packed_qty"]) is int for row in result.data["allocations"])


def test_reordered_equal_qty_pdf_rows_map_by_domain_key(historical_batch, tmp_path):
    rows = [
        {"order_id": 301, "item_id": 3011, "order_number": "ZAM-MAP-A",
         "product_id": 30, "sku": "CH030-BB-N25", "model": "Tom from A", "qty": 2},
        {"order_id": 302, "item_id": 3021, "order_number": "ZAM-MAP-B",
         "product_id": 32, "sku": "CH032-BB-N25", "model": "Leo from B", "qty": 2},
    ]
    _insert_history_batch(
        tmp_path, batch_id=78, invoice_id=778, rows=rows, pdf_rows=list(reversed(rows)),
    )

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 78}, correlation_id="reordered-pdf",
    )

    assert result.status == "SUCCESS"
    assert [(row["order_id"], row["sku"], row["model_name"], row["packed_qty"])
            for row in result.data["allocations"]] == [
        (301, "CH030-BB-N25", "Tom from A", 2),
        (302, "CH032-BB-N25", "Leo from B", 2),
    ]


def test_duplicate_sku_in_different_orders_is_disambiguated_by_order_number(
        historical_batch, tmp_path):
    rows = [
        {"order_id": 311, "item_id": 3111, "order_number": "ZAM-DUP-A",
         "product_id": 30, "sku": "CH030-BB-N25", "model": "Tom A", "qty": 2},
        {"order_id": 312, "item_id": 3121, "order_number": "ZAM-DUP-B",
         "product_id": 30, "sku": "CH030-BB-N25", "model": "Tom B", "qty": 2},
    ]
    _insert_history_batch(
        tmp_path, batch_id=79, invoice_id=779, rows=rows, pdf_rows=list(reversed(rows)),
    )

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 79}, correlation_id="duplicate-sku",
    )

    assert result.status == "SUCCESS"
    assert [(row["order_number"], row["model_name"], row["packed_qty"])
            for row in result.data["allocations"]] == [
        ("ZAM-DUP-A", "Tom A", 2),
        ("ZAM-DUP-B", "Tom B", 2),
    ]


def test_same_order_sku_and_qty_without_unique_key_fails_closed(historical_batch, tmp_path):
    rows = [
        {"order_id": 321, "item_id": 3211, "order_number": "ZAM-AMBIGUOUS",
         "product_id": 30, "sku": "CH030-BB-N25", "model": "First", "qty": 2},
        {"order_id": 321, "item_id": 3212, "order_number": "ZAM-AMBIGUOUS",
         "product_id": 30, "sku": "CH030-BB-N25", "model": "Second", "qty": 2},
    ]
    _insert_history_batch(tmp_path, batch_id=80, invoice_id=780, rows=rows)

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 80}, correlation_id="ambiguous-key",
    )

    assert result.status == "FAILED"
    assert result.error_code == "HISTORY_DOCUMENT_NOT_VERIFIABLE"
    assert result.data is None


def test_batch_with_null_invoice_id_uses_immutable_allocation_snapshot(historical_batch):
    db = backend.conn()
    db.execute("UPDATE packing_batches SET invoice_id=NULL WHERE id=77")
    db.commit()
    db.close()

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="missing-snapshot",
    )
    assert result.status == "SUCCESS"
    assert result.data["total_lines"] == 5
    assert result.data["total_qty"] == 14


def test_batch_without_snapshot_or_audit_fails_closed(historical_batch):
    db = backend.conn()
    db.execute("DROP TRIGGER packing_allocations_no_update")
    db.execute(
        """UPDATE packing_allocations SET order_number_snapshot=NULL,sku_snapshot=NULL,
                  model_name_snapshot=NULL,note_snapshot=NULL,customer_id_snapshot=NULL,
                  customer_name_snapshot=NULL,customer_email_snapshot=NULL WHERE batch_id=77"""
    )
    db.commit()
    db.close()

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="missing-snapshot",
    )
    assert result.status == "FAILED"
    assert result.error_code == "HISTORY_DOCUMENT_NOT_VERIFIABLE"
    assert result.data is None


def test_legacy_null_invoice_batch_uses_append_only_approval_audit(historical_batch):
    items = [
        {"order_id":151,"order_number":"ZAM-2609151","order_item_id":1511,"sku":"CH030-BB-N25","quantity":2},
        {"order_id":151,"order_number":"ZAM-2609151","order_item_id":1512,"sku":"CH032-BB-N25","quantity":2},
        {"order_id":141,"order_number":"ZAM-2609141","order_item_id":1411,"sku":"CH010-BB-192232","quantity":2},
        {"order_id":121,"order_number":"ZAM-2609121","order_item_id":1211,"sku":"CH036-BN-192240","quantity":1},
        {"order_id":8271,"order_number":"ZAM-2608271","order_item_id":82711,"sku":"CH010-AB-320360","quantity":7},
    ]
    actor = _ai()
    correlation_id = "legacy-null-invoice-audit"
    payload = {"order_id":151,"packing_items":items,"packing_scope_fingerprint":"fixture",
               "total_quantity":14,"expected_version":1}
    approval_id = internal_approval.request_approval(
        actor, "orders.packing_list.generate", payload=payload,
        entity_type="order", entity_id="151", expected_entity_version=1,
        correlation_id=correlation_id,
    )
    db = backend.conn()
    db.execute("DROP TRIGGER packing_allocations_no_update")
    db.execute(
        """UPDATE packing_allocations SET order_number_snapshot=NULL,sku_snapshot=NULL,
                  model_name_snapshot=NULL,note_snapshot=NULL,customer_id_snapshot=NULL,
                  customer_name_snapshot=NULL,customer_email_snapshot=NULL WHERE batch_id=77"""
    )
    execution_id = str(uuid.uuid4())
    now = backend.now_iso()
    db.execute(
        """INSERT INTO internal_operation_executions(
               execution_id,operation,operation_version,actor_id,actor_type,permission,risk_level,
               approval_id,entity_type,entity_id,idempotency_key,input_fingerprint,status,
               created_at,started_at,completed_at,request_id,correlation_id,result_summary,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (execution_id,"orders.packing_list.generate",1,actor.actor_id,actor.actor_type,
         "packing.prepare","YELLOW",approval_id,"order","151","fixture-idem","fixture-fingerprint",
         "SUCCESS",now,now,now,actor.request_id,correlation_id,json.dumps({"ok":True}),now),
    )
    db.commit()
    db.close()
    internal_audit.record_audit_event(
        "orders.packing_list.generate", result="SUCCESS", actor_context=actor,
        entity_type="order", entity_id="151", correlation_id=correlation_id,
        approval_id=approval_id,
        before_state={"order":{"customer_id":20,"customer_name":"Artystyczna Manufaktura",
                               "customer_email":"art@example.test"}},
        after_state={"phase":"domain_committed","batch_id":77,
                     "order_ids":[121,141,151,8271]},
    )

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id":77}, correlation_id="legacy-read")
    assert result.status == "SUCCESS"
    assert result.data["total_lines"] == 5
    assert result.data["total_qty"] == 14
    assert result.data["customer"]["name"] == "Artystyczna Manufaktura"


def test_current_order_changes_do_not_change_saved_batch_answer(historical_batch):
    before = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="before-mutation",
    ).data
    db = backend.conn()
    db.execute("UPDATE order_items SET qty=999,sku='CURRENT-NOT-HISTORY' WHERE order_id IN (151,141,121,8271)")
    db.execute("UPDATE products SET model='CURRENT MODEL' WHERE id IN (30,32,10,36,11)")
    db.execute("UPDATE orders SET status='cancelled',note='current note' WHERE customer_id=20")
    db.commit()
    db.close()

    after = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="after-mutation",
    ).data
    assert after == before
    assert after["total_qty"] == 14
    assert all(row["sku"] != "CURRENT-NOT-HISTORY" for row in after["allocations"])


def test_missing_file_hash_keeps_structural_history_readable(historical_batch):
    db = backend.conn()
    db.execute("UPDATE fulfillment_documents SET file_hash='' WHERE document_id=77")
    db.commit()
    db.close()

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="missing-hash",
    )
    assert result.status == "SUCCESS"
    assert result.data["total_qty"] == 14
    assert result.data["history_source"] == "allocation_snapshot"
    assert result.data["document_available"] is False
    assert result.data["document_verified"] is False


@pytest.mark.parametrize(("include_total_lines", "include_total_qty", "correlation_id"), (
    (False, True, "missing-footer-lines"),
    (True, False, "missing-footer-qty"),
))
def test_pdf_footer_is_not_required_for_structural_history(
        historical_batch, tmp_path, include_total_lines, include_total_qty, correlation_id):
    rows = [
        {"order_number": "ZAM-2609151", "sku": "CH030-BB-N25", "model": "Tom", "qty": 2},
        {"order_number": "ZAM-2609151", "sku": "CH032-BB-N25", "model": "Leo", "qty": 2},
        {"order_number": "ZAM-2609141", "sku": "CH010-BB-192232", "model": "Winsor", "qty": 2},
        {"order_number": "ZAM-2609121", "sku": "CH036-BN-192240", "model": "Sam", "qty": 1},
        {"order_number": "ZAM-2608271", "sku": "CH010-AB-320360", "model": "Winsor", "qty": 7},
    ]
    pdf_path = _write_parser_fixture_pdf(
        tmp_path / f"{correlation_id}.pdf",
        rows,
        include_total_lines=include_total_lines,
        include_total_qty=include_total_qty,
    )
    _replace_batch_document(77, pdf_path)

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id=correlation_id,
    )
    assert result.status == "SUCCESS"
    assert result.data["total_qty"] == 14
    assert result.data["history_source"] == "allocation_snapshot"
    assert result.data["document_available"] is True
    assert result.data["document_verified"] is True


def test_wrong_file_hash_does_not_block_structural_history(historical_batch):
    db = backend.conn()
    db.execute("UPDATE fulfillment_documents SET file_hash=? WHERE document_id=77", ("0" * 64,))
    db.commit()
    db.close()

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="wrong-hash",
    )
    assert result.status == "SUCCESS"
    assert result.data["total_qty"] == 14
    assert result.data["document_available"] is False
    assert result.data["document_verified"] is False


def test_pdf_contents_do_not_replace_structural_allocations(historical_batch, tmp_path):
    incomplete_rows = [
        {"order_number": "ZAM-2609151", "sku": "CH030-BB-N25", "model": "Tom", "qty": 2},
        {"order_number": "ZAM-2609151", "sku": "CH032-BB-N25", "model": "Leo", "qty": 2},
        {"order_number": "ZAM-2609141", "sku": "CH010-BB-192232", "model": "Winsor", "qty": 2},
        {"order_number": "ZAM-2609121", "sku": "CH036-BN-192240", "model": "Sam", "qty": 1},
    ]
    pdf_path = _write_parser_fixture_pdf(tmp_path / "incomplete.pdf", incomplete_rows)
    _replace_batch_document(77, pdf_path)

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="incomplete-pdf",
    )
    assert result.status == "SUCCESS"
    assert result.data["total_lines"] == 5
    assert result.data["total_qty"] == 14
    assert result.data["history_source"] == "allocation_snapshot"


def test_missing_saved_document_keeps_structural_history_readable(historical_batch):
    db = backend.conn()
    db.execute("DELETE FROM fulfillment_documents WHERE document_id=77 AND kind='packing_list'")
    db.commit()
    db.close()

    result = operations.execute_business_operation(
        _ai(), "orders.packing_history.get", {"batch_id": 77}, correlation_id="missing-document",
    )
    assert result.status == "SUCCESS"
    assert result.data["total_qty"] == 14
    assert result.data["document_id"] is None
    assert result.data["document_available"] is False
    assert result.data["history_source"] == "allocation_snapshot"


@pytest.mark.parametrize("phrase", (
    "Odczytaj listę pakową.",
    "Co było w paczce?",
    "Co było na liście pakowej?",
    "Co było spakowane?",
    "Jaka była zawartość paczki?",
    "Co zawierała paczka?",
    "Co ostatnio spakowałem?",
    "Co wysłałem?",
    "Ostatnia paczka dla klienta Artystyczna Manufaktura.",
))
def test_packing_history_phrases_have_dedicated_read_intent(phrase):
    assert runtime._detect_read_intent(phrase) == "packing_history"


def test_agent_routes_only_to_history_and_returns_deterministic_14(historical_batch):
    def select_history(kwargs):
        names = {item["name"] for item in kwargs["tools"]}
        assert names == {"orders.packing_history.get"}
        assert "Nie używaj" in kwargs["instructions"]
        assert "order_items" in kwargs["instructions"]
        return tool(
            "orders.packing_history.get",
            {"customer": "Artystyczna Manufaktura"},
            call_id="packing-history-1",
        )

    provider = runtime.FakeModelProvider([select_history])
    result = runtime.run_agent_turn(
        owner(), "Odczytaj ostatnią listę pakową dla Artystycznej Manufaktury.", provider,
    )

    assert result["status"] == "SUCCESS"
    assert result["tool_calls"] == 1
    assert len(provider.calls) == 1
    assert "Razem: 5 pozycji, 14 sztuk." in result["message"]
    assert "CH030-BB-N25 Tom — 2" in result["message"]
    assert "CH032-BB-N25 Leo — 2" in result["message"]
    assert "CH010-BB-192232 Winsor — 2" in result["message"]
    assert "CH036-BN-192240 Sam — 1" in result["message"]
    assert "CH010-AB-320360 Winsor — 7" in result["message"]
    assert "49" not in result["message"]

    db = backend.conn()
    executions = db.execute(
        "SELECT operation,status FROM internal_operation_executions "
        "WHERE operation='orders.packing_history.get'"
    ).fetchall()
    db.close()
    assert [tuple(row) for row in executions] == [("orders.packing_history.get", "SUCCESS")]


def test_agent_uses_history_read_instead_of_accepting_model_guess(historical_batch):
    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(text="Winsor 39, Sam 6, Tom 2, Leo 2. Razem 49 sztuk.", model="fake-model")
    ])
    result = runtime.run_agent_turn(owner(), "Odczytaj ostatnią listę pakową.", provider)

    assert result["status"] == "SUCCESS"
    assert result["tool_calls"] == 1
    assert "Razem: 5 pozycji, 14 sztuk." in result["message"]
    assert "49" not in result["message"]


def test_reference_conversation_keeps_alias_batch_and_existing_pdf(historical_batch):
    remember = runtime.FakeModelProvider([
        tool("agent.terminology.remember", {
            "term":"Warszawa", "meaning":"Artystyczna Manufaktura",
            "confirmed_by_user":True, "expected_version":0,
        }, call_id="remember-warszawa"),
        runtime.ProviderResponse(text="Rozumiem.", model="fake-model"),
    ])
    turn1 = runtime.run_agent_turn(
        owner(), "Zapamiętaj, że Artystyczną Manufakturę będę nazywał Warszawą.", remember)
    assert turn1["status"] == "SUCCESS"
    assert "Zapisane: Warszawa oznacza Artystyczna Manufaktura." in turn1["message"]
    conversation_id = turn1["conversation_id"]

    def read_alias(kwargs):
        evidence = json.dumps(kwargs["input_items"], ensure_ascii=False)
        assert "confirmed_business_terminology" in evidence
        assert "Warszawa" in evidence and "Artystyczna Manufaktura" in evidence
        assert {item["name"] for item in kwargs["tools"]} == {"orders.packing_history.get"}
        return tool("orders.packing_history.get", {"customer":"Artystyczna Manufaktura"},
                    call_id="history-warszawa")

    turn2_provider = runtime.FakeModelProvider([read_alias])
    turn2 = runtime.run_agent_turn(
        owner(), "Co ostatnio spakowałem do Warszawy?", turn2_provider,
        conversation_id=conversation_id)
    assert turn2["status"] == "SUCCESS"
    assert "Razem: 5 pozycji, 14 sztuk." in turn2["message"]
    assert "49" not in turn2["message"] and "Razem: 4" not in turn2["message"]

    turn3_provider = runtime.FakeModelProvider([])
    turn3 = runtime.run_agent_turn(
        owner(), "Jaka była lista pakowania?", turn3_provider,
        conversation_id=conversation_id)
    assert turn3["status"] == "SUCCESS"
    assert "batch 77" in turn3["message"]
    assert "Razem: 5 pozycji, 14 sztuk." in turn3["message"]
    assert not turn3_provider.calls

    turn4_provider = runtime.FakeModelProvider([])
    turn4 = runtime.run_agent_turn(
        owner(), "PDF chcę ją.", turn4_provider, conversation_id=conversation_id)
    assert turn4["status"] == "SUCCESS"
    assert "istniejący historyczny dokument" in turn4["message"]
    assert not turn4_provider.calls
    assert turn4["tool_calls"] == 1
    assert turn4["artifacts"] == [{
        "type":"document_link", "document_type":"packing_list",
        "name":"Historyczna lista pakowa PDF",
        "url":"/api/internal/ai/documents/packing-history/77",
        "batch_id":77, "document_id":77,
    }]

    db = backend.conn()
    writes = db.execute(
        """SELECT COUNT(*) FROM internal_operation_executions
             WHERE operation='orders.packing_list.generate'"""
    ).fetchone()[0]
    db.close()
    assert writes == 0


def test_history_pdf_route_returns_existing_verified_file_without_write(
        historical_batch, monkeypatch):
    monkeypatch.setattr(
        backend, "generate_invoice_packing_list_pdf",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("generator must not run")),
    )
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"
    response = client.get("/api/internal/ai/documents/packing-history/77")
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert hashlib.sha256(response.data).hexdigest() == hashlib.sha256(
        Path(historical_batch["pdf_path"]).read_bytes()).hexdigest()
    db = backend.conn()
    assert db.execute(
        "SELECT COUNT(*) FROM internal_operation_executions WHERE operation='orders.packing_list.generate'"
    ).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM internal_approval_requests").fetchone()[0] == 0
    db.close()

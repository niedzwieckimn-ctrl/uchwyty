import json
import uuid

import pytest

import agent_runtime as runtime
import app as backend
import business_operations as operations
import fulfillment_operations as fulfillment
import internal_approval as approvals
import internal_rbac as rbac


ROOT_ORDER_ID = 101


@pytest.fixture
def multi_order_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "multi-order-packing.db"))
    monkeypatch.setattr(backend, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **kw: None)
    monkeypatch.setattr(backend, "_client_profile_for_email", lambda email: {})
    monkeypatch.setattr(backend, "inpost_pickup_status", lambda shipment_id: {})
    monkeypatch.setattr(backend, "inpost_config_summary", lambda: {"configured": True, "missing": []})
    monkeypatch.setattr(operations, "_freshness_provider", None)
    monkeypatch.setattr(operations, "_write_success_observer", None)
    backend.app.secret_key = "multi-order-packing-test"
    backend.init_db()

    now = backend.now_iso()
    db = backend.conn()
    for product_id, sku, stock_qty in (
        (1, "SKU-PARTIAL", 3),
        (2, "SKU-NONE", 0),
        (3, "SKU-AVAILABLE", 2),
    ):
        db.execute(
            "INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)",
            (product_id, sku, f"MODEL-{product_id}", f"Produkt {product_id}", now),
        )
        db.execute("INSERT INTO stock(product_id,qty) VALUES(?,?)", (product_id, stock_qty))

    orders = (
        (101, "ZAM-2608271", "Artystyczna Manufaktura", "art@example.invalid", "partially_shipped", now),
        (102, "ZAM-2609121", "Artystyczna Manufaktura", "art@example.invalid", "confirmed", None),
        (103, "ZAM-2609151", "Artystyczna Manufaktura", "art@example.invalid", "packed", None),
        (201, "ZAM-OTHER", "Inny klient", "other@example.invalid", "confirmed", None),
    )
    for order_id, order_no, customer, email, status, shipped_at in orders:
        db.execute(
            """INSERT INTO orders(
                   id,order_no,customer_name,customer_email,status,created_at,currency,shipped_at
                 ) VALUES(?,?,?,?,?,?,?,?)""",
            (order_id, order_no, customer, email, status, now, "PLN", shipped_at),
        )

    items = (
        (1001, 101, 1, "SKU-PARTIAL", 5),
        (1002, 102, 2, "SKU-NONE", 4),
        (1003, 103, 3, "SKU-AVAILABLE", 3),
        (2001, 201, 3, "SKU-AVAILABLE", 1),
    )
    for item_id, order_id, product_id, sku, qty in items:
        db.execute(
            """INSERT INTO order_items(
                   id,order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at
                 ) VALUES(?,?,?,?,?,10,12.3,'PLN',?)""",
            (item_id, order_id, product_id, sku, qty, now),
        )

    db.execute(
        """INSERT INTO invoices(
               id,order_id,invoice_no,issue_date,sell_date,payment_type,total_net,total_gross,created_at
             ) VALUES(301,101,'FV/PARTIAL','2026-09-01','2026-09-01','transfer',20,24.6,?)""",
        (now,),
    )
    db.execute(
        """INSERT INTO invoice_allocations(
               invoice_id,order_id,order_item_id,product_id,sku,qty,created_at
             ) VALUES(301,101,1001,1,'SKU-PARTIAL',2,?)""",
        (now,),
    )
    db.commit()
    db.close()

    pdf_calls = []

    def fake_packing_pdf(order, items, meta, invoice_pdf_path=""):
        path = tmp_path / "one-shared-packing-list.pdf"
        path.write_bytes(b"%PDF-1.4\nshared packing list")
        pdf_calls.append([dict(item) for item in items])
        return str(path)

    monkeypatch.setattr(backend, "generate_invoice_packing_list_pdf", fake_packing_pdf)
    with backend.app.test_request_context():
        backend._refresh_domain_route_context()
    return {"tmp_path": tmp_path, "pdf_calls": pdf_calls}


def _actor():
    return rbac.load_actor_context(
        rbac.AI_OWNER_ASSISTANT_ACTOR_ID,
        delegated_by_actor_id=rbac.BOOTSTRAP_OWNER_ACTOR_ID,
    )


def _preview():
    with backend.app.test_request_context():
        result = operations.execute_business_operation(
            _actor(), "orders.packing_list.preview", {"order_id": ROOT_ORDER_ID}
        )
    assert result.status == "SUCCESS", result
    return result.data


def _generate_args(preview):
    return {
        "order_id": ROOT_ORDER_ID,
        "expected_version": preview["state"]["expected_version"],
        "idempotency_key": str(uuid.uuid4()),
        "packing_scope_fingerprint": preview["preview"]["fingerprint"],
        "packing_items": preview["preview"]["approval_items"],
        "total_quantity": preview["preview"]["total_quantity"],
    }


def test_preview_reuses_ui_multi_order_selection_and_excludes_unavailable_or_other_customer(multi_order_flow):
    with backend.app.test_request_context():
        ui_service = backend.order_packing_list_download_admin_service(
            ROOT_ORDER_ID,
            request=fulfillment.request_view(method="GET"),
            session={},
            structured=True,
        )
    operation_preview = _preview()["preview"]

    assert operation_preview["candidate_order_ids"] == [101, 102, 103]
    assert operation_preview["order_ids"] == [101, 103]
    assert operation_preview["total_quantity"] == 5
    assert operation_preview["items"] == ui_service["items"]
    assert [(item["order_item_id"], item["pack_qty"]) for item in operation_preview["items"]] == [
        (1001, 3),
        (1003, 2),
    ]
    assert operation_preview["items"][0]["already_shipped_qty"] == 2
    assert all(item["available_to_package"] > 0 for item in operation_preview["items"])
    assert 1002 not in {item["order_item_id"] for item in operation_preview["items"]}
    assert 2001 not in {item["order_item_id"] for item in operation_preview["items"]}


def test_agent_multi_order_intent_previews_then_creates_one_approval(multi_order_flow):
    def request_approved_write(kwargs):
        output = json.loads(kwargs["input_items"][-1]["output"])
        proposal = output["preview"]
        assert proposal["order_ids"] == [101, 103]
        assert proposal["total_quantity"] == 5
        arguments = {
            "order_id": ROOT_ORDER_ID,
            "expected_version": output["state"]["expected_version"],
            "idempotency_key": "agent-multi-order-packing",
            "packing_scope_fingerprint": proposal["fingerprint"],
            "packing_items": proposal["approval_items"],
            "total_quantity": proposal["total_quantity"],
        }
        return runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall("generate", "orders.packing_list.generate", json.dumps(arguments)),
        ))

    provider = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(
            runtime.ToolCall(
                "preview",
                "orders.packing_list.preview",
                json.dumps({"order_id": ROOT_ORDER_ID}),
            ),
        )),
        request_approved_write,
        runtime.ProviderResponse(
            text=(
                "Propozycja: ZAM-2608271, SKU-PARTIAL × 3 oraz "
                "ZAM-2609151, SKU-AVAILABLE × 2; razem 5 sztuk. "
                "Jedna wspólna lista pakowa oczekuje na zatwierdzenie."
            )
        ),
    ])

    with backend.app.test_request_context():
        result = runtime.run_agent_turn(
            rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID),
            "Spakuj wszystko dostępne w jedną paczkę i jedną listę pakową.",
            provider,
        )

    assert result["status"] == "SUCCESS", result
    assert result["tool_calls"] == 2
    assert len(result["pending_approvals"]) == 1
    assert "ZAM-2608271" in result["message"]
    assert "ZAM-2609151" in result["message"]
    assert "razem 5 sztuk" in result["message"]
    assert not multi_order_flow["pdf_calls"]


def test_approved_generate_creates_one_batch_and_document_for_all_selected_orders(multi_order_flow):
    preview = _preview()
    args = _generate_args(preview)
    with backend.app.test_request_context():
        pending = operations.execute_business_operation(_actor(), "orders.packing_list.generate", args)
        assert pending.status == "PENDING_APPROVAL", pending
        approval_payload = json.loads(
            approvals.get_request_snapshot(pending.approval_id)["safe_payload"]
        )
        assert approval_payload["packing_items"] == preview["preview"]["approval_items"]
        assert approval_payload["total_quantity"] == 5
        approvals.approve_request(
            pending.approval_id,
            rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID),
        )
        result = operations.execute_business_operation(
            _actor(), "orders.packing_list.generate", args, approval_id=pending.approval_id
        )
        replay = operations.execute_business_operation(
            _actor(), "orders.packing_list.generate", args, approval_id=pending.approval_id
        )

    assert result.status == "SUCCESS", result
    assert replay.status == "SUCCESS", replay
    assert len(multi_order_flow["pdf_calls"]) == 1
    assert {(item["source_order_id"], item["id"], item["qty"]) for item in multi_order_flow["pdf_calls"][0]} == {
        (101, 1001, 3),
        (103, 1003, 2),
    }

    db = backend.conn()
    batches = [dict(row) for row in db.execute("SELECT * FROM packing_batches")]
    allocations = [dict(row) for row in db.execute(
        "SELECT batch_id,order_id,order_item_id,qty FROM packing_allocations ORDER BY order_id"
    )]
    documents = [dict(row) for row in db.execute(
        "SELECT order_id,kind,document_id,path FROM fulfillment_documents WHERE kind='packing_list' ORDER BY order_id"
    )]
    audit_count = db.execute(
        """SELECT COUNT(*) FROM internal_audit_log
           WHERE operation='orders.packing_list.generate' AND result='SUCCESS' AND approval_id=?""",
        (pending.approval_id,),
    ).fetchone()[0]
    db.close()

    assert len(batches) == 1
    assert [(row["order_id"], row["order_item_id"], row["qty"]) for row in allocations] == [
        (101, 1001, 3),
        (103, 1003, 2),
    ]
    assert [row["order_id"] for row in documents] == [101, 103]
    assert {row["document_id"] for row in documents} == {batches[0]["id"]}
    assert len({row["path"] for row in documents}) == 1
    assert audit_count >= 1
    assert approvals.get_request_snapshot(pending.approval_id)["status"] == "CONSUMED"


def test_multi_order_generate_rejects_changed_preview_scope(multi_order_flow):
    preview = _preview()
    args = _generate_args(preview)
    db = backend.conn()
    db.execute("UPDATE stock SET qty=1 WHERE product_id=3")
    db.commit()
    db.close()

    with backend.app.test_request_context():
        result = operations.execute_business_operation(_actor(), "orders.packing_list.generate", args)

    assert result.status == "CONFLICT"
    assert result.error_code == "PACKING_SCOPE_CONFLICT"
    assert not multi_order_flow["pdf_calls"]


def test_multi_order_generate_without_preview_scope_is_blocked_before_approval(multi_order_flow):
    preview = _preview()
    args = {
        "order_id": ROOT_ORDER_ID,
        "expected_version": preview["state"]["expected_version"],
        "idempotency_key": "missing-multi-order-scope",
    }

    with backend.app.test_request_context():
        result = operations.execute_business_operation(_actor(), "orders.packing_list.generate", args)

    assert result.status == "CONFLICT"
    assert result.error_code == "PACKING_SCOPE_CONFLICT"
    assert not result.approval_id
    assert not multi_order_flow["pdf_calls"]


def test_tool_descriptions_route_one_package_intent_to_preview_then_existing_generate(multi_order_flow):
    preview_definition = operations.OPERATION_REGISTRY["orders.packing_list.preview"]
    generate_definition = operations.OPERATION_REGISTRY["orders.packing_list.generate"]

    assert "jednej paczki" in preview_definition.description
    assert "wielu zamówień" in preview_definition.description
    assert "nie twórz shipment.merge" in generate_definition.description
    assert "packing_scope_fingerprint" in generate_definition.input_schema["properties"]
    assert "packing_items" in generate_definition.input_schema["properties"]

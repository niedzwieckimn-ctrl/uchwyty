import json
import logging
import socket
import urllib.request

import pytest

import app as backend
import agent_conversation as conversations
import agent_runtime as runtime
import business_operations as operations
import internal_rbac as rbac


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "agent.db"))
    monkeypatch.delenv("AI_OWNER_ACTOR_ID", raising=False)
    backend.init_db()
    backend.app.secret_key = "agent-test"
    db = backend.conn()
    now = backend.now_iso()
    for product_id, sku, model, name, qty in (
        (1, "CH101-BLK-160", "Avery 160", "Avery czarny 160", 24),
        (2, "CH101-BLK-128", "Avery 128", "Avery czarny 128", 8),
        (3, "CH032-AB-N29", "Leo", "Leo", 6),
    ):
        db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(?,?,?,?,?)", (product_id, sku, model, name, now))
        db.execute("INSERT INTO stock(product_id,qty) VALUES(?,?)", (product_id, qty))
    db.execute("INSERT INTO customers(id,name,address,phone,email,nip,language,price_list,created_at) VALUES(10,'AM Interiors','Testowa 1','','','', 'pl','pln',?)", (now,))
    db.execute("INSERT INTO orders(id,order_no,customer_id,customer_name,status,created_at,currency,price_list) VALUES(10,'ZAM-TEST-10',10,'AM Interiors','confirmed',?,'PLN','pln')", (now,))
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency) VALUES(10,10,'FVAT 8/09/2026','2026-09-01','2026-09-01','transfer','2026-09-08','AM Interiors','',100,123,?,'PLN')", (now,))
    db.execute("INSERT INTO invoice_meta(invoice_id,invoice_items_json,paid,paid_at,updated_at) VALUES(10,'[{\"sku\":\"CH101-BLK-160\",\"model\":\"Avery 160\",\"name\":\"Avery czarny 160\",\"qty\":2,\"unit_net_price\":100}]',0,NULL,?)", (now,))
    db.commit(); db.close()
    backend.AGENT_MODEL_PROVIDER = None
    yield
    backend.AGENT_MODEL_PROVIDER = None


def owner():
    return rbac.load_actor_context(rbac.BOOTSTRAP_OWNER_ACTOR_ID, request_id="human-request")


def tool(name, args, call_id="call-1"):
    return runtime.ProviderResponse(tool_calls=(runtime.ToolCall(call_id, name, json.dumps(args)),), model="fake-model")


def fake_search_answer(query, answer):
    return runtime.FakeModelProvider([tool("inventory.product.search", {"query": query}), runtime.ProviderResponse(text=answer, model="fake-model", input_tokens=10, output_tokens=5)])


def test_basic_question_uses_gate_and_returns_grounded_stock():
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery 160?", fake_search_answer("Avery 160", "Na magazynie mamy 24 sztuki Avery 160."))
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 1 and "24" in result["message"]
    db = backend.conn()
    row = db.execute("SELECT actor_id,operation,status FROM internal_operation_executions").fetchone()
    db.close()
    assert tuple(row) == (rbac.AI_OWNER_ASSISTANT_ACTOR_ID, "inventory.product.search", "SUCCESS")


def test_runtime_product_result_matches_direct_business_operation():
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    direct = operations.execute_business_operation(ai, "inventory.product.search", {"query": "Avery 160"})

    def verify_tool_output(kwargs):
        output = json.loads(kwargs["input_items"][-1]["output"])
        assert output == direct.data
        return runtime.ProviderResponse(text="Na magazynie mamy 24 sztuki Avery 160.")

    provider = runtime.FakeModelProvider([
        tool("inventory.product.search", {"query": "Avery 160"}), verify_tool_output,
    ])
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery 160?", provider)
    assert direct.status == result["status"] == "SUCCESS"


def test_runtime_overdue_result_matches_direct_business_operation():
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    direct = operations.execute_business_operation(ai, "invoices.overdue", {})

    def verify_tool_output(kwargs):
        output = json.loads(kwargs["input_items"][-1]["output"])
        assert output == direct.data
        return runtime.ProviderResponse(text="Znaleziono 1 zaległą fakturę.")

    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}), verify_tool_output,
    ])
    result = runtime.run_agent_turn(owner(), "Czy mam zaległe faktury?", provider)
    assert direct.status == result["status"] == "SUCCESS"


def test_runtime_overdue_zero_one_and_multiple_currencies_finish_without_model_failed():
    db = backend.conn()
    db.execute("UPDATE invoice_meta SET paid=1 WHERE invoice_id=10")
    db.commit(); db.close()
    zero = runtime.run_agent_turn(owner(), "Czy mam zaległe faktury?", runtime.FakeModelProvider([
        tool("invoices.overdue", {}), runtime.ProviderResponse(text="Nie ma zaległych faktur.")
    ]))
    assert zero["status"] == "SUCCESS"

    db = backend.conn(); now = backend.now_iso()
    db.execute("UPDATE invoice_meta SET paid=0 WHERE invoice_id=10")
    db.execute("INSERT INTO orders(id,order_no,customer_name,status,created_at,currency,price_list) VALUES(11,'ZAM-EUR','Euro Client','confirmed',?,'EUR','eu_eur')", (now,))
    db.execute("INSERT INTO invoices(id,order_id,invoice_no,issue_date,sell_date,payment_type,payment_to,buyer_name,buyer_tax_no,total_net,total_gross,created_at,currency) VALUES(11,11,'FV/EUR/11','2026-09-01','2026-09-01','transfer','2026-09-08','Euro Client','',40,50,?,'EUR')", (now,))
    db.execute("INSERT INTO invoice_meta(invoice_id,invoice_items_json,paid,updated_at) VALUES(11,'[]',0,?)", (now,))
    db.commit(); db.close()

    observed = {}
    def inspect_output(kwargs):
        encoded = kwargs["input_items"][-1]["output"]
        observed["payload"] = json.loads(encoded)
        assert len(encoded.encode("utf-8")) < runtime.MAX_TOOL_RESULT_BYTES
        return runtime.ProviderResponse(text="Są 2 zaległe faktury: 123 PLN i 50 EUR.")

    multiple = runtime.run_agent_turn(owner(), "Ile mam zaległych faktur?", runtime.FakeModelProvider([
        tool("invoices.overdue", {}), inspect_output,
    ]))
    assert multiple["status"] == "SUCCESS" and multiple["error_code"] == ""
    assert observed["payload"]["count"] == 2
    assert observed["payload"]["totals_by_currency"] == {
        "PLN": {"currency": "PLN", "invoice_count": 1, "amount_outstanding": 123.0},
        "EUR": {"currency": "EUR", "invoice_count": 1, "amount_outstanding": 50.0},
    }


@pytest.mark.parametrize(("query", "operation", "arguments", "answer"), [
    ("Jakie mam zaległe faktury?", "invoices.overdue", {"as_of": "2026-09-11"},
     "Masz 1 zaległą fakturę na 123 PLN, opóźnioną o 3 dni."),
    ("czy jakiś klient zalega z płatnością?", "invoices.overdue", {"as_of": "2026-09-11"},
     "1 klient zalega z 1 fakturą na 123 PLN, a najstarsza zaległość ma 3 dni."),
    ("jaka była ostatnia faktura?", "invoices.get", {"latest": True},
     "Ostatnia faktura to FVAT 8/09/2026 z 2026-09-01 na 123 PLN."),
    ("jaka jest pozycja ostatnio wystawionej faktury?", "invoices.get", {"latest": True},
     "Pozycja to Avery 160, SKU CH101-BLK-160, 2 sztuki po 100 PLN netto."),
    ("sprawdź fakturę FVAT 8/09/2026", "invoices.get", {"number": "FVAT 8/09/2026"},
     "Faktura FVAT 8/09/2026 ma wartość 123 PLN."),
    ("ile mam uchwytów w paczkach z Chin oprócz zaplanowanych", "china.orders.summary", {"scope": "active"},
     "W paczkach innych niż zaplanowane masz 935 sztuk."),
])
def test_production_questions_complete_endpoint_tool_grounding_and_final_response(
        monkeypatch, caplog, query, operation, arguments, answer):
    if operation == "china.orders.summary":
        db = backend.conn(); now = backend.now_iso()
        for package_id, status, qty in ((1, "planned", 120), (2, "ordered", 500), (3, "shipped", 435)):
            db.execute("INSERT INTO china_packages(id,package_no,status,created_at) VALUES(?,?,?,?)",
                       (package_id, f"PO-{package_id}", status, now))
            db.execute("INSERT INTO china_items(id,package_id,product_id,sku,qty,created_at) VALUES(?,?,?,?,?,?)",
                       (package_id, package_id, 1, "CH101-BLK-160", qty, now))
        db.commit(); db.close()
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool(operation, arguments), runtime.ProviderResponse(text=answer, model="fake-model"),
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"
    with caplog.at_level(logging.INFO):
        response = client.post("/api/internal/ai/chat", json={"message": query})
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "SUCCESS" and payload["error_code"] == ""
    assert "AI_TOOL_EXECUTION_END" in caplog.text
    assert "AI_FINAL_RESPONSE" in caplog.text
    assert "AI_RUNTIME_FAILURE" not in caplog.text
    db = backend.conn()
    execution = db.execute(
        "SELECT status,result_summary FROM internal_operation_executions WHERE operation=? ORDER BY created_at DESC LIMIT 1",
        (operation,),
    ).fetchone(); db.close()
    assert execution["status"] == "SUCCESS"
    result = json.loads(execution["result_summary"])
    if operation == "china.orders.summary":
        assert result["pieces_excluding_planned"] == 935
    if operation == "invoices.get":
        assert result["record"]["invoice_number"] == "FVAT 8/09/2026"
        assert result["record"]["items"][0]["sku"] == "CH101-BLK-160"


@pytest.mark.parametrize(("query", "operation", "arguments", "answer"), [
    ("mam jakieś zaległe faktury?", "invoices.overdue", {}, "Masz 1 zaległą fakturę na 123 PLN."),
    ("kto mi zalega?", "invoices.overdue", {}, "AM Interiors zalega z 1 fakturą na 123 PLN."),
    ("pokaż przeterminowane faktury", "invoices.overdue", {}, "Masz 1 przeterminowaną fakturę."),
    ("która faktura była ostatnia?", "invoices.get", {"latest": True}, "Ostatnia to FVAT 8/09/2026."),
    ("pokaż ostatnią fakturę", "invoices.get", {"latest": True}, "Ostatnia to FVAT 8/09/2026 na 123 PLN."),
    ("co było na ostatniej fakturze?", "invoices.get", {"latest": True}, "Były 2 sztuki Avery 160."),
    ("jakie pozycje miała ostatnia faktura?", "invoices.get", {"latest": True}, "Pozycja: 2 sztuki Avery 160."),
    ("znajdź FVAT8/09/2026", "invoices.get", {"number": "FVAT8/09/2026"}, "Znaleziono FVAT 8/09/2026."),
])
def test_invoice_language_variants_use_semantic_operations(query, operation, arguments, answer):
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool(operation, arguments), runtime.ProviderResponse(text=answer, model="fake-model"),
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"
    response = client.post("/api/internal/ai/chat", json={"message": query})
    assert response.status_code == 200
    assert response.get_json()["status"] == "SUCCESS"


@pytest.mark.parametrize(("query", "operation", "arguments", "answer"), [
    ("Ile mamy Avery 160?", "inventory.product.search", {"query": "Avery 160"},
     "Na magazynie mamy 24 sztuki Avery 160."),
    ("Mam niezapłacone faktury?", "invoices.overdue", {},
     "Znaleziono 1 niezapłaconą fakturę."),
    ("Znajdź klienta AM Interiors", "customers.search", {"query": "AM Interiors"},
     "Znaleziono klienta AM Interiors."),
])
def test_production_orchestration_reaches_real_business_operation_gate(query, operation, arguments, answer):
    observed = {}

    def verify_real_result(kwargs):
        observed["tool_choice_after_result"] = kwargs["tool_choice"]
        observed["business_result"] = json.loads(kwargs["input_items"][-1]["output"])
        return runtime.ProviderResponse(text=answer, model="fake-model")

    provider = runtime.FakeModelProvider([tool(operation, arguments), verify_real_result])
    result = runtime.run_agent_turn(owner(), query, provider)

    assert result["status"] == "SUCCESS"
    assert provider.calls[0]["tool_choice"] == "required"
    assert observed["tool_choice_after_result"] == "auto"
    assert observed["business_result"]["ok"] is True
    assert observed["business_result"]["count"] >= 1
    db = backend.conn()
    execution = db.execute(
        "SELECT operation,status FROM internal_operation_executions WHERE correlation_id=? ORDER BY started_at DESC LIMIT 1",
        (result["correlation_id"],),
    ).fetchone()
    db.close()
    assert tuple(execution) == (operation, "SUCCESS")


def test_unknown_product_does_not_invent_stock():
    result = runtime.run_agent_turn(owner(), "Ile mamy produktu Nieistniejący?", fake_search_answer("Nieistniejący", "Nie znalazłem takiego produktu."))
    assert result["status"] == "SUCCESS" and "nie znalazłem" in result["message"].lower()


def test_ambiguous_product_asks_for_variant():
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery?", fake_search_answer("Avery", "Mamy kilka wariantów Avery. Który rozstaw mam sprawdzić?"))
    assert result["status"] == "SUCCESS" and "który" in result["message"].lower()


def test_write_request_is_blocked_before_model_and_database_unchanged():
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text="done")])
    before = backend.conn().execute("SELECT qty FROM stock WHERE product_id=1").fetchone()[0]
    result = runtime.run_agent_turn(owner(), "Zmień stan Avery 160 na 100.", provider)
    after = backend.conn().execute("SELECT qty FROM stock WHERE product_id=1").fetchone()[0]
    assert result["status"] == "DENIED" and result["error_code"] == "READ_ONLY_RUNTIME"
    assert before == after and provider.calls == []


@pytest.mark.parametrize("name", ["database.execute", "inventory.adjust", "internal.test.change_setting"])
def test_invented_or_write_tool_is_denied(name):
    result = runtime.run_agent_turn(owner(), "Sprawdź produkt", runtime.FakeModelProvider([tool(name, {})]))
    assert result["status"] == "DENIED" and result["error_code"] == "TOOL_NOT_ALLOWED"


def test_prompt_injection_cannot_expose_write_tool():
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    assert not ai.approval_required_permissions
    assert all(not permission.endswith((".adjust", ".send", ".manage", ".create")) for permission in ai.effective_permissions)
    names = {item["name"] for item in runtime._tool_descriptors(ai)}
    assert names == {
        "inventory.product.get", "inventory.product.search", "inventory.summary", "orders.search", "orders.get",
        "invoices.search", "invoices.get", "invoices.overdue", "customers.search",
        "customers.get", "china.orders.summary", "business.sales.summary",
    }
    assert all(item["parameters"] and "permission" not in item for item in runtime._tool_descriptors(ai))
    assert all(item["strict"] is False for item in runtime._tool_descriptors(ai))


def test_existing_context_cannot_replace_fresh_tool_for_new_data_question():
    first = runtime.run_agent_turn(owner(), "Pokaż Avery", fake_search_answer("Avery", "Znalazłem Avery."))
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text="Masz 0 zaległych faktur.")])
    second = runtime.run_agent_turn(owner(), "Ile mam zaległych faktur?", provider,
                                    conversation_id=first["conversation_id"])
    assert second["status"] == "FAILED"
    assert second["error_code"] == "TOOL_REQUIRED_FOR_DATA"


def test_old_context_number_cannot_ground_unrelated_current_answer():
    first = runtime.run_agent_turn(owner(), "Pokaż Avery", fake_search_answer("Avery", "Znalazłem Avery."))
    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}, "overdue"),
        runtime.ProviderResponse(text="Masz 24 zaległe faktury."),
    ])
    second = runtime.run_agent_turn(owner(), "Ile mam zaległych faktur?", provider,
                                    conversation_id=first["conversation_id"])
    assert second["status"] == "FAILED"
    assert second["error_code"] == "MODEL_FAILED"


def test_tool_diagnostics_are_bounded_and_redact_customer_query(caplog):
    provider = runtime.FakeModelProvider([
        tool("customers.search", {"query": "Jan Kowalski"}),
        runtime.ProviderResponse(text="Nie znaleziono klienta."),
    ])
    with caplog.at_level(logging.INFO, logger="agent_runtime"):
        result = runtime.run_agent_turn(owner(), "Znajdź klienta Jan Kowalski", provider)
    assert result["status"] == "SUCCESS"
    assert "AI_TOOL_CALL" in caplog.text and "AI_TOOL_RESULT" in caplog.text
    assert "Jan Kowalski" not in caplog.text


def test_actor_spoof_fields_are_ignored_by_endpoint(monkeypatch):
    backend.AGENT_MODEL_PROVIDER = fake_search_answer("Avery 160", "Mamy 24 sztuki.")
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True; session["csrf_token"] = "csrf"
    response = client.post("/api/internal/ai/chat", json={"message": "Ile mamy Avery 160?", "actor_type": "OWNER", "roles": ["OWNER"], "permissions": ["*"]})
    assert response.status_code == 200
    db = backend.conn(); actor_id = db.execute("SELECT actor_id FROM internal_operation_executions").fetchone()[0]; db.close()
    assert actor_id == rbac.AI_OWNER_ASSISTANT_ACTOR_ID


def test_production_chat_endpoint_uses_runtime_and_real_business_operation_gate():
    provider = fake_search_answer("Avery 160", "Mamy 24 sztuki Avery 160.")
    backend.AGENT_MODEL_PROVIDER = provider
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    response = client.post("/api/internal/ai/chat", json={"message": "Ile mamy Avery 160?"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "SUCCESS" and payload["tool_calls"] == 1
    assert provider.calls[0]["tool_choice"] == "required"
    db = backend.conn()
    execution = db.execute(
        "SELECT operation,status FROM internal_operation_executions WHERE correlation_id=?",
        (payload["correlation_id"],),
    ).fetchone()
    db.close()
    assert tuple(execution) == ("inventory.product.search", "SUCCESS")


@pytest.mark.parametrize(("query", "operation", "arguments", "assert_fresh", "answer"), [
    ("Ile mamy Avery 160?", "inventory.product.search", {"query": "Avery 160"},
     lambda data: data["count"] >= 1 and data["candidates"][0]["stock"] == 31,
     "Na magazynie mamy 31 sztuk Avery 160."),
    ("Mam niezapłacone faktury?", "invoices.overdue", {},
     lambda data: data["count"] == 1,
     "Znaleziono 1 niezapłaconą fakturę."),
    ("Znajdź klienta AM Interiors", "customers.search", {"query": "AM Interiors"},
     lambda data: data["count"] == 1,
     "Znaleziono klienta AM Interiors."),
])
def test_chat_refreshes_stale_sqlite_before_real_business_operation(
        monkeypatch, query, operation, arguments, assert_fresh, answer):
    db = backend.conn()
    db.execute("UPDATE stock SET qty=0 WHERE product_id=1")
    db.execute("UPDATE invoice_meta SET paid=1 WHERE invoice_id=10")
    db.execute("UPDATE customers SET name='Nieaktualny klient' WHERE id=10")
    db.commit(); db.close()
    pulls = []

    def mocked_pull(*, force=False, delete_missing=True):
        pulls.append((force, delete_missing))
        fresh = backend.conn()
        fresh.execute("UPDATE stock SET qty=31 WHERE product_id=1")
        fresh.execute("UPDATE invoice_meta SET paid=0 WHERE invoice_id=10")
        fresh.execute("UPDATE customers SET name='AM Interiors' WHERE id=10")
        fresh.commit(); fresh.close()
        return {"ok": True, "tables": {
            "stock": {"status": "ok"}, "invoices": {"status": "ok"},
            "invoice_meta": {"status": "ok"}, "customers": {"status": "ok"},
        }}

    observed = {}
    def verify_fresh_output(kwargs):
        observed["data"] = json.loads(kwargs["input_items"][-1]["output"])
        return runtime.ProviderResponse(text=answer, model="fake-model")

    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(backend, "pull_shared_tables_from_supabase", mocked_pull)
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool(operation, arguments), verify_fresh_output,
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    response = client.post("/api/internal/ai/chat", json={"message": query})

    assert response.status_code == 200
    assert pulls == [(False, False)]
    assert observed["data"]["ok"] is True
    assert assert_fresh(observed["data"])


def test_chat_refresh_failure_keeps_sqlite_fallback(monkeypatch, caplog):
    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(
        backend, "pull_shared_tables_from_supabase",
        lambda **_kwargs: (_ for _ in ()).throw(TimeoutError("sensitive upstream detail")),
    )
    backend.AGENT_MODEL_PROVIDER = fake_search_answer(
        "Avery 160", "Na magazynie mamy 24 sztuki Avery 160."
    )
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    with caplog.at_level(logging.INFO, logger="app"):
        response = client.post("/api/internal/ai/chat", json={"message": "Ile mamy Avery 160?"})

    assert response.status_code == 200
    assert "AI_DATA_REFRESH" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "sensitive upstream detail" not in caplog.text


def test_chat_without_supabase_skips_refresh_and_does_not_crash(monkeypatch, caplog):
    monkeypatch.setattr(backend, "supabase_enabled", lambda: False)
    monkeypatch.setattr(
        backend, "pull_shared_tables_from_supabase",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("pull must not run")),
    )
    backend.AGENT_MODEL_PROVIDER = fake_search_answer(
        "Avery 160", "Na magazynie mamy 24 sztuki Avery 160."
    )
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    with caplog.at_level(logging.INFO, logger="app"):
        response = client.post("/api/internal/ai/chat", json={"message": "Ile mamy Avery 160?"})

    assert response.status_code == 200
    assert "AI_DATA_REFRESH" in caplog.text and "not_configured" in caplog.text


def test_chat_refresh_runs_once_for_multi_tool_turn(monkeypatch):
    pulls = []
    monkeypatch.setattr(backend, "supabase_enabled", lambda: True)
    monkeypatch.setattr(
        backend, "pull_shared_tables_from_supabase",
        lambda **kwargs: pulls.append(kwargs) or {"ok": True, "tables": {}},
    )
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        tool("inventory.product.search", {"query": "Avery 160"}, "product"),
        tool("invoices.overdue", {}, "overdue"),
        runtime.ProviderResponse(text="Mamy 24 sztuki Avery 160 i 1 zaległą fakturę."),
    ])
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    response = client.post(
        "/api/internal/ai/chat",
        json={"message": "Ile mamy Avery 160 i ile jest zaległych faktur?"},
    )

    assert response.status_code == 200
    assert response.get_json()["tool_calls"] == 2
    assert pulls == [{"force": False, "delete_missing": False}]


def test_read_only_filter_survives_accidental_write_permission(monkeypatch):
    db = backend.conn()
    db.execute("INSERT INTO internal_role_permissions(role_key,permission_key,decision) VALUES('AI_OWNER_ASSISTANT','internal.test.change_setting','ALLOW') ON CONFLICT(role_key,permission_key) DO UPDATE SET decision='ALLOW'")
    db.commit(); db.close()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    assert "internal.test.change_setting" not in {item["name"] for item in runtime._tool_descriptors(ai)}


def test_mark_invoice_paid_is_blocked_before_model():
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text="done")])
    result = runtime.run_agent_turn(owner(), "Oznacz fakturę FV/1 jako zapłaconą.", provider)
    assert result["status"] == "DENIED" and result["error_code"] == "READ_ONLY_RUNTIME"
    assert provider.calls == []


def test_tool_loop_limit_stops_provider():
    responses = [tool("inventory.product.search", {"query": f"Avery {i}"}, f"c-{i}") for i in range(7)]
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery?", runtime.FakeModelProvider(responses))
    assert result["status"] == "FAILED" and result["error_code"] == "TOOL_LIMIT_EXCEEDED"


def test_repeated_identical_tool_call_is_stopped():
    provider = runtime.FakeModelProvider([
        tool("inventory.product.search", {"query": "Avery"}, "c-1"),
        tool("inventory.product.search", {"query": "Avery"}, "c-2"),
    ])
    result = runtime.run_agent_turn(owner(), "Pokaż Avery", provider)
    assert result["status"] == "FAILED" and result["error_code"] == "REPEATED_TOOL_CALL"


def test_context_resolves_followup_to_candidate_id():
    first = runtime.run_agent_turn(owner(), "Pokaż Avery", fake_search_answer("Avery", "Znalazłem warianty Avery."))
    def verify_context(kwargs):
        encoded = json.dumps(kwargs["input_items"], ensure_ascii=False)
        assert "CONVERSATION_CONTEXT_DATA" in encoded and "CH101-BLK-160" in encoded
        return tool("inventory.product.get", {"product_id": 1})
    second_provider = runtime.FakeModelProvider([verify_context, runtime.ProviderResponse(text="Ten wariant ma 24 sztuki.")])
    second = runtime.run_agent_turn(owner(), "A 160?", second_provider, conversation_id=first["conversation_id"])
    assert second["status"] == "SUCCESS" and "24" in second["message"]


def test_natural_customer_followup_injects_id_through_real_execution_gate(monkeypatch):
    human = owner()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, request_id="ai")
    cid = conversations.open_conversation(human, ai)[0]
    conversations.update_context(cid, "customers.search", {"query": "Firma"},
                                 {"results": [{"id": 17, "name": "Firma"}]})
    captured = {}
    original = operations._HANDLERS["orders.search"]
    def handler(data, *args, **kwargs):
        captured.update(data)
        return {"ok": True, "results": [], "count": 0, "truncated": False}
    monkeypatch.setitem(operations._HANDLERS, "orders.search", handler)
    provider = runtime.FakeModelProvider([
        tool("orders.search", {}, "customer-orders"),
        runtime.ProviderResponse(text="Nie ma takich zamówień."),
    ])
    result = runtime.run_agent_turn(human, "ile ma zrealizowanych zamówień?", provider, conversation_id=cid)
    monkeypatch.setitem(operations._HANDLERS, "orders.search", original)
    assert result["status"] == "SUCCESS" and captured["customer_id"] == 17
    assert provider.calls[0]["tool_choice"] == "required"


def test_ordinal_followup_injects_selected_product_id_through_gate(monkeypatch):
    human = owner()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID, request_id="ai")
    cid = conversations.open_conversation(human, ai)[0]
    conversations.update_context(cid, "inventory.product.search", {"query": "Avery"}, {"candidates": [
        {"id": 3, "sku": "A-1", "model": "Avery"}, {"id": 8, "sku": "A-2", "model": "Avery"},
    ]})
    captured = {}
    original = operations._HANDLERS["inventory.product.get"]
    def handler(data, *args, **kwargs):
        captured.update(data)
        return {"ok": True, "id": 8, "sku": "A-2", "model": "Avery", "ean": None, "name": "Avery", "stock": 6}
    monkeypatch.setitem(operations._HANDLERS, "inventory.product.get", handler)
    result = runtime.run_agent_turn(human, "ten drugi", runtime.FakeModelProvider([
        tool("inventory.product.get", {}, "second-product"),
        runtime.ProviderResponse(text="Wybrany wariant ma 6 sztuk."),
    ]), conversation_id=cid)
    monkeypatch.setitem(operations._HANDLERS, "inventory.product.get", original)
    assert result["status"] == "SUCCESS" and captured["product_id"] == 8


def test_missing_referent_returns_clarification_without_tool():
    result = runtime.run_agent_turn(owner(), "A ten klient?", runtime.FakeModelProvider([
        runtime.ProviderResponse(text="Którego klienta masz na myśli?")]))
    assert result["status"] == "SUCCESS" and "którego" in result["message"].lower()


def test_multi_tool_and_partial_failure(monkeypatch):
    original = operations._HANDLERS["orders.search"]
    monkeypatch.setitem(operations._HANDLERS, "orders.search", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}, "overdue"),
        tool("orders.search", {"status": "in_progress"}, "orders"),
        runtime.ProviderResponse(text="Potwierdziłem brak zaległości, ale nie udało się sprawdzić aktywnych zamówień."),
    ])
    result = runtime.run_agent_turn(owner(), "Sprawdź zaległości i aktywne zamówienia", provider)
    monkeypatch.setitem(operations._HANDLERS, "orders.search", original)
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 2
    assert "nie udało" in result["message"]


def test_multiple_successful_read_tools_share_one_turn():
    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}, "overdue"),
        tool("orders.search", {"status": "in_progress"}, "orders"),
        runtime.ProviderResponse(text="Nie znaleziono zaległości ani aktywnych zamówień."),
    ])
    result = runtime.run_agent_turn(owner(), "Sprawdź zaległości i aktywne zamówienia", provider)
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 2
    db = backend.conn()
    rows = db.execute("SELECT correlation_id,after_state FROM internal_audit_log WHERE operation='agent.tool_result'").fetchall()
    db.close()
    assert len(rows) == 2 and {row["correlation_id"] for row in rows} == {result["correlation_id"]}
    assert all(result["conversation_id"] in row["after_state"] for row in rows)


def test_context_does_not_authorize_followup_write():
    first = runtime.run_agent_turn(owner(), "Pokaż Avery", fake_search_answer("Avery", "Znalazłem Avery."))
    provider = runtime.FakeModelProvider([runtime.ProviderResponse(text="done")])
    result = runtime.run_agent_turn(owner(), "Dobra, zmień jego stan na 1.", provider,
                                    conversation_id=first["conversation_id"])
    assert result["status"] == "DENIED" and result["error_code"] == "READ_ONLY_RUNTIME"
    assert provider.calls == []


@pytest.mark.parametrize("response", [TimeoutError("timeout"), ValueError("boom"), {"bad": True}])
def test_model_failures_are_controlled(response):
    provider = runtime.FakeModelProvider([response]) if response != {"bad": True} else runtime.FakeModelProvider([lambda _k: response])
    result = runtime.run_agent_turn(owner(), "Dzień dobry", provider)
    assert result["status"] == "FAILED" and result["message"] == "Asystent chwilowo nie może zakończyć odpowiedzi."
    assert "traceback" not in json.dumps(result).lower()


def test_tool_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setitem(operations._HANDLERS, "inventory.product.search", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db secret")))
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery?", runtime.FakeModelProvider([tool("inventory.product.search", {"query": "Avery"})]))
    assert result["status"] == "FAILED" and result["error_code"] == "HANDLER_FAILED"


def test_secret_and_ungrounded_number_are_blocked_or_redacted():
    secret = "sk-abcdefghijklmnopqrstuv"
    redacted = runtime.run_agent_turn(owner(), "Dzień dobry", runtime.FakeModelProvider([runtime.ProviderResponse(text=f"api_key={secret} {secret}")]))
    assert secret not in json.dumps(redacted) and "[REDACTED]" in redacted["message"]
    bad_number = runtime.run_agent_turn(owner(), "Ile mamy Avery 160?", fake_search_answer("Avery 160", "Mamy około 30 sztuk."))
    assert bad_number["status"] == "FAILED" and bad_number["error_code"] == "MODEL_FAILED"


def test_audit_correlates_human_ai_and_execution():
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery 160?", fake_search_answer("Avery 160", "Mamy 24 sztuki."))
    db = backend.conn()
    events = db.execute("SELECT operation,actor_id,correlation_id,after_state FROM internal_audit_log WHERE operation LIKE 'agent.%' AND operation NOT LIKE 'agent.conversation.%' ORDER BY occurred_at,audit_id").fetchall()
    db.close()
    assert {row[0] for row in events} >= {"agent.requested", "agent.tool_selected", "agent.tool_result", "agent.completed"}
    assert {row[2] for row in events} == {result["correlation_id"]}
    encoded = " ".join(row[3] or "" for row in events)
    assert rbac.BOOTSTRAP_OWNER_ACTOR_ID in encoded and rbac.AI_OWNER_ASSISTANT_ACTOR_ID in encoded


def test_real_adapter_uses_env_config_and_safe_responses_contract(monkeypatch):
    captured = {}
    class Response:
        headers = {"x-request-id": "req-provider-1"}
        def raise_for_status(self): pass
        def json(self):
            return {"id": "resp-1", "model": "configured-model", "usage": {"input_tokens": 3, "output_tokens": 2},
                    "output": [{"type": "function_call", "call_id": "call-1", "name": "inventory__product__search", "arguments": '{"query":"Avery"}'}]}
    def post(url, **kwargs):
        captured.update(url=url, **kwargs); return Response()
    monkeypatch.setattr(runtime.requests, "post", post)
    provider = runtime.OpenAIResponsesProvider(model="configured-model", api_key="test-key")
    reply = provider.complete(instructions="safe", input_items=[{"role": "user", "content": "test"}],
                              tools=[{"type": "function", "name": "inventory.product.search", "description": "search", "parameters": {"type": "object"}, "strict": True}],
                              previous_response_id="", timeout_seconds=7, tool_choice="required")
    assert reply.tool_calls[0].name == "inventory.product.search"
    assert captured["json"]["store"] is False and captured["json"]["parallel_tool_calls"] is False
    assert captured["json"]["tool_choice"] == "required"
    assert "previous_response_id" not in captured["json"]
    assert captured["json"]["tools"][0]["name"] == "inventory__product__search"
    assert captured["timeout"] == 7 and captured["headers"]["Authorization"] == "Bearer test-key"


def test_store_false_tool_flow_replays_output_without_previous_response_id(monkeypatch):
    requests_sent = []
    response_bodies = [
        {
            "id": "resp-not-stored",
            "model": "configured-model",
            "output": [{
                "id": "fc-1", "type": "function_call", "status": "completed",
                "call_id": "call-1", "name": "inventory__product__search",
                "arguments": '{"query":"Avery 160"}',
            }],
            "usage": {"input_tokens": 4, "output_tokens": 2},
        },
        {
            "id": "resp-final",
            "model": "configured-model",
            "output": [{
                "id": "msg-1", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "Na magazynie mamy 24 sztuki Avery 160."}],
            }],
            "usage": {"input_tokens": 8, "output_tokens": 6},
        },
    ]

    class Response:
        status_code = 200
        headers = {"x-request-id": "req-test"}

        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    def post(_url, **kwargs):
        requests_sent.append(kwargs["json"])
        return Response(response_bodies.pop(0))

    monkeypatch.setattr(runtime.requests, "post", post)
    provider = runtime.OpenAIResponsesProvider(model="configured-model", api_key="test-key")
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery 160?", provider)

    assert result["status"] == "SUCCESS" and "24" in result["message"]
    assert len(requests_sent) == 2
    assert all(request["store"] is False for request in requests_sent)
    assert [request["tool_choice"] for request in requests_sent] == ["required", "auto"]
    assert all("previous_response_id" not in request for request in requests_sent)
    second_input = requests_sent[1]["input"]
    assert second_input[0]["role"] == "user"
    assert second_input[1]["type"] == "function_call"
    assert second_input[1]["call_id"] == "call-1"
    assert second_input[2]["type"] == "function_call_output"
    assert second_input[2]["call_id"] == "call-1"
    assert '"stock":24' in second_input[2]["output"]


def test_provider_failure_is_diagnostic_in_log_but_endpoint_response_stays_safe(monkeypatch, caplog):
    secret = "sk-this-secret-must-never-reach-logs"
    user_message = "Ile mamy Avery 160? prywatny-marker"

    class ErrorResponse:
        status_code = 400
        headers = {"x-request-id": "req-provider-error"}

        def raise_for_status(self):
            raise runtime.requests.HTTPError("unsafe transport detail", response=self)

        def json(self):
            return {"error": {"code": "model_not_found", "message": f"{user_message} Authorization: Bearer {secret}"}}

    monkeypatch.setattr(runtime.requests, "post", lambda *_a, **_k: ErrorResponse())
    backend.AGENT_MODEL_PROVIDER = runtime.OpenAIResponsesProvider(model="configured-test-model", api_key=secret)
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"

    with caplog.at_level(logging.ERROR, logger="agent_runtime"):
        response = client.post("/api/internal/ai/chat", json={"message": user_message})

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["error_code"] == "MODEL_FAILED"
    assert payload["message"] == "Asystent chwilowo nie może zakończyć odpowiedzi."
    encoded_response = json.dumps(payload, ensure_ascii=False)
    assert secret not in encoded_response and user_message not in encoded_response

    log = caplog.text
    assert "AI_PROVIDER_FAILURE" in log
    assert '"exception_type": "HTTPError"' in log
    assert '"http_status": 400' in log
    assert '"api_error_code": "model_not_found"' in log
    assert '"model": "configured-test-model"' in log
    assert '"stage": "request"' in log
    assert '"safe_message": "OpenAI API zwróciło błąd HTTP 400."' in log
    assert secret not in log and user_message not in log
    assert "Authorization" not in log and "unsafe transport detail" not in log

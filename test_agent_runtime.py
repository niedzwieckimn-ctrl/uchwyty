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
    backend._rate_hits.clear()
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


def respond(text, model="fake-model", input_tokens=0, output_tokens=0):
    return runtime.ProviderResponse(text=text, model=model,input_tokens=input_tokens,output_tokens=output_tokens)


def clarify(text, model="fake-model"):
    return runtime.ProviderResponse(text=text,model=model)


def fake_search_answer(query, answer):
    return runtime.FakeModelProvider([tool("inventory.product.search", {"query": query}), respond(text=answer, model="fake-model", input_tokens=10, output_tokens=5)])


def test_orchestration_a_greeting_uses_respond_without_business_operation():
    provider = runtime.FakeModelProvider([respond("Cześć, w czym mogę pomóc?")])
    result = runtime.run_agent_turn(owner(), "cześć", provider)
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 0
    db = backend.conn(); count = db.execute("SELECT COUNT(*) FROM internal_operation_executions").fetchone()[0]; db.close()
    assert count == 0 and provider.calls[0]["tool_choice"] == "auto"


def test_orchestration_b_fresh_inventory_requires_business_tool_then_respond():
    provider = runtime.FakeModelProvider([
        tool("inventory.summary", {}), respond("Na magazynie są 38 sztuki."),
    ])
    result = runtime.run_agent_turn(owner(), "ile mam sztuk na magazynie?", provider)
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 1
    db = backend.conn(); operation = db.execute("SELECT operation,status FROM internal_operation_executions").fetchone(); db.close()
    assert tuple(operation) == ("inventory.summary", "SUCCESS")


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
        return respond(text="Na magazynie mamy 24 sztuki Avery 160.")

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
        return respond(text="Znaleziono 1 zaległą fakturę.")

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
        tool("invoices.overdue", {}), respond(text="Nie ma zaległych faktur.")
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
        return respond(text="Są 2 zaległe faktury: 123 PLN i 50 EUR.")

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
        tool(operation, arguments), respond(text=answer, model="fake-model"),
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
        tool(operation, arguments), respond(text=answer, model="fake-model"),
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
        return respond(text=answer, model="fake-model")

    provider = runtime.FakeModelProvider([tool(operation, arguments), verify_real_result])
    result = runtime.run_agent_turn(owner(), query, provider)

    assert result["status"] == "SUCCESS"
    assert provider.calls[0]["tool_choice"] == "auto"
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
    provider = runtime.FakeModelProvider([respond(text="done")])
    before = backend.conn().execute("SELECT qty FROM stock WHERE product_id=1").fetchone()[0]
    result = runtime.run_agent_turn(owner(), "Zmień stan Avery 160 na 100.", provider)
    after = backend.conn().execute("SELECT qty FROM stock WHERE product_id=1").fetchone()[0]
    assert result["status"] == "SUCCESS"
    assert before == after and len(provider.calls) == 1


@pytest.mark.parametrize("name", ["database.execute", "inventory.adjust", "internal.test.change_setting"])
def test_invented_or_write_tool_is_denied(name):
    result = runtime.run_agent_turn(owner(), "Sprawdź produkt", runtime.FakeModelProvider([tool(name, {})]))
    assert result["status"] == "DENIED" and result["error_code"] == "TOOL_NOT_ALLOWED"


def test_prompt_injection_cannot_expose_write_tool():
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    descriptors = runtime._tool_descriptors(ai)
    assert all(operations.OPERATION_REGISTRY[item['name']].read_only or item['name'] in operations.ORDER_WRITES | {runtime.MEMORY_WRITE} for item in descriptors)
    assert not any(item['name'].startswith('assistant.') for item in descriptors)
    assert all(item['strict'] is False for item in descriptors)


def test_tool_diagnostics_are_bounded_and_redact_customer_query(caplog):
    provider = runtime.FakeModelProvider([
        tool("customers.search", {"query": "Jan Kowalski"}),
        respond(text="Nie znaleziono klienta."),
    ])
    with caplog.at_level(logging.INFO, logger="agent_runtime"):
        result = runtime.run_agent_turn(owner(), "Znajdź klienta Jan Kowalski", provider)
    assert result["status"] == "SUCCESS"
    assert "AI_TOOL_EXECUTION_END" in caplog.text
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
    assert provider.calls[0]["tool_choice"] == "auto"
    db = backend.conn()
    execution = db.execute(
        "SELECT operation,status FROM internal_operation_executions WHERE correlation_id=?",
        (payload["correlation_id"],),
    ).fetchone()
    db.close()
    assert tuple(execution) == ("inventory.product.search", "SUCCESS")


def test_read_only_filter_survives_accidental_write_permission(monkeypatch):
    db = backend.conn()
    db.execute("INSERT INTO internal_role_permissions(role_key,permission_key,decision) VALUES('AI_OWNER_ASSISTANT','internal.test.change_setting','ALLOW') ON CONFLICT(role_key,permission_key) DO UPDATE SET decision='ALLOW'")
    db.commit(); db.close()
    ai = rbac.load_actor_context(rbac.AI_OWNER_ASSISTANT_ACTOR_ID)
    assert "internal.test.change_setting" not in {item["name"] for item in runtime._tool_descriptors(ai)}


def test_mark_invoice_paid_is_blocked_before_model():
    provider = runtime.FakeModelProvider([respond(text="done")])
    result = runtime.run_agent_turn(owner(), "Oznacz fakturę FV/1 jako zapłaconą.", provider)
    assert result["status"] == "SUCCESS"
    assert len(provider.calls) == 1


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


def _client():
    client = backend.app.test_client()
    with client.session_transaction() as session:
        session["admin_authenticated"] = True
        session["csrf_token"] = "csrf"
    return client


def test_missing_referent_returns_clarification_without_tool():
    result = runtime.run_agent_turn(owner(), "A ten klient?", runtime.FakeModelProvider([
        clarify("Którego klienta masz na myśli?")]))
    assert result["status"] == "SUCCESS" and "którego" in result["message"].lower()


def test_multi_tool_and_partial_failure(monkeypatch):
    original = operations._HANDLERS["orders.search"]
    monkeypatch.setitem(operations._HANDLERS, "orders.search", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}, "overdue"),
        tool("orders.search", {"status": "in_progress"}, "orders"),
        respond(text="Potwierdziłem brak zaległości, ale nie udało się sprawdzić aktywnych zamówień."),
    ])
    result = runtime.run_agent_turn(owner(), "Sprawdź zaległości i aktywne zamówienia", provider)
    monkeypatch.setitem(operations._HANDLERS, "orders.search", original)
    assert result["status"] == "SUCCESS" and result["tool_calls"] == 2
    assert "nie udało" in result["message"]


def test_multiple_successful_read_tools_share_one_turn():
    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}, "overdue"),
        tool("orders.search", {"status": "in_progress"}, "orders"),
        respond(text="Nie znaleziono zaległości ani aktywnych zamówień."),
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
    provider = runtime.FakeModelProvider([respond(text="done")])
    result = runtime.run_agent_turn(owner(), "Dobra, zmień jego stan na 1.", provider,
                                    conversation_id=first["conversation_id"])
    assert result["status"] == "SUCCESS"
    assert len(provider.calls) == 1


@pytest.mark.parametrize("response", [TimeoutError("timeout"), ValueError("boom"), {"bad": True}])
def test_model_failures_are_controlled(response):
    provider = runtime.FakeModelProvider([response]) if response != {"bad": True} else runtime.FakeModelProvider([lambda _k: response])
    result = runtime.run_agent_turn(owner(), "Dzień dobry", provider)
    assert result["status"] == "FAILED" and result["message"] == "Asystent chwilowo nie może zakończyć odpowiedzi."
    assert "traceback" not in json.dumps(result).lower()


def test_tool_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setitem(operations._HANDLERS, "inventory.product.search", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db secret")))
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery?", runtime.FakeModelProvider([tool("inventory.product.search", {"query": "Avery"})]))
    assert result["status"] == "FAILED" and result["error_code"] == "MODEL_FAILED"


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


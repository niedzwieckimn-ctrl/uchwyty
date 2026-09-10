import json
import logging
import socket
import urllib.request

import pytest

import app as backend
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
        return runtime.ProviderResponse(text="Nie znaleziono zaległych faktur.")

    provider = runtime.FakeModelProvider([
        tool("invoices.overdue", {}), verify_tool_output,
    ])
    result = runtime.run_agent_turn(owner(), "Czy mam zaległe faktury?", provider)
    assert direct.status == result["status"] == "SUCCESS"


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
        "inventory.product.get", "inventory.product.search", "orders.search", "orders.get",
        "invoices.search", "invoices.get", "invoices.overdue", "customers.search",
        "customers.get", "business.sales.summary",
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
                              previous_response_id="", timeout_seconds=7)
    assert reply.tool_calls[0].name == "inventory.product.search"
    assert captured["json"]["store"] is False and captured["json"]["parallel_tool_calls"] is False
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

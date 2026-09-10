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
    assert names == {"inventory.product.get", "inventory.product.search"}
    assert all(item["parameters"] and "permission" not in item for item in runtime._tool_descriptors(ai))


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


def test_tool_loop_limit_stops_provider():
    responses = [tool("inventory.product.search", {"query": "Avery"}, f"c-{i}") for i in range(6)]
    result = runtime.run_agent_turn(owner(), "Ile mamy Avery?", runtime.FakeModelProvider(responses))
    assert result["status"] == "FAILED" and result["error_code"] == "TOOL_LIMIT_EXCEEDED"


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
    events = db.execute("SELECT operation,actor_id,correlation_id,after_state FROM internal_audit_log WHERE operation LIKE 'agent.%' ORDER BY occurred_at,audit_id").fetchall()
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

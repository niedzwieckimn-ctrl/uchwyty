import json
import socket
import urllib.request

import pytest

import agent_runtime as runtime
import app as backend


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network forbidden")))
    monkeypatch.setattr(backend, "DB_PATH", str(tmp_path / "assistant-ui.db"))
    monkeypatch.setattr(backend, "maybe_pull_shared_from_supabase", lambda **_kwargs: None)
    monkeypatch.setattr(backend, "trigger_background_supabase_sync", lambda **_kwargs: None)
    backend.init_db()
    backend.app.secret_key = "assistant-ui-test"
    backend.AGENT_MODEL_PROVIDER = None
    test_client = backend.app.test_client()
    yield test_client
    backend.AGENT_MODEL_PROVIDER = None


def login(client):
    with client.session_transaction() as current:
        current["admin_authenticated"] = True
        current["csrf_token"] = "test-csrf"


def test_page_and_endpoint_require_internal_login(client):
    page = client.get("/ai-assistant")
    api = client.post("/api/internal/ai/chat", json={"message": "test"})
    assert page.status_code == 302 and "/login" in page.headers["Location"]
    assert api.status_code == 401


def test_internal_page_has_expected_navigation_and_conversation_controls(client):
    login(client)
    response = client.get("/ai-assistant")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert html.index(">Skan QR</a>") < html.index(">Asystent AI</a>") < html.index(">Ustawienia ▾</button>")
    assert "Zapytaj o dane firmy" in html
    assert "Ile mamy Avery 160?" in html
    assert "Znajdź CH101-BLK" in html
    assert "Ile mamy Andre?" in html
    assert "Asystent sprawdza dane..." in html
    assert "event.key === 'Enter' && !event.shiftKey" in html


def test_browser_request_contains_only_message_conversation_and_renders_only_safe_message(client):
    login(client)
    html = client.get("/ai-assistant").get_data(as_text=True)
    assert "fetch('/api/internal/ai/chat'" in html
    assert "JSON.stringify({message: message, conversation_id: conversationId})" in html
    assert "Nowa rozmowa" in html
    forbidden_request_fields = ("actor_id", "actor_type", "roles", "permissions", "risk_level", "AI_OWNER_ACTOR_ID")
    assert all(value not in html for value in forbidden_request_fields)
    forbidden_output_fields = ("execution_id", "correlation_id", "tool_calls", "raw JSON", "stacktrace", "system prompt")
    assert all(value not in html for value in forbidden_output_fields)
    assert "data.message" in html
    assert "OPENAI_API_KEY" not in html
    assert "localStorage" not in html and "sessionStorage" not in html


def test_fake_provider_smoke_returns_grounded_answer(client):
    db = backend.conn()
    now = backend.now_iso()
    db.execute("INSERT INTO products(id,sku,model,name,created_at) VALUES(1,'CH101-BLK-160','Avery 160','Avery czarny 160',?)", (now,))
    db.execute("INSERT INTO stock(product_id,qty) VALUES(1,24)")
    db.commit()
    db.close()
    backend.AGENT_MODEL_PROVIDER = runtime.FakeModelProvider([
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall("call-1", "inventory.product.search", json.dumps({"query": "Avery 160"})),), model="fake-model"),
        runtime.ProviderResponse(tool_calls=(runtime.ToolCall(
            "call-final", "assistant.respond",
            json.dumps({"message": "Na magazynie mamy 24 sztuki Avery 160.",
                        "numeric_claims": [{"kind": "stock", "value": 24}]}),
        ),), model="fake-model"),
    ])
    login(client)
    response = client.post("/api/internal/ai/chat", json={"message": "Ile mamy Avery 160?"})
    assert response.status_code == 200
    assert response.get_json()["message"] == "Na magazynie mamy 24 sztuki Avery 160."


def test_ui_has_controlled_error_mapping(client):
    login(client)
    html = client.get("/ai-assistant").get_data(as_text=True)
    assert "MODEL_NOT_CONFIGURED" in html
    assert "TOOL_REQUIRED_FOR_DATA" in html
    assert "data?.status === 'DENIED'" in html
    assert "response.status === 401" in html
    assert "response.status === 403" in html
    assert "controller.abort()" in html
    assert "Nie udało się teraz pobrać odpowiedzi. Spróbuj ponownie." in html
    assert "Sesja wygasła. Zaloguj się ponownie." in html


def test_assistant_ui_is_read_only(client):
    login(client)
    html = client.get("/ai-assistant").get_data(as_text=True)
    assert "trybie tylko do odczytu" in html
    assert "mikrofon" not in html.lower()
    assert "getUserMedia" not in html

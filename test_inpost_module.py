import inpost_module


def test_config_accepts_correct_values(monkeypatch):
    monkeypatch.setenv("INPOST_API_TOKEN", "x" * 60)
    monkeypatch.setenv("INPOST_ORGANIZATION_ID", "123456")
    cfg = inpost_module.config_summary()
    assert cfg["configured"] is True
    assert cfg["organization_id"] == "123456"
    assert cfg["token"] == "x" * 60
    assert cfg["swapped"] is False


def test_config_corrects_swapped_values(monkeypatch):
    monkeypatch.setenv("INPOST_API_TOKEN", "123456")
    monkeypatch.setenv("INPOST_ORGANIZATION_ID", "x" * 60)
    cfg = inpost_module.config_summary()
    assert cfg["configured"] is True
    assert cfg["organization_id"] == "123456"
    assert cfg["token"] == "x" * 60
    assert cfg["swapped"] is True


def test_config_accepts_token_misplaced_as_organization(monkeypatch):
    monkeypatch.delenv("INPOST_API_TOKEN", raising=False)
    monkeypatch.setenv("INPOST_ORGANIZATION_ID", "x" * 60)
    cfg = inpost_module.config_summary()
    assert cfg["configured"] is True
    assert cfg["token"] == "x" * 60
    assert cfg["organization_id"] == ""


def test_courier_shipment_requests_automatic_courier_pickup(monkeypatch):
    captured = {}
    monkeypatch.setattr(inpost_module, "organization_id", lambda: "123456")

    def fake_request(path, method="GET", payload=None, accept="application/json"):
        captured.update(path=path, method=method, payload=payload)
        return {"id": 987}

    monkeypatch.setattr(inpost_module, "_request", fake_request)
    inpost_module.create_courier_shipment(
        {
            "name": "Odbiorca", "street": "Testowa 1", "city": "Warszawa",
            "post_code": "00-001", "phone": "500600700", "email": "test@example.com",
        },
        {"length": 400, "width": 300, "height": 200, "weight": 5, "quantity": 1},
        "ZAM-TEST",
    )

    assert captured["path"] == "/organizations/123456/shipments"
    assert captured["method"] == "POST"
    assert captured["payload"]["service"] == "inpost_courier_standard"
    assert captured["payload"]["custom_attributes"] == {"sending_method": "dispatch_order"}

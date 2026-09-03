import json
import os
import re
import urllib.error
import urllib.request


class InPostError(RuntimeError):
    pass


_DISCOVERED_ORGANIZATION_ID = ""


def _looks_like_api_token(value):
    value = (value or "").strip()
    return len(value) >= 40 or value.count(".") >= 2


def config_summary():
    token = os.environ.get("INPOST_API_TOKEN", "").strip()
    organization_id = os.environ.get("INPOST_ORGANIZATION_ID", "").strip()
    # Częsty błąd konfiguracji na Renderze: token i numeryczny identyfikator
    # organizacji są wklejone do przeciwnych zmiennych. Rozpoznajemy go bez
    # ujawniania sekretu i używamy wartości we właściwych rolach.
    swapped = _looks_like_api_token(organization_id) and token.isdigit()
    if swapped:
        token, organization_id = organization_id, token
    elif not token and _looks_like_api_token(organization_id):
        # Pozwala naprawić konfigurację, w której jedyna dostępna wartość
        # (token) została wcześniej wpisana do pola organizacji.
        token, organization_id = organization_id, ""
    invalid = []
    if token and not _looks_like_api_token(token):
        invalid.append("INPOST_API_TOKEN")
    if organization_id and not organization_id.isdigit() and not _looks_like_api_token(organization_id):
        invalid.append("INPOST_ORGANIZATION_ID")
    return {
        # ID organizacji jest opcjonalne: gdy go brak, pobieramy je bezpiecznie
        # z /organizations przy użyciu tokenu.
        "configured": bool(token and "INPOST_API_TOKEN" not in invalid),
        "missing": [name for name, value in (
            ("INPOST_API_TOKEN", token),
        ) if not value] + invalid,
        "token": token,
        "organization_id": organization_id,
        "swapped": swapped,
        "sandbox": os.environ.get("INPOST_SANDBOX", "0").lower() in {"1", "true", "yes", "on"},
    }


def _base_url():
    custom = os.environ.get("INPOST_API_BASE_URL", "").strip().rstrip("/")
    if custom:
        return custom
    if config_summary()["sandbox"]:
        return "https://sandbox-api-shipx-pl.easypack24.net/v1"
    return "https://api-shipx-pl.easypack24.net/v1"


def _request(path, method="GET", payload=None, accept="application/json"):
    cfg = config_summary()
    if not cfg["configured"]:
        raise InPostError("Brak konfiguracji: " + ", ".join(cfg["missing"]))
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": "Bearer " + cfg["token"], "Accept": accept}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(_base_url() + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            data = response.read()
            if accept == "application/json":
                return json.loads(data.decode("utf-8"))
            return data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            details = json.loads(raw)
            message = details.get("message") or details.get("error") or raw
        except Exception:
            message = raw
        # Odpowiedź serwera może zawierać pełny URL z omyłkowo wklejonym
        # tokenem. Nigdy nie pokazujemy sekretów w panelu ani w logach.
        safe_message = str(message)
        for secret in {
            cfg.get("token", ""),
            os.environ.get("INPOST_API_TOKEN", "").strip(),
            os.environ.get("INPOST_ORGANIZATION_ID", "").strip(),
        }:
            if secret and _looks_like_api_token(secret):
                safe_message = safe_message.replace(secret, "[ukryto]")
        raise InPostError(f"InPost HTTP {exc.code}: {safe_message}") from exc
    except urllib.error.URLError as exc:
        raise InPostError(f"Brak połączenia z InPost: {exc.reason}") from exc


def organization_id():
    global _DISCOVERED_ORGANIZATION_ID
    cfg = config_summary()
    if cfg["organization_id"].isdigit():
        return cfg["organization_id"]
    if _DISCOVERED_ORGANIZATION_ID:
        return _DISCOVERED_ORGANIZATION_ID
    response = _request("/organizations")
    organizations = list(response.get("items") or []) if isinstance(response, dict) else []
    courier = [item for item in organizations if "inpost_courier_standard" in (item.get("services") or [])]
    candidates = courier or organizations
    if not candidates:
        raise InPostError("Token InPost nie ma dostępu do organizacji obsługującej przesyłki kurierskie")
    if len(candidates) > 1:
        raise InPostError("Token ma dostęp do kilku organizacji. Ustaw INPOST_ORGANIZATION_ID na Renderze.")
    resolved = str(candidates[0].get("id") or "").strip()
    if not resolved.isdigit():
        raise InPostError("InPost nie zwrócił poprawnego ID organizacji")
    _DISCOVERED_ORGANIZATION_ID = resolved
    return resolved


def split_street_building(value):
    text = (value or "").strip().rstrip(",")
    match = re.match(r"^(.*?)[,\s]+(\d+[A-Za-z0-9/\-]*)$", text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return text, "1"


def normalize_polish_phone(value):
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11 and digits.startswith("48"):
        digits = digits[2:]
    return digits


def create_courier_shipment(receiver, parcel, reference, service="inpost_courier_standard", options=None):
    resolved_organization_id = organization_id()
    options = options or {}
    street, building = split_street_building(receiver.get("street"))
    parcel_payload = {
        "dimensions": {
            "length": str(parcel["length"]), "width": str(parcel["width"]),
            "height": str(parcel["height"]), "unit": "mm",
        },
        "weight": {"amount": str(parcel["weight"]), "unit": "kg"},
        "is_non_standard": bool(parcel.get("non_standard")),
    }
    payload = {
        "receiver": {
            "company_name": receiver.get("name") or "Odbiorca",
            "email": receiver.get("email") or "",
            "phone": normalize_polish_phone(receiver.get("phone")),
            "address": {
                "street": street,
                "building_number": building,
                "city": receiver.get("city") or "",
                "post_code": receiver.get("post_code") or "",
                "country_code": "PL",
            },
        },
        "parcels": [dict(parcel_payload) for _ in range(max(1, min(99, int(parcel.get("quantity", 1)))))],
        "service": service,
        "reference": reference[:100],
        "comments": (parcel.get("comments") or "")[:100],
        "additional_services": list(options.get("additional_services") or []),
    }
    insurance = float(options.get("insurance") or 0)
    cod = float(options.get("cod") or 0)
    if insurance > 0:
        payload["insurance"] = {"amount": round(insurance, 2), "currency": "PLN"}
    if cod > 0:
        if insurance < cod:
            raise InPostError("Ubezpieczenie musi być co najmniej równe kwocie pobrania")
        payload["cod"] = {"amount": round(cod, 2), "currency": "PLN"}
    return _request(f"/organizations/{resolved_organization_id}/shipments", "POST", payload)


def get_label(shipment_id, label_format="pdf", label_type="A6"):
    fmt = label_format.lower()
    if fmt not in {"pdf", "zpl", "epl"}:
        raise InPostError("Nieobsługiwany format etykiety")
    return _request(
        f"/shipments/{int(shipment_id)}/label?format={fmt}&type={label_type}",
        accept="application/octet-stream",
    )


def get_shipment(shipment_id):
    return _request(f"/shipments/{int(shipment_id)}")


def create_dispatch_order(shipment_ids, pickup):
    resolved_organization_id = organization_id()
    clean_ids = list(dict.fromkeys(str(int(value)) for value in shipment_ids if value))
    if not clean_ids:
        raise InPostError("Wybierz co najmniej jedną przesyłkę")
    street, building = split_street_building(pickup.get("street"))
    phone = normalize_polish_phone(pickup.get("phone"))
    if not street or not pickup.get("city") or not pickup.get("post_code") or not phone:
        raise InPostError("Uzupełnij adres odbioru i telefon kontaktowy")
    payload = {
        "shipments": clean_ids,
        "comment": (pickup.get("comment") or "")[:100],
        "name": (pickup.get("name") or "Magazyn")[:100],
        "phone": phone,
        "email": (pickup.get("email") or "")[:100],
        "address": {
            "street": street,
            "building_number": building,
            "city": pickup.get("city") or "",
            "post_code": pickup.get("post_code") or "",
            "country_code": "PL",
        },
    }
    return _request(f"/organizations/{resolved_organization_id}/dispatch_orders", "POST", payload)

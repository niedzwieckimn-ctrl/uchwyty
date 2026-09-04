"""Minimalny klient oficjalnego 17TRACK Tracking API v2.4."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request


API_BASE = "https://api.17track.net/track/v2.4"
STATUS_RANK = {"planned": 0, "ordered": 1, "shipped": 2, "problem": 2, "arrived": 3}


class SeventeenTrackError(RuntimeError):
    pass


def enabled(flag: str, api_key: str) -> bool:
    return str(flag or "").strip().lower() in {"1", "true", "yes", "on"} and bool(str(api_key or "").strip())


def verify_webhook_signature(raw_body: bytes, signature: str, api_key: str) -> bool:
    if not raw_body or not signature or not api_key:
        return False
    expected = hashlib.sha256(raw_body + b"/" + api_key.encode("utf-8")).hexdigest()
    return hmac.compare_digest(expected.lower(), signature.strip().lower())


def map_package_status(value: str) -> str | None:
    key = "".join(ch for ch in str(value or "").lower() if ch.isalnum())
    if key in {"intransit", "outfordelivery", "availableforpickup", "pickup"}:
        return "shipped"
    if key in {"delivered"}:
        return "arrived"
    if key in {"deliveryfailure", "exception", "expired", "failed", "failure"}:
        return "problem"
    return None


def monotonic_status(current: str, suggested: str | None) -> str:
    current = str(current or "planned").strip().lower()
    if not suggested or current == "arrived":
        return current
    if suggested == "problem":
        return "problem" if STATUS_RANK.get(current, 0) < STATUS_RANK["arrived"] else current
    return suggested if STATUS_RANK.get(suggested, 0) >= STATUS_RANK.get(current, 0) else current


class SeventeenTrackClient:
    def __init__(self, api_key: str, timeout: int = 15):
        self.api_key = str(api_key or "").strip()
        self.timeout = max(3, min(int(timeout or 15), 30))
        if not self.api_key:
            raise SeventeenTrackError("Brak klucza API 17TRACK")

    def _post(self, endpoint: str, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(f"{API_BASE}/{endpoint}", data=body, method="POST")
        request.add_header("17token", self.api_key)
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise SeventeenTrackError(f"17TRACK HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SeventeenTrackError(f"17TRACK: {type(exc).__name__}") from exc
        if not isinstance(result, dict) or int(result.get("code", -1)) != 0:
            raise SeventeenTrackError(f"17TRACK odrzucił żądanie: {str(result)[:500]}")
        return result.get("data") or {}

    @staticmethod
    def _parcel(number: str, carrier_code=None):
        number = str(number or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9-]{5,50}", number):
            raise SeventeenTrackError("Nieprawidłowy format numeru trackingowego")
        row = {"number": number}
        if carrier_code not in (None, "", 0, "0"):
            row["carrier"] = int(carrier_code)
        return row

    def register(self, number: str, carrier_code=None):
        data = self._post("register", [self._parcel(number, carrier_code)])
        accepted = data.get("accepted") or []
        rejected = data.get("rejected") or []
        if accepted:
            return accepted[0]
        # "already registered" jest idempotentnym sukcesem.
        error = (rejected[0].get("error") or {}) if rejected else {}
        if int(error.get("code") or 0) == -18019901:
            return {**self._parcel(number, carrier_code), "already_registered": True}
        raise SeventeenTrackError(str(error.get("message") or "Nie udało się zarejestrować trackingu"))

    def get_tracking_info(self, parcels: list[dict]):
        if not parcels:
            return []
        data = self._post("gettrackinfo", parcels[:40])
        return data.get("accepted") or data.get("items") or data if isinstance(data, list) else data.get("accepted", [])

    def request_push(self, parcels: list[dict]):
        return self._post("push", parcels[:40])


def parse_tracking_payload(payload: dict) -> dict:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    track_info = data.get("track_info") or {}
    latest = track_info.get("latest_status") or data.get("latest_status") or {}
    status = latest.get("status") if isinstance(latest, dict) else latest
    substatus = latest.get("sub_status") if isinstance(latest, dict) else ""
    providers = (track_info.get("tracking") or {}).get("providers") or []
    provider = providers[-1] if providers else {}
    events = provider.get("events") or track_info.get("events") or []
    last_event = events[-1] if events else {}
    carrier = provider.get("provider") or data.get("carrier") or ""
    if isinstance(carrier, dict):
        carrier = carrier.get("name") or carrier.get("key") or ""
    return {
        "number": str(data.get("number") or "").strip(),
        "carrier_code": data.get("carrier"),
        "carrier": str(carrier or ""),
        "status": str(status or ""),
        "substatus": str(substatus or ""),
        "last_event": str(last_event.get("description") or last_event.get("location") or ""),
        "last_update": str(last_event.get("time_iso") or last_event.get("time_utc") or data.get("track_info_latest_time") or ""),
        "events": events[-8:],
        "eta": str((track_info.get("time_metrics") or {}).get("estimated_delivery_date") or ""),
    }

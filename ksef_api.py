# -*- coding: utf-8 -*-
"""
Minimalny klient KSeF API v2 do wysyłki pojedynczej faktury FA(3).

Sekrety nie są trzymane w kodzie. Ustaw je w Render jako Environment Variables:
  KSEF_ENV   = test | demo | prod
  KSEF_NIP   = 8661754935
  KSEF_TOKEN = token wygenerowany w KSeF
"""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.padding import PKCS7


BASE_URLS = {
    "test": "https://api-test.ksef.mf.gov.pl/v2",
    "te": "https://api-test.ksef.mf.gov.pl/v2",
    "demo": "https://api-demo.ksef.mf.gov.pl/v2",
    "tr": "https://api-demo.ksef.mf.gov.pl/v2",
    "prod": "https://api.ksef.mf.gov.pl/v2",
    "production": "https://api.ksef.mf.gov.pl/v2",
    "pr": "https://api.ksef.mf.gov.pl/v2",
}


@dataclass
class KsefConfig:
    env: str
    base_url: str
    nip: str
    token: str
    timeout: int = 45

    @property
    def missing(self) -> List[str]:
        missing: List[str] = []
        if not self.nip:
            missing.append("KSEF_NIP")
        if not self.token:
            missing.append("KSEF_TOKEN")
        return missing


class KsefApiError(RuntimeError):
    pass


def _cfg() -> KsefConfig:
    env = (os.getenv("KSEF_ENV") or "test").strip().lower()
    return KsefConfig(
        env=env,
        base_url=BASE_URLS.get(env, BASE_URLS["test"]),
        nip=(os.getenv("KSEF_NIP") or "").strip(),
        token=(os.getenv("KSEF_TOKEN") or "").strip(),
        timeout=int(os.getenv("KSEF_TIMEOUT", "45") or "45"),
    )


def ksef_config_summary() -> Dict[str, Any]:
    cfg = _cfg()
    return {
        "env": cfg.env,
        "base_url": cfg.base_url,
        "configured": not cfg.missing,
        "missing": cfg.missing,
    }


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _sha256_b64(data: bytes) -> str:
    return _b64(hashlib.sha256(data).digest())


def _public_key_from_der_b64(der_b64: str):
    raw = base64.b64decode(der_b64)
    try:
        cert = x509.load_der_x509_certificate(raw)
        return cert.public_key()
    except Exception:
        return serialization.load_der_public_key(raw)


def _rsa_oaep_sha256_encrypt(public_key, data: bytes) -> str:
    encrypted = public_key.encrypt(
        data,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=SHA256()),
            algorithm=SHA256(),
            label=None,
        ),
    )
    return _b64(encrypted)


def _aes_cbc_pkcs7_encrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    padder = PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def _raise_for_bad_response(response: requests.Response, step: str) -> None:
    if response.status_code < 400:
        return
    body = _json_or_text(response)
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("title") or body.get("message") or str(body)
        details = body.get("details")
        if details:
            detail += " " + "; ".join(map(str, details[:5] if isinstance(details, list) else [details]))
    else:
        detail = str(body)[:1000]
    raise KsefApiError(f"{step}: KSeF zwrócił HTTP {response.status_code}: {detail}")


def _headers(bearer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Error-Format": "problem-details",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _post(session: requests.Session, url: str, payload: Dict[str, Any], step: str, bearer: Optional[str] = None) -> Dict[str, Any]:
    response = session.post(url, json=payload, headers=_headers(bearer), timeout=_cfg().timeout)
    _raise_for_bad_response(response, step)
    return response.json() if response.content else {}


def _get(session: requests.Session, url: str, step: str, bearer: Optional[str] = None) -> Dict[str, Any]:
    response = session.get(url, headers=_headers(bearer), timeout=_cfg().timeout)
    _raise_for_bad_response(response, step)
    return response.json() if response.content else {}


def _get_public_keys(session: requests.Session, cfg: KsefConfig) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    response = session.get(f"{cfg.base_url}/security/public-key-certificates", headers=_headers(), timeout=cfg.timeout)
    _raise_for_bad_response(response, "Pobieranie kluczy publicznych KSeF")
    certs = response.json()
    token_key = None
    symmetric_key = None
    for cert in certs:
        usage = cert.get("usage") or []
        if "KsefTokenEncryption" in usage and token_key is None:
            token_key = cert
        if "SymmetricKeyEncryption" in usage and symmetric_key is None:
            symmetric_key = cert
    if not token_key:
        raise KsefApiError("KSeF nie zwrócił klucza do szyfrowania tokena.")
    if not symmetric_key:
        raise KsefApiError("KSeF nie zwrócił klucza do szyfrowania klucza sesji.")
    return token_key, symmetric_key


def _authenticate(session: requests.Session, cfg: KsefConfig, token_key: Dict[str, Any]) -> str:
    challenge_data = _post(session, f"{cfg.base_url}/auth/challenge", {}, "Pobieranie challenge KSeF")
    challenge = challenge_data.get("challenge")
    timestamp_ms = challenge_data.get("timestamp")
    if not challenge or timestamp_ms is None:
        raise KsefApiError(f"Nieprawidłowa odpowiedź challenge KSeF: {challenge_data}")

    public_key = _public_key_from_der_b64(token_key["certificate"])
    token_payload = f"{cfg.token}|{timestamp_ms}".encode("utf-8")
    encrypted_token = _rsa_oaep_sha256_encrypt(public_key, token_payload)

    init_payload = {
        "challenge": challenge,
        "contextIdentifier": {"type": "Nip", "value": cfg.nip},
        "encryptedToken": encrypted_token,
        "publicKeyId": token_key.get("publicKeyId"),
    }
    auth_init = _post(session, f"{cfg.base_url}/auth/ksef-token", init_payload, "Start uwierzytelniania KSeF")
    auth_ref = auth_init.get("referenceNumber")
    auth_token = (auth_init.get("authenticationToken") or {}).get("token")
    if not auth_ref or not auth_token:
        raise KsefApiError(f"Nieprawidłowa odpowiedź auth/ksef-token: {auth_init}")

    last_status: Dict[str, Any] = {}
    for _ in range(10):
        time.sleep(1)
        last_status = _get(session, f"{cfg.base_url}/auth/{auth_ref}", "Status uwierzytelniania KSeF", auth_token)
        code = ((last_status.get("status") or {}).get("code"))
        if code == 200:
            break
        if code and int(code) >= 400:
            details = (last_status.get("status") or {}).get("details") or []
            raise KsefApiError("Uwierzytelnianie KSeF nieudane: " + "; ".join(map(str, details or [last_status])))
    else:
        raise KsefApiError(f"KSeF nie zakończył uwierzytelniania w czasie oczekiwania: {last_status}")

    tokens = _post(session, f"{cfg.base_url}/auth/token/redeem", {}, "Pobieranie access token KSeF", auth_token)
    access_token = (tokens.get("accessToken") or {}).get("token")
    if not access_token:
        raise KsefApiError(f"KSeF nie zwrócił access token: {tokens}")
    return access_token


def _open_online_session(session: requests.Session, cfg: KsefConfig, access_token: str, symmetric_cert: Dict[str, Any], aes_key: bytes, iv: bytes) -> str:
    public_key = _public_key_from_der_b64(symmetric_cert["certificate"])
    encrypted_sym_key = _rsa_oaep_sha256_encrypt(public_key, aes_key)
    payload = {
        "formCode": {"systemCode": "FA (3)", "schemaVersion": "1-0E", "value": "FA"},
        "encryption": {
            "encryptedSymmetricKey": encrypted_sym_key,
            "initializationVector": _b64(iv),
            "publicKeyId": symmetric_cert.get("publicKeyId"),
        },
    }
    data = _post(session, f"{cfg.base_url}/sessions/online", payload, "Otwarcie sesji online KSeF", access_token)
    ref = data.get("referenceNumber")
    if not ref:
        raise KsefApiError(f"KSeF nie zwrócił numeru sesji: {data}")
    return ref


def _send_encrypted_invoice(session: requests.Session, cfg: KsefConfig, access_token: str, session_ref: str, xml_bytes: bytes, aes_key: bytes, iv: bytes) -> str:
    encrypted = _aes_cbc_pkcs7_encrypt(xml_bytes, aes_key, iv)
    payload = {
        "invoiceHash": _sha256_b64(xml_bytes),
        "invoiceSize": len(xml_bytes),
        "encryptedInvoiceHash": _sha256_b64(encrypted),
        "encryptedInvoiceSize": len(encrypted),
        "encryptedInvoiceContent": _b64(encrypted),
        "offlineMode": False,
    }
    data = _post(session, f"{cfg.base_url}/sessions/online/{session_ref}/invoices", payload, "Wysłanie faktury do KSeF", access_token)
    ref = data.get("referenceNumber")
    if not ref:
        raise KsefApiError(f"KSeF nie zwrócił numeru referencyjnego faktury: {data}")
    return ref


def _poll_invoice_number(session: requests.Session, cfg: KsefConfig, access_token: str, session_ref: str, invoice_ref: str) -> Tuple[str, Dict[str, Any]]:
    last_status: Dict[str, Any] = {}
    for _ in range(12):
        time.sleep(2)
        last_status = _get(
            session,
            f"{cfg.base_url}/sessions/{session_ref}/invoices/{invoice_ref}",
            "Status faktury w KSeF",
            access_token,
        )
        ksef_number = last_status.get("ksefNumber") or ""
        if ksef_number:
            return ksef_number, last_status
        status_code = ((last_status.get("status") or {}).get("code"))
        if status_code and int(status_code) >= 400:
            details = (last_status.get("status") or {}).get("details") or []
            raise KsefApiError("KSeF odrzucił fakturę: " + "; ".join(map(str, details or [last_status])))
    return "", last_status


def send_invoice_to_ksef(xml_text: str) -> Dict[str, Any]:
    cfg = _cfg()
    if cfg.missing:
        return {
            "ok": False,
            "message": "Brakuje konfiguracji KSeF w Render: " + ", ".join(cfg.missing),
            "env": cfg.env,
            "base_url": cfg.base_url,
        }

    try:
        xml_bytes = xml_text.encode("utf-8")
        aes_key = os.urandom(32)
        iv = os.urandom(16)

        with requests.Session() as session:
            token_key, symmetric_key = _get_public_keys(session, cfg)
            access_token = _authenticate(session, cfg, token_key)
            session_ref = _open_online_session(session, cfg, access_token, symmetric_key, aes_key, iv)
            invoice_ref = _send_encrypted_invoice(session, cfg, access_token, session_ref, xml_bytes, aes_key, iv)
            ksef_number, status = _poll_invoice_number(session, cfg, access_token, session_ref, invoice_ref)

        return {
            "ok": True,
            "message": "Faktura wysłana do KSeF.",
            "env": cfg.env,
            "base_url": cfg.base_url,
            "session_reference_number": session_ref,
            "invoice_reference_number": invoice_ref,
            "ksef_number": ksef_number,
            "raw_status": status,
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "env": cfg.env,
            "base_url": cfg.base_url,
        }

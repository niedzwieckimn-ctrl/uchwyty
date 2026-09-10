"""Additive internal identity and permission foundation.

The module deliberately does not authenticate customers and does not inspect
request-provided actor, role or permission names.  A request actor can currently
come only from the existing, signed Flask administrator session.  Future system
credentials and employee login can use ``load_actor_context`` after their own
authentication succeeds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import re
import sqlite3
import threading
import uuid
from typing import Callable, Iterable

from flask import abort, g, has_request_context, jsonify, request, session


ACTOR_HUMAN = "HUMAN"
ACTOR_SYSTEM = "SYSTEM"
ACTOR_AI_AGENT = "AI_AGENT"
ACTOR_TYPES = frozenset({ACTOR_HUMAN, ACTOR_SYSTEM, ACTOR_AI_AGENT})

ALLOW = "ALLOW"
DENY = "DENY"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
DECISIONS = frozenset({ALLOW, DENY, APPROVAL_REQUIRED})

BOOTSTRAP_OWNER_ACTOR_ID = "00000000-0000-4000-8000-000000000001"
BOOTSTRAP_OWNER_DISPLAY_NAME = "Bootstrap Owner"


PERMISSIONS = {
    "orders.read_full": "Pełny odczyt zamówień",
    "orders.fulfillment_read": "Ograniczony odczyt do realizacji",
    "orders.create": "Tworzenie zamówień",
    "orders.update": "Zmiana zamówień i pozycji",
    "orders.change_status": "Zmiana statusu zamówienia",
    "orders.cancel": "Anulowanie zamówienia",
    "orders.delete": "Fizyczne usunięcie zamówienia",
    "orders.send_confirmation": "Wysyłka potwierdzenia zamówienia",
    "orders.stock_audit": "Odczyt audytu odjęcia stanu",
    "orders.stock_repair": "Naprawa odjęcia stanu",
    "inventory.read": "Odczyt magazynu",
    "inventory.adjust": "Korekta stanu fizycznego",
    "inventory.catalog_manage": "Zarządzanie katalogiem produktów",
    "inventory.pricing_manage": "Zarządzanie cennikami",
    "inventory.assets_manage": "Zarządzanie zdjęciami produktów",
    "inventory.replenishment_read": "Odczyt analizy uzupełnień",
    "inventory.discrepancy_report": "Zgłoszenie rozbieżności magazynowej",
    "packing.read": "Odczyt procesu pakowania",
    "packing.prepare": "Przygotowanie listy pakowej",
    "packing.confirm": "Potwierdzenie kompletacji",
    "invoices.read": "Pełny odczyt faktur",
    "invoices.status_read": "Odczyt ograniczonego statusu faktury",
    "invoices.create_draft": "Przygotowanie projektu faktury",
    "invoices.publish": "Publikacja faktury i rozliczenie stanu",
    "invoices.modify": "Edycja lub regeneracja faktury",
    "invoices.reverse": "Wycofanie albo usunięcie faktury",
    "invoices.send_customer": "Wysłanie faktury klientowi",
    "ksef.read": "Odczyt danych KSeF",
    "ksef.validate": "Walidacja dokumentu KSeF",
    "ksef.send": "Wysłanie dokumentu do KSeF",
    "ksef.override_status": "Ręczne nadpisanie statusu KSeF",
    "customers.read": "Pełny odczyt klientów",
    "customers.shipping_read": "Odczyt danych potrzebnych do wysyłki",
    "customers.manage": "Tworzenie i aktualizacja klientów",
    "customers.delete": "Usunięcie klienta",
    "shipping.read": "Odczyt danych przesyłki",
    "shipping.create": "Utworzenie przesyłki",
    "shipping.label_read": "Pobranie etykiety",
    "shipping.request_pickup": "Zamówienie podjazdu kuriera",
    "shipping.mark_shipped": "Potwierdzenie wysłania",
    "shipping.track": "Sprawdzenie statusu przesyłki",
    "purchases.read": "Odczyt dostaw bez kosztów",
    "purchases.manage": "Zarządzanie dostawą i pozycjami",
    "purchases.tracking": "Obsługa trackingu dostawy",
    "purchases.receive": "Przyjęcie dostawy na magazyn",
    "purchases.costs": "Odczyt i zapis kosztów dostawy",
    "purchases.documents": "Obsługa dokumentów dostawy",
    "purchases.delete": "Usunięcie dostawy lub pozycji",
    "purchases.create_draft": "Przygotowanie propozycji zakupu",
    "payments.read": "Odczyt płatności i zaległości",
    "payments.remind": "Wysłanie przypomnienia o płatności",
    "payments.set_status": "Zmiana statusu płatności",
    "communications.test_retry": "Test i ponawianie komunikacji",
    "reports.read": "Odczyt raportów",
    "reports.search_manage": "Zarządzanie analityką wyszukiwań",
    "cashflow.read": "Odczyt cash flow",
    "cashflow.manage": "Zarządzanie cash flow",
    "system.company_manage": "Zarządzanie konfiguracją firmy",
    "system.sync": "Synchronizacja danych",
    "system.jobs": "Uruchamianie zadań systemowych",
    "system.users_manage": "Zarządzanie użytkownikami wewnętrznymi",
    "system.audit_read": "Odczyt centralnego audytu",
    "approvals.review": "Odczyt operacji oczekujących na akceptację",
    "approvals.decide": "Zatwierdzanie lub odrzucanie operacji",
}


ROLE_DEFINITIONS = {
    "OWNER": ("Owner / administrator", "HUMAN"),
    "MAGAZYN": ("Magazyn", "HUMAN"),
    "KSIEGOWOSC": ("Księgowość", "HUMAN"),
    "SPRZEDAZ_BOK": ("Sprzedaż / BOK", "HUMAN"),
    "LOGISTYKA": ("Logistyka", "HUMAN"),
    "ZAKUPY": ("Zakupy", "HUMAN"),
    "AI_WAREHOUSE": ("AI Warehouse", "AI_AGENT"),
    "AI_SALES": ("AI Sales", "AI_AGENT"),
    "AI_FINANCE": ("AI Finance", "AI_AGENT"),
    "AI_LOGISTICS": ("AI Logistics", "AI_AGENT"),
    "AI_PURCHASING": ("AI Purchasing", "AI_AGENT"),
    "AI_OWNER_ASSISTANT": ("AI Owner Assistant", "AI_AGENT"),
    "SYSTEM_KSEF_SCHEDULER": ("System KSeF scheduler", "SYSTEM"),
    "SYSTEM_PAYMENT_REMINDERS": ("System payment reminders", "SYSTEM"),
    "SYSTEM_INPOST_WEBHOOK": ("System InPost webhook", "SYSTEM"),
    "SYSTEM_INPOST_PICKUP_WORKER": ("System InPost pickup worker", "SYSTEM"),
    "SYSTEM_17TRACK_WEBHOOK": ("System 17TRACK webhook", "SYSTEM"),
    "SYSTEM_ORDER_EMAIL_RETRY": ("System order e-mail retry", "SYSTEM"),
    "SYSTEM_DATA_SYNC": ("System data sync", "SYSTEM"),
}


def _decisions(allow: Iterable[str] = (), approval: Iterable[str] = ()) -> dict[str, str]:
    result = {permission: ALLOW for permission in allow}
    result.update({permission: APPROVAL_REQUIRED for permission in approval})
    return result


ROLE_PERMISSION_DECISIONS = {
    "OWNER": _decisions(
        allow=set(PERMISSIONS) - {
            "orders.delete", "orders.stock_repair", "inventory.adjust",
            "inventory.pricing_manage", "invoices.publish", "invoices.modify",
            "invoices.reverse", "ksef.send", "ksef.override_status",
            "customers.delete", "purchases.receive", "purchases.delete",
            "payments.set_status", "system.company_manage",
        },
        approval={
            "orders.delete", "orders.stock_repair", "inventory.adjust",
            "inventory.pricing_manage", "invoices.publish", "invoices.modify",
            "invoices.reverse", "ksef.send", "ksef.override_status",
            "customers.delete", "purchases.receive", "purchases.delete",
            "payments.set_status", "system.company_manage",
        },
    ),
    "MAGAZYN": _decisions(
        allow={
            "orders.fulfillment_read", "orders.stock_audit", "inventory.read",
            "inventory.assets_manage", "inventory.replenishment_read",
            "inventory.discrepancy_report", "packing.read", "packing.prepare",
            "packing.confirm", "customers.shipping_read", "shipping.read",
            "shipping.create", "shipping.label_read", "shipping.request_pickup",
            "shipping.mark_shipped", "shipping.track", "purchases.read",
        },
        approval={"orders.change_status", "inventory.adjust", "purchases.receive"},
    ),
    "KSIEGOWOSC": _decisions(
        allow={
            "orders.read_full", "orders.stock_audit", "invoices.read",
            "invoices.status_read", "invoices.create_draft", "invoices.send_customer",
            "ksef.read", "ksef.validate", "customers.read", "purchases.costs",
            "purchases.documents", "payments.read", "payments.remind",
            "reports.read", "cashflow.read", "cashflow.manage",
        },
        approval={
            "orders.stock_repair", "inventory.pricing_manage", "invoices.publish",
            "invoices.modify", "invoices.reverse", "ksef.send",
            "ksef.override_status", "payments.set_status", "system.audit_read",
        },
    ),
    "SPRZEDAZ_BOK": _decisions(
        allow={
            "orders.read_full", "orders.fulfillment_read", "orders.create",
            "orders.update", "orders.change_status", "orders.send_confirmation",
            "invoices.status_read", "customers.read", "customers.shipping_read",
            "customers.manage", "shipping.read", "shipping.track", "reports.read",
            "reports.search_manage",
        },
        approval={"orders.cancel", "system.audit_read"},
    ),
    "LOGISTYKA": _decisions(
        allow={
            "orders.fulfillment_read", "packing.read", "customers.shipping_read",
            "shipping.read", "shipping.create", "shipping.label_read",
            "shipping.request_pickup", "shipping.mark_shipped", "shipping.track",
            "purchases.read", "purchases.tracking",
        },
        approval={"orders.change_status", "system.audit_read"},
    ),
    "ZAKUPY": _decisions(
        allow={
            "orders.fulfillment_read", "inventory.read", "inventory.replenishment_read",
            "purchases.read", "purchases.manage", "purchases.tracking",
            "purchases.costs", "purchases.documents", "purchases.create_draft",
            "reports.read",
        },
        approval={
            "inventory.catalog_manage", "purchases.receive", "purchases.delete",
            "system.audit_read",
        },
    ),
    "AI_WAREHOUSE": _decisions(allow={
        "orders.fulfillment_read", "inventory.read", "inventory.replenishment_read",
        "inventory.discrepancy_report", "packing.read", "packing.prepare",
        "customers.shipping_read", "shipping.read", "shipping.label_read", "shipping.track",
    }),
    "AI_SALES": _decisions(
        allow={
            "orders.read_full", "orders.fulfillment_read", "invoices.status_read",
            "customers.read", "customers.shipping_read", "shipping.read",
            "shipping.track", "reports.read",
        },
        approval={
            "orders.create", "orders.update", "orders.change_status",
            "orders.send_confirmation", "customers.manage", "reports.search_manage",
        },
    ),
    "AI_FINANCE": _decisions(
        allow={
            "orders.read_full", "invoices.read", "invoices.status_read",
            "invoices.create_draft", "ksef.read", "ksef.validate", "customers.read",
            "payments.read", "reports.read", "cashflow.read",
        },
        approval={
            "invoices.publish", "invoices.modify", "invoices.send_customer",
            "ksef.send", "payments.remind", "cashflow.manage",
        },
    ),
    "AI_LOGISTICS": _decisions(
        allow={
            "orders.fulfillment_read", "invoices.status_read", "customers.shipping_read",
            "packing.read", "shipping.read", "shipping.label_read", "shipping.track",
            "purchases.read", "purchases.tracking",
        },
        approval={"orders.change_status", "shipping.create", "shipping.request_pickup"},
    ),
    "AI_PURCHASING": _decisions(
        allow={
            "orders.fulfillment_read", "inventory.read", "inventory.replenishment_read",
            "purchases.read", "purchases.tracking", "purchases.documents",
            "purchases.create_draft", "reports.read",
        },
        approval={"purchases.manage", "purchases.costs"},
    ),
    "AI_OWNER_ASSISTANT": _decisions(allow={
        "orders.read_full", "orders.fulfillment_read", "orders.stock_audit",
        "inventory.read", "inventory.replenishment_read", "invoices.read",
        "invoices.status_read", "ksef.read", "customers.read", "shipping.read",
        "shipping.track", "purchases.read", "payments.read", "reports.read",
        "cashflow.read", "system.audit_read", "approvals.review",
    }),
    "SYSTEM_KSEF_SCHEDULER": _decisions(
        allow={"invoices.read", "invoices.send_customer", "ksef.read", "ksef.validate"},
        approval={"ksef.send"},
    ),
    "SYSTEM_PAYMENT_REMINDERS": _decisions(
        allow={"invoices.read", "payments.read", "payments.remind"}
    ),
    "SYSTEM_INPOST_WEBHOOK": _decisions(allow={
        "orders.fulfillment_read", "shipping.read", "shipping.track", "shipping.mark_shipped",
    }),
    "SYSTEM_INPOST_PICKUP_WORKER": _decisions(
        allow={"shipping.read"}, approval={"shipping.request_pickup"}
    ),
    "SYSTEM_17TRACK_WEBHOOK": _decisions(
        allow={"purchases.read", "purchases.tracking"}
    ),
    "SYSTEM_ORDER_EMAIL_RETRY": _decisions(
        allow={"orders.read_full", "orders.send_confirmation"}
    ),
    "SYSTEM_DATA_SYNC": _decisions(allow={"system.sync"}),
}


SYSTEM_ACTORS = {
    "10000000-0000-4000-8000-000000000001": "SYSTEM_KSEF_SCHEDULER",
    "10000000-0000-4000-8000-000000000002": "SYSTEM_PAYMENT_REMINDERS",
    "10000000-0000-4000-8000-000000000003": "SYSTEM_INPOST_WEBHOOK",
    "10000000-0000-4000-8000-000000000004": "SYSTEM_INPOST_PICKUP_WORKER",
    "10000000-0000-4000-8000-000000000005": "SYSTEM_17TRACK_WEBHOOK",
    "10000000-0000-4000-8000-000000000006": "SYSTEM_ORDER_EMAIL_RETRY",
    "10000000-0000-4000-8000-000000000007": "SYSTEM_DATA_SYNC",
}


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    actor_type: str
    display_name: str
    roles: tuple[str, ...] = field(default_factory=tuple)
    effective_permissions: frozenset[str] = field(default_factory=frozenset)
    approval_required_permissions: frozenset[str] = field(default_factory=frozenset)
    request_id: str = ""
    session_id: str = ""
    credential_id: str = ""
    delegated_by_actor_id: str = ""
    approval_id: str = ""
    reason: str = ""
    source: str = ""

    def __post_init__(self):
        if self.actor_type not in ACTOR_TYPES:
            raise ValueError(f"Nieobsługiwany actor_type: {self.actor_type}")

    def permission_decision(self, permission: str) -> str:
        if permission in self.approval_required_permissions:
            return APPROVAL_REQUIRED
        if permission in self.effective_permissions:
            return ALLOW
        return DENY


_connection_factory: Callable[[], sqlite3.Connection] | None = None
_configuration_lock = threading.Lock()


def configure(connection_factory: Callable[[], sqlite3.Connection]) -> None:
    if not callable(connection_factory):
        raise TypeError("connection_factory musi być wywoływalne")
    global _connection_factory
    with _configuration_lock:
        _connection_factory = connection_factory


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def initialize_schema(db: sqlite3.Connection) -> None:
    """Create and seed only independent internal RBAC tables."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS internal_actors(
            actor_id TEXT PRIMARY KEY,
            actor_type TEXT NOT NULL CHECK(actor_type IN ('HUMAN','SYSTEM','AI_AGENT')),
            display_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS internal_users(
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id TEXT NOT NULL UNIQUE REFERENCES internal_actors(actor_id) ON DELETE CASCADE,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS internal_roles(
            role_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            actor_category TEXT NOT NULL CHECK(actor_category IN ('HUMAN','SYSTEM','AI_AGENT')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS internal_permissions(
            permission_key TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS internal_role_permissions(
            role_key TEXT NOT NULL REFERENCES internal_roles(role_key) ON DELETE CASCADE,
            permission_key TEXT NOT NULL REFERENCES internal_permissions(permission_key) ON DELETE CASCADE,
            decision TEXT NOT NULL CHECK(decision IN ('ALLOW','DENY','APPROVAL_REQUIRED')),
            PRIMARY KEY(role_key, permission_key)
        );
        CREATE TABLE IF NOT EXISTS internal_actor_roles(
            actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE CASCADE,
            role_key TEXT NOT NULL REFERENCES internal_roles(role_key) ON DELETE CASCADE,
            assigned_at TEXT NOT NULL,
            PRIMARY KEY(actor_id, role_key)
        );
        CREATE TABLE IF NOT EXISTS internal_sessions(
            session_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS internal_service_credentials(
            credential_id TEXT PRIMARY KEY,
            actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id) ON DELETE CASCADE,
            secret_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_internal_actor_roles_role
            ON internal_actor_roles(role_key);
        CREATE INDEX IF NOT EXISTS idx_internal_sessions_actor
            ON internal_sessions(actor_id);
        CREATE INDEX IF NOT EXISTS idx_internal_credentials_actor
            ON internal_service_credentials(actor_id);
        """
    )
    now = _utc_now()
    db.executemany(
        """INSERT INTO internal_permissions(permission_key,description,created_at)
           VALUES(?,?,?) ON CONFLICT(permission_key) DO UPDATE SET description=excluded.description""",
        [(key, description, now) for key, description in sorted(PERMISSIONS.items())],
    )
    db.executemany(
        """INSERT INTO internal_roles(role_key,display_name,actor_category,created_at)
           VALUES(?,?,?,?) ON CONFLICT(role_key) DO UPDATE SET
           display_name=excluded.display_name,actor_category=excluded.actor_category""",
        [(key, values[0], values[1], now) for key, values in sorted(ROLE_DEFINITIONS.items())],
    )
    for role_key, decisions in ROLE_PERMISSION_DECISIONS.items():
        db.executemany(
            """INSERT INTO internal_role_permissions(role_key,permission_key,decision)
               VALUES(?,?,?) ON CONFLICT(role_key,permission_key) DO UPDATE SET decision=excluded.decision""",
            [(role_key, permission, decision) for permission, decision in sorted(decisions.items())],
        )

    _upsert_actor(db, BOOTSTRAP_OWNER_ACTOR_ID, ACTOR_HUMAN, BOOTSTRAP_OWNER_DISPLAY_NAME, now)
    _assign_role(db, BOOTSTRAP_OWNER_ACTOR_ID, "OWNER", now)
    for actor_id, role_key in SYSTEM_ACTORS.items():
        _upsert_actor(db, actor_id, ACTOR_SYSTEM, ROLE_DEFINITIONS[role_key][0], now)
        _assign_role(db, actor_id, role_key, now)
    db.commit()


def _upsert_actor(db, actor_id: str, actor_type: str, display_name: str, now: str) -> None:
    db.execute(
        """INSERT INTO internal_actors(actor_id,actor_type,display_name,status,created_at,updated_at)
           VALUES(?,?,?,'active',?,?) ON CONFLICT(actor_id) DO UPDATE SET
           actor_type=excluded.actor_type,display_name=excluded.display_name,updated_at=excluded.updated_at""",
        (actor_id, actor_type, display_name, now, now),
    )


def _assign_role(db, actor_id: str, role_key: str, now: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO internal_actor_roles(actor_id,role_key,assigned_at) VALUES(?,?,?)",
        (actor_id, role_key, now),
    )


def _request_id() -> str:
    supplied = request.headers.get("X-Request-ID", "") if has_request_context() else ""
    if supplied and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied):
        return supplied
    return str(uuid.uuid4())


def load_actor_context(
    actor_id: str,
    *,
    request_id: str = "",
    session_id: str = "",
    credential_id: str = "",
    delegated_by_actor_id: str = "",
    approval_id: str = "",
    reason: str = "",
    source: str = "internal",
) -> ActorContext | None:
    """Load an actor from the trusted backend catalogue; unknown actors fail closed."""
    if _connection_factory is None or not actor_id:
        return None
    db = _connection_factory()
    try:
        actor = db.execute(
            "SELECT actor_id,actor_type,display_name,status FROM internal_actors WHERE actor_id=?",
            (actor_id,),
        ).fetchone()
        if not actor or actor[3] != "active":
            return None
        roles = tuple(
            row[0] for row in db.execute(
                """SELECT ar.role_key
                   FROM internal_actor_roles ar
                   JOIN internal_roles r ON r.role_key=ar.role_key
                   WHERE ar.actor_id=? AND r.actor_category=? ORDER BY ar.role_key""",
                (actor_id, actor[1]),
            ).fetchall()
        )
        rows = db.execute(
            """SELECT rp.permission_key,rp.decision
               FROM internal_actor_roles ar
               JOIN internal_roles r ON r.role_key=ar.role_key
               JOIN internal_role_permissions rp ON rp.role_key=ar.role_key
               WHERE ar.actor_id=? AND r.actor_category=?""",
            (actor_id, actor[1]),
        ).fetchall()
    finally:
        db.close()

    decisions: dict[str, str] = {}
    precedence = {ALLOW: 1, APPROVAL_REQUIRED: 2, DENY: 3}
    for permission, decision in rows:
        if precedence[decision] > precedence.get(decisions.get(permission, ""), 0):
            decisions[permission] = decision
    return ActorContext(
        actor_id=actor[0],
        actor_type=actor[1],
        display_name=actor[2],
        roles=roles,
        effective_permissions=frozenset(k for k, v in decisions.items() if v == ALLOW),
        approval_required_permissions=frozenset(
            k for k, v in decisions.items() if v == APPROVAL_REQUIRED
        ),
        request_id=request_id or _request_id(),
        session_id=session_id,
        credential_id=credential_id,
        delegated_by_actor_id=delegated_by_actor_id,
        approval_id=approval_id,
        reason=reason,
        source=source,
    )


def bind_bootstrap_owner_session(flask_session) -> None:
    """Attach the transitional owner identity after legacy admin authentication."""
    flask_session["internal_actor_id"] = BOOTSTRAP_OWNER_ACTOR_ID
    flask_session["internal_session_id"] = str(uuid.uuid4())


def current_actor_context() -> ActorContext | None:
    """Resolve only trusted server-side authentication state.

    Request headers, query parameters and form fields are intentionally ignored.
    Existing administrator sessions without new keys remain compatible.
    """
    if not has_request_context():
        return None
    existing = getattr(g, "actor_context", None)
    if existing is not None:
        return existing
    if not session.get("admin_authenticated"):
        return None
    actor_id = session.get("internal_actor_id") or BOOTSTRAP_OWNER_ACTOR_ID
    context = load_actor_context(
        actor_id,
        request_id=_request_id(),
        session_id=session.get("internal_session_id", ""),
        source="legacy_admin_session",
    )
    if context is not None:
        g.actor_context = context
    return context


def require_permission(permission: str):
    """Protect an explicitly migrated endpoint; all other routes stay legacy."""
    if permission not in PERMISSIONS:
        raise ValueError(f"Nieznane permission: {permission}")

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            actor = current_actor_context()
            if actor is None:
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Brak wewnętrznej tożsamości"), 401
                abort(401)
            decision = actor.permission_decision(permission)
            if decision != ALLOW:
                if request.path.startswith("/api/"):
                    return jsonify(
                        ok=False,
                        error="Brak wymaganego uprawnienia",
                        permission=permission,
                        approval_required=(decision == APPROVAL_REQUIRED),
                    ), 403
                abort(403)
            return func(*args, **kwargs)

        wrapped.required_permission = permission
        return wrapped

    return decorator

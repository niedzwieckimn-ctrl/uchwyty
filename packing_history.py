"""Read-only access to immutable packing-list data.

The allocation snapshot is authoritative for new lists.  A verified PDF is an
optional human document, not a prerequisite for the agent read.  Older rows
without the structural snapshot may still use append-only audit evidence and a
verified historical PDF.  Current orders and order items are never used to
reconstruct history.
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from pypdf import PdfReader


OPERATION = "orders.packing_history.get"


class PackingHistoryError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


_QUANTITY_HEADERS = frozenset({"ILOŚĆ", "MENGE", "QTY", "CANT.", "Q.TÀ"})
_LINE_TOTAL_PREFIXES = ("Pozycje:", "Positionen:", "Items:", "Artículos:", "Articoli:")
_QTY_TOTAL_PREFIXES = ("Razem sztuk:", "Gesamtmenge:", "Total quantity:",
                       "Cantidad total:", "Quantità totale:")


def _not_verifiable(message: str) -> PackingHistoryError:
    return PackingHistoryError("HISTORY_DOCUMENT_NOT_VERIFIABLE", message)


def _key_text(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _source_order_number(value: Any, note: Any) -> str:
    order_number = " ".join(str(value or "").split())
    note_text = " ".join(str(note or "").split())
    if note_text and order_number.casefold().endswith((" " + note_text).casefold()):
        order_number = order_number[:-(len(note_text) + 1)].strip()
    return order_number


def _stable_key(order_number: Any, sku: Any, qty: Any) -> tuple[str, str, int]:
    return _key_text(order_number), _key_text(sku), int(qty)


def _clean_lines(page) -> list[str]:
    return [" ".join(line.split()) for line in (page.extract_text() or "").splitlines()
            if line.strip()]


def _footer_value(lines: list[str], prefixes: tuple[str, ...]) -> int | None:
    for line in lines:
        if any(line.startswith(prefix) for prefix in prefixes):
            match = re.search(r"(-?\d+)\s*$", line)
            return int(match.group(1)) if match else None
    return None


def _document_rows(content: bytes) -> tuple[list[dict[str, Any]], int | None, int | None]:
    try:
        reader = PdfReader(BytesIO(content))
    except Exception as exc:
        raise PackingHistoryError(
            "PACKING_HISTORY_DOCUMENT_UNREADABLE",
            "Nie udało się odczytać zapisanej historycznej listy pakowej.",
        ) from exc

    rows: list[dict[str, Any]] = []
    footer_lines: int | None = None
    footer_qty: int | None = None
    for page in reader.pages:
        lines = _clean_lines(page)
        footer_lines = _footer_value(lines, _LINE_TOTAL_PREFIXES) or footer_lines
        footer_qty = _footer_value(lines, _QTY_TOTAL_PREFIXES) or footer_qty
        header_indexes = [index for index, line in enumerate(lines)
                          if line.upper() in _QUANTITY_HEADERS]
        if not header_indexes:
            continue
        start = header_indexes[-1] + 1
        end = len(lines)
        for index in range(start, len(lines)):
            if (any(lines[index].startswith(prefix) for prefix in _LINE_TOTAL_PREFIXES)
                    or any(lines[index].startswith(prefix) for prefix in _QTY_TOTAL_PREFIXES)):
                end = index
                break
        table = lines[start:end]
        if len(table) % 5:
            raise PackingHistoryError(
                "PACKING_HISTORY_DOCUMENT_UNREADABLE",
                "Zapisana historyczna lista pakowa ma nieobsługiwany układ danych.",
            )
        for offset in range(0, len(table), 5):
            line_no, sku, model_name, source, qty_text = table[offset:offset + 5]
            if not line_no.isdigit() or not qty_text.isdigit():
                raise PackingHistoryError(
                    "PACKING_HISTORY_DOCUMENT_UNREADABLE",
                    "Zapisana historyczna lista pakowa ma nieprawidłowy układ pozycji.",
                )
            expected_line = len(rows) + 1
            if int(line_no) != expected_line:
                raise PackingHistoryError(
                    "PACKING_HISTORY_DOCUMENT_UNREADABLE",
                    "Nie udało się jednoznacznie odtworzyć kolejności pozycji listy pakowej.",
                )
            source_parts = source.split(" · ", 1)
            rows.append({
                "order_number": source_parts[0].strip(),
                "sku": sku,
                "model_name": model_name,
                "note": source_parts[1].strip() if len(source_parts) == 2 else "",
                "packed_qty": int(qty_text),
            })
    return rows, footer_lines, footer_qty


def _selector(data: Mapping[str, Any]) -> tuple[str, Any]:
    supplied = []
    for key in ("batch_id", "order_id", "order_number", "customer_id", "customer"):
        if data.get(key) not in (None, ""):
            supplied.append((key, data[key]))
    for key in ("today", "latest"):
        if data.get(key) is True:
            supplied.append((key, True))
    if len(supplied) != 1:
        raise PackingHistoryError(
            "PACKING_HISTORY_SELECTOR_REQUIRED",
            "Wskaż dokładnie jeden batch, zamówienie albo klienta historycznej listy pakowej.",
        )
    return supplied[0]


def _json_object(value: Any) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _snapshot_allocation_keys(allocations: list[dict[str, Any]]):
    snapshot_fields = ("order_number_snapshot", "sku_snapshot", "customer_name_snapshot")
    has_any_snapshot = any(any(str(row.get(field) or "").strip() for field in snapshot_fields)
                           for row in allocations)
    if not has_any_snapshot:
        return None
    keyed = []
    seen_keys = set()
    customers = set()
    customer = None
    for allocation in allocations:
        order_number = " ".join(str(allocation.get("order_number_snapshot") or "").split())
        sku = " ".join(str(allocation.get("sku_snapshot") or "").split())
        customer_name = " ".join(str(allocation.get("customer_name_snapshot") or "").split())
        if not order_number or not sku or not customer_name:
            raise _not_verifiable("Snapshot alokacji historycznego batcha jest niekompletny.")
        try:
            qty = int(allocation["qty"])
            customer_id = (int(allocation["customer_id_snapshot"])
                           if allocation.get("customer_id_snapshot") not in (None, "") else None)
        except (TypeError, ValueError) as exc:
            raise _not_verifiable("Snapshot alokacji historycznego batcha ma nieprawidłowe dane.") from exc
        if qty <= 0:
            raise _not_verifiable("Historyczna alokacja ma nieprawidłową ilość.")
        customer_email = " ".join(str(allocation.get("customer_email_snapshot") or "").split())
        identity = (customer_id, _key_text(customer_name), _key_text(customer_email))
        customers.add(identity)
        customer = {"id": customer_id, "name": customer_name, "email": customer_email}
        key = _stable_key(order_number, sku, qty)
        if key in seen_keys:
            raise _not_verifiable(
                "Historyczne pozycje nie mają jednoznacznego klucza order_number, SKU i qty.")
        seen_keys.add(key)
        keyed.append((allocation, key))
    if len(customers) != 1:
        raise _not_verifiable("Snapshot historycznego batcha nie identyfikuje klienta jednoznacznie.")
    return keyed, customer


def _audit_allocation_keys(db, batch: Mapping[str, Any], allocations: list[dict[str, Any]]):
    """Recover an older BO-created batch from append-only approval/audit evidence."""
    try:
        rows = db.execute(
            """SELECT approval_id,correlation_id,before_state,after_state
                 FROM internal_audit_log
                WHERE operation=? AND result='SUCCESS'
                ORDER BY occurred_at DESC,rowid DESC""",
            ("orders.packing_list.generate",),
        ).fetchall()
    except Exception as exc:
        raise _not_verifiable("Brak niemodyfikowalnego snapshotu historycznego batcha.") from exc
    committed = None
    for row in rows:
        after = _json_object(row["after_state"])
        if (after and after.get("phase") == "domain_committed"
                and int(after.get("batch_id") or 0) == int(batch["id"])):
            committed = dict(row)
            break
    if not committed or not str(committed.get("approval_id") or "").strip():
        raise _not_verifiable("Brak niemodyfikowalnego snapshotu historycznego batcha.")
    approval_id = str(committed["approval_id"])
    execution = db.execute(
        """SELECT status,operation,correlation_id FROM internal_operation_executions
             WHERE approval_id=? ORDER BY updated_at DESC LIMIT 1""",
        (approval_id,),
    ).fetchone()
    if (execution is None or execution["status"] != "SUCCESS"
            or execution["operation"] != "orders.packing_list.generate"
            or str(execution["correlation_id"] or "") != str(committed["correlation_id"] or "")):
        raise _not_verifiable("Wykonanie powiązane z historycznym batchem nie ma potwierdzonego SUCCESS.")
    approval_audit = db.execute(
        """SELECT after_state FROM internal_audit_log
             WHERE operation='approval.requested' AND approval_id=?
             ORDER BY occurred_at DESC,rowid DESC LIMIT 1""",
        (approval_id,),
    ).fetchone()
    approval_state = _json_object(approval_audit["after_state"]) if approval_audit else None
    payload = approval_state.get("safe_payload") if approval_state else None
    items = payload.get("packing_items") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or not items:
        raise _not_verifiable("Approval nie zawiera kompletnego snapshotu pozycji historycznego batcha.")
    by_item = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise _not_verifiable("Snapshot approval zawiera nieprawidłową pozycję.")
        try:
            identity = (int(item.get("order_id") or 0), int(item.get("order_item_id") or 0))
            qty = int(item.get("quantity") or 0)
        except (TypeError, ValueError) as exc:
            raise _not_verifiable("Snapshot approval ma nieprawidłowy identyfikator lub ilość.") from exc
        if identity[0] <= 0 or identity[1] <= 0 or qty <= 0 or identity in by_item:
            raise _not_verifiable("Snapshot approval nie identyfikuje pozycji jednoznacznie.")
        by_item[identity] = dict(item)
    allocation_identities = {
        (int(row["order_id"]), int(row["order_item_id"])) for row in allocations
    }
    if set(by_item) != allocation_identities:
        raise _not_verifiable("Snapshot approval nie odpowiada dokładnie alokacjom packing batcha.")
    keyed = []
    seen_keys = set()
    for allocation in allocations:
        identity = (int(allocation["order_id"]), int(allocation["order_item_id"]))
        item = by_item[identity]
        qty = int(allocation["qty"] or 0)
        if int(item.get("quantity") or 0) != qty:
            raise _not_verifiable("Ilości snapshotu approval nie odpowiadają alokacjom packing batcha.")
        order_number = " ".join(str(item.get("order_number") or "").split())
        sku = " ".join(str(item.get("sku") or "").split())
        if not order_number or not sku:
            raise _not_verifiable("Snapshot approval nie zawiera stabilnego klucza pozycji.")
        key = _stable_key(order_number, sku, qty)
        if key in seen_keys:
            raise _not_verifiable(
                "Historyczne pozycje nie mają jednoznacznego klucza order_number, SKU i qty.")
        seen_keys.add(key)
        keyed.append((allocation, key))
    before = _json_object(committed.get("before_state")) or {}
    order = before.get("order") if isinstance(before.get("order"), Mapping) else {}
    customer_name = " ".join(str(order.get("customer_name") or "").split())
    if not customer_name:
        raise _not_verifiable("Historyczny audit nie identyfikuje klienta batcha.")
    try:
        customer_id = int(order["customer_id"]) if order.get("customer_id") not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise _not_verifiable("Historyczny audit ma nieprawidłową tożsamość klienta.") from exc
    customer = {
        "id": customer_id,
        "name": customer_name,
        "email": " ".join(str(order.get("customer_email") or "").split()),
    }
    return keyed, customer


def _historical_allocation_keys(db, batch: Mapping[str, Any], allocations: list[dict[str, Any]]):
    snapshot = _snapshot_allocation_keys(allocations)
    return snapshot if snapshot is not None else _audit_allocation_keys(db, batch, allocations)


def _batch_allocations(db, batch_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(
        "SELECT * FROM packing_allocations WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()]


def _document_records(db, batch_id: int) -> list[dict[str, Any]]:
    """Prefer immutable history; use the old current pointer for legacy DBs."""
    try:
        rows = db.execute(
            """SELECT order_id,document_id,path,file_hash,created_at
                 FROM fulfillment_document_history
                WHERE kind='packing_list' AND document_id=?
                ORDER BY created_at DESC,order_id""",
            (batch_id,),
        ).fetchall()
    except Exception:
        rows = []
    if not rows:
        rows = db.execute(
            """SELECT order_id,document_id,path,file_hash,created_at
                 FROM fulfillment_documents
                WHERE kind='packing_list' AND document_id=?
                ORDER BY created_at DESC,order_id""",
            (batch_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _verified_document(records: list[dict[str, Any]], allocation_order_ids: set[int]):
    if not records:
        return None, False
    document_order_ids = {int(row["order_id"]) for row in records}
    identities = {
        (int(row["document_id"]), str(row.get("path") or ""), str(row.get("file_hash") or ""))
        for row in records
    }
    if document_order_ids != allocation_order_ids or len(identities) != 1:
        return None, False
    document = records[0]
    path = Path(str(document.get("path") or ""))
    expected_hash = str(document.get("file_hash") or "").strip()
    if not expected_hash:
        return document, False
    try:
        content = path.read_bytes()
    except (OSError, ValueError):
        return document, False
    return document, bool(content and sha256(content).hexdigest() == expected_hash)


def _select_batch(db, data: Mapping[str, Any]):
    selector, value = _selector(data)
    if selector == "batch_id":
        return db.execute("SELECT * FROM packing_batches WHERE id=?", (int(value),)).fetchone(), None
    if selector == "order_id":
        return db.execute(
            """SELECT pb.* FROM packing_batches pb
                 WHERE pb.root_order_id=?
                    OR EXISTS (SELECT 1 FROM packing_allocations pa
                                WHERE pa.batch_id=pb.id AND pa.order_id=?)
                 ORDER BY pb.created_at DESC,pb.id DESC LIMIT 1""",
            (int(value), int(value)),
        ).fetchone(), None
    if selector == "order_number":
        requested = _key_text(value)
        matches = []
        unverifiable_batches = 0
        for raw_batch in db.execute(
                "SELECT * FROM packing_batches ORDER BY created_at DESC,id DESC").fetchall():
            batch = dict(raw_batch)
            allocations = _batch_allocations(db, int(batch["id"]))
            if not allocations:
                continue
            try:
                evidence = _historical_allocation_keys(db, batch, allocations)
            except PackingHistoryError:
                unverifiable_batches += 1
                continue
            if any(key[0] == requested for _allocation, key in evidence[0]):
                matches.append((raw_batch, evidence))
        if matches:
            return matches[0]
        if unverifiable_batches:
            raise _not_verifiable(
                "Nie można wiarygodnie ustalić historycznego batcha dla tego numeru zamówienia.")
        return None, None
    if selector in {"customer_id", "customer"}:
        requested = int(value) if selector == "customer_id" else _key_text(value)
        matches = []
        identities = set()
        unverifiable_batches = 0
        for raw_batch in db.execute(
                "SELECT * FROM packing_batches ORDER BY created_at DESC,id DESC").fetchall():
            batch = dict(raw_batch)
            allocations = _batch_allocations(db, int(batch["id"]))
            if not allocations:
                continue
            try:
                evidence = _historical_allocation_keys(db, batch, allocations)
            except PackingHistoryError:
                # A batch with no immutable identity cannot be assigned to this
                # customer. It is never reconstructed or returned.
                unverifiable_batches += 1
                continue
            customer = evidence[1]
            matched = (customer.get("id") == requested if selector == "customer_id" else
                       requested in {_key_text(customer.get("name")), _key_text(customer.get("email"))})
            if matched:
                identities.add((customer.get("id"), _key_text(customer.get("name")),
                                _key_text(customer.get("email"))))
                matches.append((raw_batch, evidence))
        if len(identities) > 1:
            raise PackingHistoryError(
                "PACKING_HISTORY_CUSTOMER_AMBIGUOUS",
                "Nazwa klienta pasuje do więcej niż jednej historii pakowania. Wskaż klienta jednoznacznie.",
            )
        if matches:
            return matches[0]
        if unverifiable_batches:
            raise _not_verifiable(
                "Nie można wiarygodnie ustalić historycznego batcha dla tego klienta.")
        return None, None
    if selector == "today":
        today = datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
        rows = db.execute(
            """SELECT * FROM packing_batches
                 WHERE substr(created_at,1,10)=?
                 ORDER BY created_at DESC,id DESC""",
            (today,),
        ).fetchall()
        if len(rows) > 1:
            raise PackingHistoryError(
                "PACKING_HISTORY_SCOPE_AMBIGUOUS",
                "Dziś zapisano więcej niż jedną listę pakową. Wskaż numer zamówienia albo konkretny batch.",
            )
        return (rows[0] if rows else None), None
    return (db.execute(
        "SELECT * FROM packing_batches ORDER BY created_at DESC,id DESC LIMIT 1"
    ).fetchone(), None)


def read(data: Mapping[str, Any], *, connection_factory) -> dict[str, Any]:
    db = connection_factory()
    try:
        batch, cached_evidence = _select_batch(db, data)
        if batch is None:
            raise PackingHistoryError(
                "PACKING_HISTORY_NOT_FOUND",
                "Nie znaleziono historycznej listy pakowej dla wskazanego zakresu.",
            )
        batch = dict(batch)
        allocations = _batch_allocations(db, int(batch["id"]))
        if not allocations:
            raise PackingHistoryError(
                "PACKING_HISTORY_ALLOCATIONS_MISSING",
                "Historyczny batch nie zawiera zapisanych alokacji listy pakowej.",
            )
        snapshot_evidence = _snapshot_allocation_keys(allocations)
        keyed_allocations, customer = (
            cached_evidence if cached_evidence is not None
            else _historical_allocation_keys(db, batch, allocations)
        )
        allocation_order_ids = {int(row["order_id"]) for row in allocations}
        documents = _document_records(db, int(batch["id"]))
        document, document_verified = _verified_document(documents, allocation_order_ids)

        if snapshot_evidence is not None:
            result_allocations = []
            incomplete_fields = []
            for allocation, _key in keyed_allocations:
                result_row = {
                    "order_item_id": int(allocation["order_item_id"]),
                    "order_id": int(allocation["order_id"]),
                    "order_number": " ".join(str(allocation.get("order_number_snapshot") or "").split()),
                    "sku": " ".join(str(allocation.get("sku_snapshot") or "").split()),
                    "model_name": " ".join(str(allocation.get("model_name_snapshot") or "").split()),
                    "note": " ".join(str(allocation.get("note_snapshot") or "").split()),
                    "packed_qty": int(allocation["qty"]),
                }
                if not result_row["model_name"]:
                    incomplete_fields.append(
                        f"allocations[{len(result_allocations)}].model_name"
                    )
                result_allocations.append(result_row)
            return {
                "ok": True,
                "batch_id": int(batch["id"]),
                "created_at": str(batch.get("created_at") or ""),
                "order_ids": sorted(allocation_order_ids),
                "allocations": result_allocations,
                "total_lines": len(result_allocations),
                "total_qty": sum(int(row["qty"] or 0) for row in allocations),
                "document_id": int(document["document_id"]) if document else None,
                "document_path": str(document.get("path") or "") if document and document_verified else "",
                "document_available": bool(document and document_verified),
                "document_verified": bool(document_verified),
                "complete": not incomplete_fields,
                "incomplete_fields": incomplete_fields,
                "history_source": "allocation_snapshot",
                "customer": customer,
            }
        if not document:
            raise PackingHistoryError(
                "PACKING_HISTORY_INCOMPLETE",
                "Starszy batch nie ma kompletnego snapshotu ani weryfikowalnego dokumentu historycznego.",
            )
    finally:
        db.close()

    path = Path(str(document["path"] or ""))
    try:
        content = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise PackingHistoryError(
            "PACKING_HISTORY_DOCUMENT_UNAVAILABLE",
            "Nie ma dostępu do zapisanej historycznej listy pakowej; nie rekonstruuję jej z zamówień.",
        ) from exc
    if not content:
        raise PackingHistoryError(
            "PACKING_HISTORY_DOCUMENT_UNAVAILABLE",
            "Zapisana historyczna lista pakowa jest pusta; nie rekonstruuję jej z zamówień.",
        )
    expected_hash = str(document.get("file_hash") or "").strip()
    if not expected_hash:
        raise _not_verifiable(
            "Historyczny dokument nie ma zapisanej sumy kontrolnej i nie może zostać wiarygodnie odczytany."
        )
    if sha256(content).hexdigest() != expected_hash:
        raise PackingHistoryError(
            "PACKING_HISTORY_DOCUMENT_MISMATCH",
            "Zapisana historyczna lista pakowa nie zgadza się z utrwaloną sumą kontrolną.",
        )

    printed_rows, footer_lines, footer_qty = _document_rows(content)
    if footer_lines is None or footer_qty is None:
        raise _not_verifiable(
            "Historyczny dokument nie zawiera pełnej stopki z liczbą pozycji i sumą ilości."
        )
    allocation_qty = sum(int(row["qty"] or 0) for row in allocations)
    if (len(printed_rows) != len(allocations)
            or footer_lines != len(allocations)
            or footer_qty != allocation_qty):
        raise PackingHistoryError(
            "PACKING_HISTORY_DOCUMENT_MISMATCH",
            "Zapisany dokument nie zgadza się z historycznymi alokacjami batcha.",
        )

    printed_by_key = {}
    for printed in printed_rows:
        key = _stable_key(printed["order_number"], printed["sku"], printed["packed_qty"])
        if key in printed_by_key:
            raise _not_verifiable(
                "Wiersze historycznego dokumentu nie mają jednoznacznego klucza order_number, SKU i qty."
            )
        printed_by_key[key] = printed
    allocation_keys = {key for _allocation, key in keyed_allocations}
    if set(printed_by_key) != allocation_keys:
        raise _not_verifiable(
            "Nie można jednoznacznie przypisać wszystkich wierszy dokumentu do alokacji packing batcha."
        )

    result_allocations = []
    for allocation, key in keyed_allocations:
        printed = printed_by_key[key]
        result_allocations.append({
            "order_item_id": int(allocation["order_item_id"]),
            "order_id": int(allocation["order_id"]),
            "order_number": printed["order_number"],
            "sku": printed["sku"],
            "model_name": printed["model_name"],
            "note": printed["note"],
            "packed_qty": int(allocation["qty"]),
        })
    return {
        "ok": True,
        "batch_id": int(batch["id"]),
        "created_at": str(batch.get("created_at") or ""),
        "order_ids": sorted({int(row["order_id"]) for row in allocations}),
        "allocations": result_allocations,
        "total_lines": len(result_allocations),
        "total_qty": allocation_qty,
        "document_id": int(document["document_id"]),
        "document_path": str(document["path"]),
        "document_available": True,
        "document_verified": True,
        "complete": True,
        "incomplete_fields": [],
        "history_source": "legacy_verified_document",
        "customer": customer,
    }

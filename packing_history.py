"""Read-only access to the immutable contents of a saved packing list.

Packing allocations are authoritative for line identity and quantity.  Product
labels and order notes are read from the saved PDF because the legacy
``packing_allocations`` schema does not snapshot those display fields.
Current order items are deliberately never queried here.
"""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
from pathlib import Path
import json
import re
from typing import Any, Mapping

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
    for key in ("batch_id", "order_id", "customer_id", "customer"):
        if data.get(key) not in (None, ""):
            supplied.append((key, data[key]))
    if data.get("latest") is True:
        supplied.append(("latest", True))
    if len(supplied) != 1:
        raise PackingHistoryError(
            "PACKING_HISTORY_SELECTOR_REQUIRED",
            "Wskaż dokładnie jeden batch, zamówienie albo klienta historycznej listy pakowej.",
        )
    return supplied[0]


def _select_batch(db, data: Mapping[str, Any]):
    selector, value = _selector(data)
    if selector == "batch_id":
        return db.execute("SELECT * FROM packing_batches WHERE id=?", (int(value),)).fetchone()
    if selector == "order_id":
        return db.execute(
            """SELECT pb.* FROM packing_batches pb
                 WHERE pb.root_order_id=?
                    OR EXISTS (SELECT 1 FROM packing_allocations pa
                                WHERE pa.batch_id=pb.id AND pa.order_id=?)
                 ORDER BY pb.created_at DESC,pb.id DESC LIMIT 1""",
            (int(value), int(value)),
        ).fetchone()
    if selector == "customer_id":
        return db.execute(
            """SELECT DISTINCT pb.* FROM packing_batches pb
                 JOIN packing_allocations pa ON pa.batch_id=pb.id
                 JOIN orders o ON o.id=pa.order_id
                WHERE o.customer_id=?
                ORDER BY pb.created_at DESC,pb.id DESC LIMIT 1""",
            (int(value),),
        ).fetchone()
    if selector == "customer":
        customer = " ".join(str(value).split())
        identities = db.execute(
            """SELECT DISTINCT COALESCE('id:'||CAST(o.customer_id AS TEXT),
                       'name:'||LOWER(TRIM(o.customer_name))||'|email:'||LOWER(TRIM(COALESCE(o.customer_email,'')))) identity
                 FROM packing_batches pb
                 JOIN packing_allocations pa ON pa.batch_id=pb.id
                 JOIN orders o ON o.id=pa.order_id
                WHERE LOWER(TRIM(o.customer_name))=LOWER(?)
                   OR LOWER(TRIM(COALESCE(o.customer_email,'')))=LOWER(?)""",
            (customer, customer),
        ).fetchall()
        if len(identities) > 1:
            raise PackingHistoryError(
                "PACKING_HISTORY_CUSTOMER_AMBIGUOUS",
                "Nazwa klienta pasuje do więcej niż jednej historii pakowania. Wskaż klienta jednoznacznie.",
            )
        return db.execute(
            """SELECT DISTINCT pb.* FROM packing_batches pb
                 JOIN packing_allocations pa ON pa.batch_id=pb.id
                 JOIN orders o ON o.id=pa.order_id
                WHERE LOWER(TRIM(o.customer_name))=LOWER(?)
                   OR LOWER(TRIM(COALESCE(o.customer_email,'')))=LOWER(?)
                ORDER BY pb.created_at DESC,pb.id DESC LIMIT 1""",
            (customer, customer),
        ).fetchone()
    return db.execute(
        "SELECT * FROM packing_batches ORDER BY created_at DESC,id DESC LIMIT 1"
    ).fetchone()


def _historical_allocation_keys(db, batch: Mapping[str, Any], allocations: list[dict[str, Any]]):
    """Build allocation-side keys only from immutable invoice snapshots."""
    invoice_id = int(batch.get("invoice_id") or 0)
    if invoice_id <= 0:
        raise _not_verifiable(
            "Historyczny batch nie ma kompletnego snapshotu potrzebnego do weryfikacji pozycji dokumentu."
        )
    try:
        invoice_rows = [dict(row) for row in db.execute(
            """SELECT order_id,order_item_id,sku,qty FROM invoice_allocations
                 WHERE invoice_id=? ORDER BY id""",
            (invoice_id,),
        ).fetchall()]
        meta = db.execute(
            "SELECT invoice_items_json FROM invoice_meta WHERE invoice_id=?",
            (invoice_id,),
        ).fetchone()
    except Exception as exc:
        raise _not_verifiable(
            "Nie można zweryfikować historycznego snapshotu pozycji dokumentu."
        ) from exc
    if not invoice_rows or meta is None or not str(meta["invoice_items_json"] or "").strip():
        raise _not_verifiable(
            "Historyczny batch nie ma kompletnego snapshotu potrzebnego do weryfikacji pozycji dokumentu."
        )
    try:
        snapshot_items = json.loads(meta["invoice_items_json"])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise _not_verifiable("Historyczny snapshot pozycji dokumentu jest nieczytelny.") from exc
    if not isinstance(snapshot_items, list) or not snapshot_items:
        raise _not_verifiable("Historyczny snapshot pozycji dokumentu jest niekompletny.")

    def unique_by_item(rows, *, source: str):
        by_item: dict[tuple[int, int], dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise _not_verifiable(f"Historyczny snapshot {source} ma nieprawidłowy wiersz.")
            try:
                order_id = int(row.get("source_order_id") or row.get("order_id") or 0)
                item_id = int(row.get("order_item_id") or row.get("id") or 0)
            except (TypeError, ValueError) as exc:
                raise _not_verifiable(f"Historyczny snapshot {source} ma nieprawidłowy identyfikator.") from exc
            identity = (order_id, item_id)
            if order_id <= 0 or item_id <= 0 or identity in by_item:
                raise _not_verifiable(f"Historyczny snapshot {source} nie identyfikuje pozycji jednoznacznie.")
            by_item[identity] = dict(row)
        return by_item

    invoice_by_item = unique_by_item(invoice_rows, source="alokacji faktury")
    snapshot_by_item = unique_by_item(snapshot_items, source="pozycji faktury")
    allocation_identities = {
        (int(row["order_id"]), int(row["order_item_id"])) for row in allocations
    }
    if set(invoice_by_item) != allocation_identities or set(snapshot_by_item) != allocation_identities:
        raise _not_verifiable(
            "Historyczny snapshot nie odpowiada dokładnie alokacjom packing batcha."
        )

    keyed = []
    seen_keys = set()
    for allocation in allocations:
        identity = (int(allocation["order_id"]), int(allocation["order_item_id"]))
        invoice_row = invoice_by_item[identity]
        snapshot = snapshot_by_item[identity]
        allocation_qty = int(allocation["qty"] or 0)
        try:
            invoice_qty = int(invoice_row.get("qty") or 0)
            snapshot_qty = int(snapshot.get("qty") or 0)
        except (TypeError, ValueError) as exc:
            raise _not_verifiable("Historyczny snapshot ma nieprawidłową ilość pozycji.") from exc
        if allocation_qty <= 0 or invoice_qty != allocation_qty or snapshot_qty != allocation_qty:
            raise _not_verifiable(
                "Ilości historycznego snapshotu nie odpowiadają alokacjom packing batcha."
            )
        invoice_sku = " ".join(str(invoice_row.get("sku") or "").split())
        snapshot_sku = " ".join(str(snapshot.get("sku") or "").split())
        if not invoice_sku or not snapshot_sku or _key_text(invoice_sku) != _key_text(snapshot_sku):
            raise _not_verifiable("Historyczny snapshot nie zawiera spójnego SKU pozycji.")
        note = snapshot.get("source_order_note") or ""
        order_number = _source_order_number(snapshot.get("source_order_no"), note)
        if not order_number:
            raise _not_verifiable("Historyczny snapshot nie zawiera numeru zamówienia pozycji.")
        key = _stable_key(order_number, invoice_sku, allocation_qty)
        if key in seen_keys:
            raise _not_verifiable(
                "Historyczne pozycje nie mają jednoznacznego klucza order_number, SKU i qty."
            )
        seen_keys.add(key)
        keyed.append((allocation, key))
    return keyed


def read(data: Mapping[str, Any], *, connection_factory) -> dict[str, Any]:
    db = connection_factory()
    try:
        batch = _select_batch(db, data)
        if batch is None:
            raise PackingHistoryError(
                "PACKING_HISTORY_NOT_FOUND",
                "Nie znaleziono historycznej listy pakowej dla wskazanego zakresu.",
            )
        batch = dict(batch)
        allocations = [dict(row) for row in db.execute(
            """SELECT id,order_id,order_item_id,qty FROM packing_allocations
                 WHERE batch_id=? ORDER BY id""",
            (int(batch["id"]),),
        ).fetchall()]
        if not allocations:
            raise PackingHistoryError(
                "PACKING_HISTORY_ALLOCATIONS_MISSING",
                "Historyczny batch nie zawiera zapisanych alokacji listy pakowej.",
            )
        keyed_allocations = _historical_allocation_keys(db, batch, allocations)
        document = db.execute(
            """SELECT document_id,path,file_hash,created_at FROM fulfillment_documents
                 WHERE kind='packing_list' AND document_id=?
                 ORDER BY created_at DESC LIMIT 1""",
            (int(batch["id"]),),
        ).fetchone()
        if document is None:
            raise PackingHistoryError(
                "PACKING_HISTORY_DOCUMENT_UNAVAILABLE",
                "Batch istnieje, ale nie ma dostępu do jego zapisanej historycznej listy pakowej; nie rekonstruuję jej z zamówień.",
            )
        document = dict(document)
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
    }

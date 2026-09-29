"""KSeF batch entry point and shared implementation used by the scheduler."""
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# A failure in an unrelated module must not block verified invoice data.
KSEF_REQUIRED_TABLES = frozenset({
    'company_profile', 'customers', 'products', 'orders', 'order_items',
    'invoices', 'invoice_meta', 'invoice_allocations', 'ksef_documents',
})


def _backend(backend=None):
    if backend is None:
        import app as backend
    return backend


def candidates(now, backend=None):
    """Return invoice ids using the existing qualification rules."""
    backend = _backend(backend)
    start = os.environ.get("KSEF_AUTOMATION_START_DATE", "").strip()
    if not start:
        raise ValueError("Ustaw KSEF_AUTOMATION_START_DATE na dzień rozpoczęcia automatu (YYYY-MM-DD).")
    datetime.strptime(start, "%Y-%m-%d")
    if now.tzinfo is not None:
        now = now.astimezone(ZoneInfo("Europe/Warsaw"))
    due_date = (now.date() if now.hour >= 17 else now.date() - timedelta(days=1)).isoformat()
    connection = backend.conn()
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT i.id,i.created_at,k.ksef_number,k.status,COALESCE(m.sent_to_client,0) mailed
               FROM invoices i
               LEFT JOIN ksef_documents k ON k.invoice_id=i.id
               LEFT JOIN invoice_meta m ON m.invoice_id=i.id
               WHERE i.publication_state='complete' AND substr(i.created_at,1,10)>=?
               ORDER BY i.id""",
            (start,),
        )]
    finally:
        connection.close()
    return [
        row["id"] for row in rows
        if not (row["ksef_number"] and row["mailed"])
        and (
            str(row["created_at"] or "")[:10] <= due_date
            or row["ksef_number"]
            or row["status"] in ("sending", "processing", "unknown")
        )
    ]


def _result_complete(result):
    return (
        not result.get("error")
        and result.get("status") not in ("error", "unknown", "rejected", "sending", "processing")
        and bool(result.get("number_received"))
        and bool(result.get("mailed"))
    )


def run_batch(backend, now=None, send=True, progress=None):
    now = now or datetime.now(ZoneInfo("Europe/Warsaw"))
    if backend.supabase_enabled():
        pulled = backend.pull_shared_tables_from_supabase(force=True)
        tables = pulled.get('tables', {}) if isinstance(pulled, dict) else {}
        failed = sorted(name for name in KSEF_REQUIRED_TABLES
                        if not isinstance(tables.get(name), dict) or tables[name].get('status') != 'ok')
        if failed:
            raise RuntimeError("KSEF_SYNC_FAILED: nie potwierdzono pobrania aktualnych danych z Supabase"
                               + ("; tabele: " + ", ".join(failed) if failed else ""))
        unrelated = sorted(name for name, value in tables.items()
                           if name not in KSEF_REQUIRED_TABLES and value.get('status') == 'error')
        if unrelated:
            backend.app.logger.warning('KSEF_SYNC_UNRELATED_ERRORS tables=%s', ','.join(unrelated))
    invoice_ids = candidates(now, backend)
    if not send:
        return {
            "ok": True,
            "dry_run": True,
            "local_time": now.isoformat(),
            "invoice_ids": invoice_ids,
            "results": [],
        }

    results = []
    for invoice_id in invoice_ids:
        if progress:
            progress("before_invoice", invoice_id)
        try:
            with backend.app.test_request_context(
                "/invoices/" + str(invoice_id) + "/ksef/send", method="POST"
            ):
                backend._refresh_domain_route_context()
                response = backend.app.view_functions["invoice_ksef_send"](invoice_id)
                http_status = int(response[1]) if isinstance(response, tuple) else getattr(response, "status_code", 200)
                if http_status >= 400:
                    raise RuntimeError("KSeF HTTP " + str(http_status) + ": " +
                                       (str(response[0]) if isinstance(response, tuple) else response.get_data(as_text=True))[:250])
                document = backend.load_ksef_doc(invoice_id)
                metadata = backend.load_invoice_meta(invoice_id) or {}
                results.append({
                    "invoice_id": invoice_id,
                    "status": document.get("status"),
                    "number_received": bool(document.get("ksef_number")),
                    "mailed": bool(metadata.get("sent_to_client")),
                    "error": document.get("last_error") or "",
                })
        except Exception as exc:
            backend.app.logger.exception("KSeF: faktura %s", invoice_id)
            results.append({"invoice_id": invoice_id, "error": str(exc)[:250]})
        if progress:
            progress("after_invoice", invoice_id)

    return {
        "ok": all(_result_complete(result) for result in results),
        "local_time": now.isoformat(),
        "invoice_ids": invoice_ids,
        "results": results,
    }


def main():
    # The standalone command must not start either in-process worker while it
    # imports the Flask application and runs one explicit batch.
    os.environ["INPOST_PICKUP_WORKER"] = "0"
    os.environ["KSEF_SCHEDULER_WORKER"] = "0"
    os.environ["SUPABASE_AUTO_SYNC_ON_WRITE"] = "0"
    import app as backend

    now = datetime.now(ZoneInfo("Europe/Warsaw"))
    send = "--send" in sys.argv
    summary = run_batch(backend, now=now, send=send)
    if not send:
        print(json.dumps({
            "dry_run": True,
            "local_time": summary["local_time"],
            "invoice_ids": summary["invoice_ids"],
        }))
        return 0
    print(json.dumps({
        "local_time": summary["local_time"],
        "results": summary["results"],
    }, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

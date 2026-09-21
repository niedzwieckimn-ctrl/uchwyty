"""KSeF batch entry point and shared implementation used by the scheduler."""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo


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
    connection = backend.conn()
    try:
        rows = [dict(row) for row in connection.execute(
            """SELECT i.id,k.ksef_number,k.status,COALESCE(m.sent_to_client,0) mailed
               FROM invoices i
               LEFT JOIN ksef_documents k ON k.invoice_id=i.id
               LEFT JOIN invoice_meta m ON m.invoice_id=i.id
               WHERE i.publication_state='complete' AND substr(i.created_at,1,10)>=?
               ORDER BY i.id""",
            (start,),
        )]
    finally:
        connection.close()
    after_daily_cutoff = now.hour >= 17
    return [
        row["id"] for row in rows
        if not (row["ksef_number"] and row["mailed"])
        and (
            after_daily_cutoff
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
        backend.pull_shared_tables_from_supabase(force=True)
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
                if isinstance(response, tuple) and int(response[1]) >= 400:
                    raise RuntimeError(str(response[0]))
                document = backend.load_ksef_doc(invoice_id)
                metadata = backend.load_invoice_meta(invoice_id) or {}
                results.append({
                    "invoice_id": invoice_id,
                    "status": document.get("status"),
                    "number_received": bool(document.get("ksef_number")),
                    "mailed": bool(metadata.get("sent_to_client")),
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

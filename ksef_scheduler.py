"""In-process daily KSeF scheduler with a durable cross-process lease."""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


JOB_NAME = "ksef_daily_batch"
WARSAW = ZoneInfo("Europe/Warsaw")
CUTOFF_HOUR = 17
CHECK_INTERVAL_SECONDS = 60
LEASE_SECONDS = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS ksef_scheduler_runs(
    job_name TEXT NOT NULL,
    run_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_until REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    failed_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(job_name, run_date)
)
"""


def initialize(connection) -> None:
    connection.execute(SCHEMA)
    connection.commit()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _first_row(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value if isinstance(value, dict) and value else None


class Store:
    def __init__(self, backend):
        self.backend = backend

    def claim(self, run_date: str, now: datetime | None = None):
        now = now or _utc_now()
        token = str(uuid.uuid4())
        if self.backend.supabase_enabled():
            return _first_row(self.backend.supabase_request(
                "/rest/v1/rpc/claim_ksef_scheduler_run",
                method="POST",
                payload={
                    "p_job_name": JOB_NAME,
                    "p_run_date": run_date,
                    "p_token": token,
                    "p_now": _iso(now),
                    "p_lease_seconds": LEASE_SECONDS,
                },
                timeout=20,
            ))

        now_epoch = now.timestamp()
        now_iso = _iso(now)
        connection = self.backend.conn()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ksef_scheduler_runs WHERE job_name=? AND run_date=?",
                (JOB_NAME, run_date),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO ksef_scheduler_runs(
                           job_name,run_date,status,attempt_count,lease_token,lease_until,
                           created_at,started_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (JOB_NAME, run_date, "running", 1, token,
                     now_epoch + LEASE_SECONDS, now_iso, now_iso, now_iso),
                )
            elif row["status"] == "completed" or (
                row["status"] == "running" and float(row["lease_until"] or 0) > now_epoch
            ):
                connection.commit()
                return None
            else:
                changed = connection.execute(
                    """UPDATE ksef_scheduler_runs
                       SET status='running',attempt_count=attempt_count+1,lease_token=?,
                           lease_until=?,started_at=?,completed_at=NULL,failed_at=NULL,
                           last_error='',updated_at=?
                       WHERE job_name=? AND run_date=? AND status<>'completed'
                         AND (status='failed' OR lease_until<=?)""",
                    (token, now_epoch + LEASE_SECONDS, now_iso, now_iso,
                     JOB_NAME, run_date, now_epoch),
                )
                if changed.rowcount != 1:
                    connection.commit()
                    return None
            connection.commit()
            claimed = connection.execute(
                "SELECT * FROM ksef_scheduler_runs WHERE job_name=? AND run_date=? AND lease_token=?",
                (JOB_NAME, run_date, token),
            ).fetchone()
            return dict(claimed) if claimed else None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def state(self, run_date: str):
        if self.backend.supabase_enabled():
            return _first_row(self.backend.supabase_request(
                "/rest/v1/ksef_scheduler_runs",
                params={
                    "job_name": "eq." + JOB_NAME,
                    "run_date": "eq." + run_date,
                    "select": "*",
                    "limit": 1,
                },
                timeout=20,
            ))
        connection = self.backend.conn()
        try:
            row = connection.execute(
                "SELECT * FROM ksef_scheduler_runs WHERE job_name=? AND run_date=?",
                (JOB_NAME, run_date),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def renew(self, run, now: datetime | None = None) -> bool:
        now = now or _utc_now()
        values = {
            "lease_until": _iso(now + timedelta(seconds=LEASE_SECONDS)),
            "updated_at": _iso(now),
        }
        if self.backend.supabase_enabled():
            rows = self.backend.supabase_request(
                "/rest/v1/ksef_scheduler_runs",
                method="PATCH",
                params=self._filters(run),
                payload=values,
                prefer="return=representation",
                timeout=20,
            ) or []
            return bool(rows)
        connection = self.backend.conn()
        try:
            changed = connection.execute(
                """UPDATE ksef_scheduler_runs SET lease_until=?,updated_at=?
                   WHERE job_name=? AND run_date=? AND status='running' AND lease_token=?""",
                (now.timestamp() + LEASE_SECONDS, values["updated_at"],
                 JOB_NAME, run["run_date"], run["lease_token"]),
            )
            connection.commit()
            return changed.rowcount == 1
        finally:
            connection.close()

    def complete(self, run, now: datetime | None = None) -> bool:
        now = now or _utc_now()
        return self._finish(run, {
            "status": "completed",
            "completed_at": _iso(now),
            "failed_at": None,
            "last_error": "",
            "lease_token": None,
            "lease_until": None,
            "updated_at": _iso(now),
        }, sqlite_lease_until=0)

    def fail(self, run, error: str, now: datetime | None = None) -> bool:
        now = now or _utc_now()
        return self._finish(run, {
            "status": "failed",
            "failed_at": _iso(now),
            "last_error": str(error or "")[:1000],
            "lease_token": None,
            "lease_until": None,
            "updated_at": _iso(now),
        }, sqlite_lease_until=0)

    def _filters(self, run):
        return {
            "job_name": "eq." + JOB_NAME,
            "run_date": "eq." + str(run["run_date"]),
            "status": "eq.running",
            "lease_token": "eq." + str(run["lease_token"]),
        }

    def _finish(self, run, values, sqlite_lease_until: float) -> bool:
        if self.backend.supabase_enabled():
            rows = self.backend.supabase_request(
                "/rest/v1/ksef_scheduler_runs",
                method="PATCH",
                params=self._filters(run),
                payload=values,
                prefer="return=representation",
                timeout=20,
            ) or []
            return bool(rows)
        sqlite_values = dict(values)
        sqlite_values["lease_until"] = sqlite_lease_until
        keys = list(sqlite_values)
        connection = self.backend.conn()
        try:
            changed = connection.execute(
                "UPDATE ksef_scheduler_runs SET " + ",".join(key + "=?" for key in keys)
                + " WHERE job_name=? AND run_date=? AND status='running' AND lease_token=?",
                tuple(sqlite_values[key] for key in keys)
                + (JOB_NAME, run["run_date"], run["lease_token"]),
            )
            connection.commit()
            return changed.rowcount == 1
        finally:
            connection.close()


def _batch_error(summary) -> str:
    failures = []
    for result in summary.get("results") or []:
        if result.get("error"):
            failures.append(f"invoice_id={result.get('invoice_id')}: {result['error']}")
        elif not result.get("number_received") or not result.get("mailed"):
            failures.append(
                f"invoice_id={result.get('invoice_id')}: status={result.get('status') or 'unknown'}"
            )
    return ("; ".join(failures) or "Batch KSeF nie zakończył wszystkich faktur pełnym sukcesem")[:1000]


def check_once(backend, now: datetime | None = None, run_batch_fn=None):
    now = now or datetime.now(WARSAW)
    if now.tzinfo is None:
        now = now.replace(tzinfo=WARSAW)
    local_now = now.astimezone(WARSAW)
    run_date = local_now.date().isoformat()
    backend.app.logger.info(
        "KSEF_SCHEDULER_CHECK run_date=%s local_hour=%s cutoff_reached=%s",
        run_date, local_now.hour, local_now.hour >= CUTOFF_HOUR,
    )
    if local_now.hour < CUTOFF_HOUR:
        return {"status": "before_cutoff", "run_date": run_date}

    store = Store(backend)
    run = store.claim(run_date, now=local_now.astimezone(timezone.utc))
    if not run:
        current = store.state(run_date)
        if current and current.get("status") == "completed":
            backend.app.logger.info("KSEF_BATCH_SKIPPED_ALREADY_DONE run_date=%s", run_date)
            return {"status": "completed", "run_date": run_date}
        return {"status": "leased", "run_date": run_date}

    backend.app.logger.info(
        "KSEF_BATCH_ACQUIRED run_date=%s attempt=%s",
        run_date, run.get("attempt_count"),
    )
    backend.app.logger.info("KSEF_BATCH_STARTED run_date=%s", run_date)

    stopped = threading.Event()
    lease_lost = threading.Event()

    def renew_lease():
        while not stopped.wait(max(10, LEASE_SECONDS // 3)):
            try:
                if not store.renew(run):
                    lease_lost.set()
                    return
            except Exception:
                backend.app.logger.exception("KSEF_BATCH_FAILED run_date=%s phase=lease_renew", run_date)
                lease_lost.set()
                return

    heartbeat = threading.Thread(
        target=renew_lease, name="ksef-daily-lease", daemon=True,
    )
    heartbeat.start()

    def progress(_phase=None, _invoice_id=None):
        if lease_lost.is_set() or not store.renew(run):
            lease_lost.set()
            raise RuntimeError("Utracono dzienną blokadę batcha KSeF")

    try:
        if run_batch_fn is None:
            from run_ksef_batch import run_batch
            run_batch_fn = run_batch
        summary = run_batch_fn(backend, now=local_now, send=True, progress=progress)
        if not summary.get("ok"):
            raise RuntimeError(_batch_error(summary))
        if lease_lost.is_set() or not store.complete(run):
            raise RuntimeError("Utracono dzienną blokadę przed zapisem wyniku KSeF")
        backend.app.logger.info(
            "KSEF_BATCH_COMPLETED run_date=%s invoices=%s",
            run_date, len(summary.get("results") or []),
        )
        return {"status": "completed", "run_date": run_date, "summary": summary}
    except Exception as exc:
        store.fail(run, str(exc))
        backend.app.logger.exception(
            "KSEF_BATCH_FAILED run_date=%s error=%s", run_date, str(exc)[:500],
        )
        return {"status": "failed", "run_date": run_date, "error": str(exc)[:1000]}
    finally:
        stopped.set()
        heartbeat.join(timeout=1)


_worker_lock = threading.Lock()
_worker = None


def start_worker(backend):
    global _worker
    if os.environ.get("KSEF_SCHEDULER_WORKER", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    with _worker_lock:
        if _worker and _worker.is_alive():
            return _worker

        def run():
            while True:
                try:
                    with backend.app.app_context():
                        check_once(backend)
                except Exception:
                    backend.app.logger.exception("KSEF_BATCH_FAILED phase=scheduler_check")
                time.sleep(CHECK_INTERVAL_SECONDS)

        _worker = threading.Thread(target=run, name="ksef-daily-scheduler", daemon=True)
        _worker.start()
        return _worker

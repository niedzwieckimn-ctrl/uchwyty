CREATE TABLE IF NOT EXISTS payment_reminder_attempts(
    attempt_id TEXT PRIMARY KEY,
    invoice_id INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    completed_at TEXT,
    channel TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    recipient TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCESS','FAILED')),
    error TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(invoice_id) REFERENCES invoices(id)
);

CREATE INDEX IF NOT EXISTS idx_payment_reminder_attempts_invoice
    ON payment_reminder_attempts(invoice_id,attempted_at DESC);

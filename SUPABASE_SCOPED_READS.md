# Supabase reads and transfer accounting

Baseline: `998e61e6f7138b1bbb6551172f1709bf751a26b5`.

Warm GET requests use `supabase_read_cache.plan()` to refresh only the page's
dependencies in the background. The invoice list and client invoice list read
four tables; downloading an InPost label refreshes only its order. The existing
30-second freshness interval is shared per table, including across different
pages. A full-table read satisfies a narrower read, not vice versa. Requests
arriving during a refresh are queued and rechecked for freshness before I/O.

Cold bootstrap still restores the complete set of 20 tables. A partial or failed
bootstrap cannot establish the durable success marker and retains its existing
recovery path. Forced refreshes before writes, business-operation freshness,
stock accounting, invoice publication, carrier booking and cloud claims retain
their existing contracts. Scoped snapshots never infer deletions. Pending local
writes and shipment/payment state remain protected by the existing merge guards.

Global status reconciliation is allowed only when every dependent full table
has been refreshed successfully within the freshness interval. A failed table
cannot cause status writes based on a partial snapshot. Per-scope failures back
off from one minute to fifteen minutes without blocking unrelated scopes.

All `supabase_request` calls request gzip and decode it when the server returns
`Content-Encoding: gzip`; uncompressed responses work unchanged. With performance
logging enabled, `SUPABASE_TRANSFER` records the method, path (without query
parameters), response status, compressed response bytes, decoded bytes and time.
No tokens, filters or response bodies are logged. These are application-observed
response sizes, not an exact Supabase billing meter; other clients, storage
downloads and protocol overhead are outside this counter. `SUPABASE_SCOPED_READ`
records the tables actually refreshed. Unknown GET callers use a full dependency
set for compatibility and emit `SUPABASE_READ_SCOPE_FALLBACK` for follow-up.

Validation was offline with network access forbidden: cold/partial bootstrap,
concurrent page scopes, failure backoff, narrow-read cache identity, nondeletion,
forced POST refresh, gzip/plain responses, private-log redaction, invoice/stock
consistency, SQLite contention, shipment recipient/idempotency and KSeF recovery.
The first run passed 52 cases; the second passed 104 cases (some cases overlap).
The invoice read test reduces table requests from 20 to 4. No production byte
saving percentage or page latency measurement has been claimed.

Deploy all runtime files together: `app.py` imports `supabase_read_cache.py`.
No new dependency, schema migration or environment variable is needed. Keep the
current `SUPABASE_BACKGROUND_PULL_INTERVAL_SEC` value. Deploy only when Supabase
is available: a Render restart may require a complete cold bootstrap. After
deployment, verify successful bootstrap, scoped refresh logs and measured bytes;
compare the next complete usage interval in Supabase rather than cumulative
usage already incurred before this change.

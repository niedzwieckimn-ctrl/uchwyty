-- Wake the existing Render application before the courier cutoff, including DST.
-- The public health GET starts no shipment/pickup itself; the durable worker
-- retains the normal 12:20 deadline, leases and uncertain-result reconciliation.
-- Reuses the extensions and health endpoint already used for KSeF.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;
SELECT cron.schedule('uchwyty-inpost-wakeup', '* * * * *', $cron$
    SELECT net.http_get(
        url := 'https://uchwyty.onrender.com/health/ksef-worker',
        timeout_milliseconds := 10000
    )
    WHERE EXTRACT(ISODOW FROM CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Warsaw') BETWEEN 1 AND 5
      AND (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Warsaw')::time
          BETWEEN TIME '12:15' AND TIME '12:21:59';
$cron$);
COMMIT;

-- External clock for the existing KSeF worker. No tokens or invoice data in HTTP.
-- Deploy /health/ksef-worker first; its response must show worker_alive=true.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_cron;
CREATE EXTENSION IF NOT EXISTS pg_net;
-- Named schedule is updated in place on reapplication; no duplicate job.
SELECT cron.schedule('uchwyty-ksef-wakeup', '* * * * *', $cron$
    SELECT net.http_get(
        url := 'https://uchwyty.onrender.com/health/ksef-worker',
        timeout_milliseconds := 10000
    )
    WHERE (
        -- Prewarm before cutoff and keep the process running during its main batch.
        (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Warsaw')::time BETWEEN TIME '16:55' AND TIME '18:00'
        -- Recovery/retry after hosting/provider outages, including the next morning.
        OR EXTRACT(MINUTE FROM CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Warsaw') = 0
    );
$cron$);
COMMIT;

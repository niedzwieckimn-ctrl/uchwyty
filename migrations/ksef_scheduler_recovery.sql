-- Apply before the recovery code. Existing invoices and attempts are preserved.
BEGIN;
ALTER TABLE public.ksef_scheduler_runs
    ADD COLUMN IF NOT EXISTS summary_json jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE OR REPLACE FUNCTION public.claim_ksef_scheduler_run(
    p_job_name text, p_run_date date, p_token text, p_now timestamptz, p_lease_seconds integer
) RETURNS SETOF public.ksef_scheduler_runs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $function$
BEGIN
    -- Serialize claims across dates as well as across workers.
    PERFORM pg_advisory_xact_lock(hashtextextended(p_job_name, 0));
    IF EXISTS (SELECT 1 FROM public.ksef_scheduler_runs
               WHERE job_name=p_job_name AND status='running' AND lease_until>p_now) THEN
        RETURN;
    END IF;
    RETURN QUERY
    INSERT INTO public.ksef_scheduler_runs AS runs (
        job_name,run_date,status,attempt_count,lease_token,lease_until,created_at,started_at,updated_at
    ) VALUES (
        p_job_name,p_run_date,'running',1,p_token,
        p_now+make_interval(secs=>greatest(p_lease_seconds,60)),p_now,p_now,p_now
    ) ON CONFLICT (job_name,run_date) DO UPDATE SET
        status='running',attempt_count=runs.attempt_count+1,lease_token=excluded.lease_token,
        lease_until=excluded.lease_until,started_at=excluded.started_at,
        completed_at=NULL,failed_at=NULL,last_error='',updated_at=excluded.updated_at
    WHERE (runs.status IN ('completed','failed') AND runs.updated_at<=p_now-interval '5 minutes')
       OR (runs.status='running' AND (runs.lease_until IS NULL OR runs.lease_until<=p_now))
    RETURNING *;
END;
$function$;
REVOKE ALL ON FUNCTION public.claim_ksef_scheduler_run(text,date,text,timestamptz,integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_ksef_scheduler_run(text,date,text,timestamptz,integer) TO service_role;
NOTIFY pgrst, 'reload schema';
COMMIT;

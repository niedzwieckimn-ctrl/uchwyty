-- Run once in Supabase SQL Editor before deploying the in-process scheduler.
-- The service-role backend is the only caller; browser roles receive no access.
create table if not exists public.ksef_scheduler_runs (
    job_name text not null,
    run_date date not null,
    status text not null check (status in ('running', 'completed', 'failed')),
    attempt_count integer not null default 0,
    lease_token text,
    lease_until timestamptz,
    created_at timestamptz not null,
    started_at timestamptz,
    completed_at timestamptz,
    failed_at timestamptz,
    last_error text not null default '',
    updated_at timestamptz not null,
    primary key (job_name, run_date)
);

alter table public.ksef_scheduler_runs enable row level security;
revoke all privileges on table public.ksef_scheduler_runs from anon, authenticated;
grant select, insert, update on table public.ksef_scheduler_runs to service_role;

create or replace function public.claim_ksef_scheduler_run(
    p_job_name text,
    p_run_date date,
    p_token text,
    p_now timestamptz,
    p_lease_seconds integer
)
returns setof public.ksef_scheduler_runs
language sql
security definer
set search_path = public
as $function$
    insert into public.ksef_scheduler_runs(
        job_name, run_date, status, attempt_count, lease_token, lease_until,
        created_at, started_at, updated_at
    ) values (
        p_job_name, p_run_date, 'running', 1, p_token,
        p_now + make_interval(secs => greatest(p_lease_seconds, 60)),
        p_now, p_now, p_now
    )
    on conflict (job_name, run_date) do update
       set status = 'running',
           attempt_count = ksef_scheduler_runs.attempt_count + 1,
           lease_token = excluded.lease_token,
           lease_until = excluded.lease_until,
           started_at = excluded.started_at,
           completed_at = null,
           failed_at = null,
           last_error = '',
           updated_at = excluded.updated_at
     where ksef_scheduler_runs.status <> 'completed'
       and (
           ksef_scheduler_runs.status = 'failed'
           or ksef_scheduler_runs.lease_until is null
           or ksef_scheduler_runs.lease_until <= p_now
       )
    returning *;
$function$;

revoke all on function public.claim_ksef_scheduler_run(text,date,text,timestamptz,integer)
    from public, anon, authenticated;
grant execute on function public.claim_ksef_scheduler_run(text,date,text,timestamptz,integer)
    to service_role;

-- Run only in a new, disposable LOCAL database named test_architect_dashboard_*.
-- psql -v ON_ERROR_STOP=1 -d test_architect_dashboard_YYYYMMDD -f this_file.sql
\set ON_ERROR_STOP on
select current_database() like 'test_architect_dashboard_%' as safe_db \gset
\if :safe_db
\else
\echo Refusing to create synthetic fixtures outside a test_architect_dashboard_* database.
\quit 1
\endif
create schema auth;
create schema architect_portal;
do $$begin
 if not exists(select from pg_roles where rolname='anon') then create role anon; end if;
 if not exists(select from pg_roles where rolname='authenticated') then create role authenticated; end if;
end$$;
create function auth.uid() returns uuid language sql stable as $$select nullif(current_setting('request.jwt.claim.sub',true),'')::uuid$$;
create table public.architect_profiles(id uuid primary key);
create table public.architect_commission_ledger(id uuid primary key default gen_random_uuid(),architect_id uuid, sale_line_id bigint,invoice_id bigint,sold_qty int,amount_net numeric(14,2),entry_type text,status text,reason text,sale_date date,created_at timestamptz default now(),reversal_of uuid,settlement_id uuid);
create table public.architect_monthly_settlements(id uuid primary key default gen_random_uuid(),architect_id uuid,period_month date default '2026-10-01',batch_no int default 1,status text,total_net numeric(14,2));
create table public.architect_payouts(id uuid primary key default gen_random_uuid(),settlement_id uuid,amount_net numeric(14,2),paid_at timestamptz);
create table public.architect_sale_lines(id bigint primary key,redemption_id uuid);
create table public.architect_code_redemptions(id uuid primary key,code_id uuid);
create table public.architect_codes(id uuid primary key,architect_id uuid);
create function architect_portal.take_rate(uuid,text,int,int) returns boolean language sql as $$select true$$;
create function architect_portal.dispatch(text,jsonb) returns jsonb language sql as $$select '{"ok":true,"unchanged_dispatch":true}'::jsonb$$;
\ir migrations/architect_dashboard_presentation_020.sql
begin;
create function pg_temp.expect_summary(payable text,pending text,paid text,deduction text,corrections text,codes int default 0) returns void language plpgsql as $$
declare actual jsonb;
begin
 actual:=architect_portal.request('architect.dashboard','{"presentation_version":1}')->'summary';
 assert actual=jsonb_build_object('presentation_version',1,'payable',payable,'pending',pending,'paid',paid,'future_deduction',deduction,'pending_corrections',corrections,'used_codes',codes),format('Unexpected summary: %s',actual);
end$$;
insert into public.architect_profiles values('11111111-1111-1111-1111-111111111111'),('22222222-2222-2222-2222-222222222222');
select set_config('request.jwt.claim.sub','11111111-1111-1111-1111-111111111111',false);
-- Empty account.
select pg_temp.expect_summary('0.00','0.00','0.00','0.00','0.00');
-- Pending sale and a pending partial return are separately visible.
insert into public.architect_commission_ledger(id,architect_id,amount_net,entry_type,status) values('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',auth.uid(),100.00,'accrual','pending');
select pg_temp.expect_summary('0.00','100.00','0.00','0.00','0.00');
insert into public.architect_commission_ledger(id,architect_id,amount_net,entry_type,status,reversal_of) values('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',auth.uid(),-25.00,'return_adjustment','pending','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
select pg_temp.expect_summary('0.00','100.00','0.00','0.00','25.00');
-- Approval preserves the original 100 - 25 calculation.
update public.architect_commission_ledger set status='approved';
select pg_temp.expect_summary('75.00','0.00','0.00','0.00','0.00');
-- A monthly settlement does not double-count the same entries.
insert into public.architect_monthly_settlements(id,architect_id,status,total_net) values('cccccccc-cccc-cccc-cccc-cccccccccccc',auth.uid(),'approved',75.00);
update public.architect_commission_ledger set status='settled',settlement_id='cccccccc-cccc-cccc-cccc-cccccccccccc';
select pg_temp.expect_summary('75.00','0.00','0.00','0.00','0.00');
-- Recorded payout: later adjustments cannot rewrite this sum.
insert into public.architect_payouts(settlement_id,amount_net,paid_at) values('cccccccc-cccc-cccc-cccc-cccccccccccc',75.00,now()-interval '1 day');
update public.architect_commission_ledger set status='paid';
update public.architect_monthly_settlements set status='paid';
select pg_temp.expect_summary('0.00','0.00','75.00','0.00','0.00');
insert into public.architect_commission_ledger(architect_id,amount_net,entry_type,status,reversal_of) values(auth.uid(),-25.00,'return_adjustment','approved','aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
select pg_temp.expect_summary('0.00','0.00','75.00','25.00','0.00');
do $$declare h jsonb; begin
 h:=architect_portal.request('architect.earnings','{"presentation_version":1}');
 assert (select count(*)=1 from jsonb_array_elements(h->'ledger') x where x->>'status'='approved' and (x->>'after_payout')::boolean);
 assert jsonb_array_length(h->'payouts')=1;
end$$;
-- Pending earnings do not silently cover an approved debt.
insert into public.architect_commission_ledger(architect_id,amount_net,entry_type,status) values(auth.uid(),40.00,'accrual','pending');
select pg_temp.expect_summary('0.00','40.00','75.00','25.00','0.00');
update public.architect_commission_ledger set status='approved' where status='pending';
select pg_temp.expect_summary('15.00','0.00','75.00','0.00','0.00');
-- Two confirmed lines for one code count as ONE, redemption alone as ZERO.
insert into public.architect_codes values('dddddddd-dddd-dddd-dddd-dddddddddddd',auth.uid()),('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee',auth.uid());
insert into public.architect_code_redemptions values('dddddddd-dddd-dddd-dddd-dddddddddddd','dddddddd-dddd-dddd-dddd-dddddddddddd'),('eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee','eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee');
insert into public.architect_sale_lines values(1,'dddddddd-dddd-dddd-dddd-dddddddddddd'),(2,'dddddddd-dddd-dddd-dddd-dddddddddddd'),(3,'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee');
update public.architect_commission_ledger set sale_line_id=1,invoice_id=1,sold_qty=2 where id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
insert into public.architect_commission_ledger(architect_id,amount_net,entry_type,status,sale_line_id,invoice_id,sold_qty) values(auth.uid(),0,'accrual','paid',2,2,1);
select pg_temp.expect_summary('15.00','0.00','75.00','0.00','0.00',1);
-- Other architect and unpaid payout draft cannot change the user's totals.
insert into public.architect_commission_ledger(architect_id,amount_net,entry_type,status) values('22222222-2222-2222-2222-222222222222',99999,'accrual','approved');
insert into public.architect_payouts(settlement_id,amount_net,paid_at) values('cccccccc-cccc-cccc-cccc-cccccccccccc',88888,null);
select pg_temp.expect_summary('15.00','0.00','75.00','0.00','0.00',1);
-- Summary covers every row, even past the 500-entry history limit.
insert into public.architect_commission_ledger(architect_id,amount_net,entry_type,status) select auth.uid(),0.01,'accrual','pending' from generate_series(1,501);
select pg_temp.expect_summary('15.00','5.01','75.00','0.00','0.00',1);
create temp table before_read as select md5(string_agg(to_jsonb(l)::text,'' order by id)) checksum from public.architect_commission_ledger l;
select architect_portal.request('architect.earnings','{"presentation_version":1}')->'summary';
do $$begin
 assert (select count(*)=500 from jsonb_array_elements(architect_portal.request('architect.earnings','{"presentation_version":1}')->'ledger'));
 assert (select checksum from before_read)=(select md5(string_agg(to_jsonb(l)::text,'' order by id)) from public.architect_commission_ledger l);
 assert not has_function_privilege('authenticated','architect_portal.earnings_view(boolean)','execute');
 assert not has_function_privilege('anon','architect_portal.earnings_view(boolean)','execute');
 assert architect_portal.request('b2b.profile','{}')->>'unchanged_dispatch'='true';
 assert architect_portal.request('architect.dashboard','{}')->>'unchanged_dispatch'='true';
end$$;
-- Missing architect role (e.g. distributor) is denied; no identity parameter exists.
select set_config('request.jwt.claim.sub','33333333-3333-3333-3333-333333333333',false);
do $$begin assert architect_portal.request('architect.dashboard','{"presentation_version":1}')->>'status'='403'; end$$;
select set_config('request.jwt.claim.sub','',false);
do $$begin assert architect_portal.request('architect.dashboard','{"presentation_version":1}')->>'status'='401'; end$$;
rollback;
\echo PASS: empty, pending, approved, paid, partial return, post-payment correction, debt carry-forward, code counting, full totals, isolation and read-only evidence.

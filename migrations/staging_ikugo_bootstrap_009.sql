-- STAGING ONLY: repair the incomplete synthetic fixture in irhrzdsjnwjyqfvhiclk.
-- This is deliberately NOT a production migration. No credentials, new users,
-- orders, invoices, allocations, sale confirmations or payments are created.
-- Backfill only missing technical fields on the existing synthetic records.
begin;
set local lock_timeout = '5s';
set local statement_timeout = '60s';

do $guard$
declare configured_url text := current_setting('app.settings.supabase_url', true);
begin
  if configured_url is not null and configured_url <> ''
     and rtrim(configured_url, '/') <> 'https://irhrzdsjnwjyqfvhiclk.supabase.co' then
    raise exception '009 refused: configured Supabase project is not staging';
  end if;
  if to_regclass('public.staging_product_prices') is null
     or to_regclass('public.product_images') is null
     or to_regclass('public.product_image_assignments') is null
     or not exists (select 1 from auth.users
       where id='0062dcbf-717e-40a7-a820-3f79aefaa33f'::uuid
         and email='dystrybutor.staging@example.com')
     or not exists (select 1 from auth.users
       where id='f887962b-23d7-4379-aebf-79a171f03089'::uuid
         and email='cinek89_17@o2.pl') then
    raise exception '009 refused: exact staging fixture identity was not confirmed';
  end if;
  if exists (select 1 from public.products p where p.created_at is null
             and not exists (select 1 from public.staging_product_prices s where s.product_id=p.id))
     or exists (select 1 from public.customers c where c.created_at is null
             and not exists (select 1 from public.distributor_program_memberships d where d.customer_id=c.id))
     or exists (select 1 from public.orders o where o.warehouse_issued is null
             and not exists (select 1 from public.distributor_program_memberships d where d.customer_id=o.customer_id))
     or exists (select 1 from public.invoices i where i.payment_type is null
             and (i.id <> 5 or i.invoice_no is distinct from 'STAGING-ARCH-5-TEST')) then
    raise exception '009 refused: backfill would affect a record outside the known synthetic fixture';
  end if;
  -- The original minimal schema had only id in these two empty tables.
  -- Do not fabricate missing package/product identifiers for unexpected data.
  if not exists (select 1 from information_schema.columns where table_schema='public'
                 and table_name='china_packages' and column_name='package_no')
     and exists (select 1 from public.china_packages) then
    raise exception '009 refused: incomplete china_packages contains unexpected records';
  end if;
  if not exists (select 1 from information_schema.columns where table_schema='public'
                 and table_name='china_items' and column_name='package_id')
     and exists (select 1 from public.china_items) then
    raise exception '009 refused: incomplete china_items contains unexpected records';
  end if;
end $guard$;

-- Preserve all existing non-NULL values. Prefer the original linked event date.
update public.products p set created_at=coalesce(
  (select min(oi.created_at) from public.order_items oi where oi.product_id=p.id),
  to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'))
where p.created_at is null;
update public.customers c set created_at=coalesce(
  (select min(o.created_at) from public.orders o where o.customer_id=c.id),
  to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'))
where c.created_at is null;
update public.orders set warehouse_issued=0 where warehouse_issued is null;
-- Explicitly synthetic metadata repair; this does not issue an invoice or pay it.
update public.invoices set payment_type='transfer'
where id=5 and invoice_no='STAGING-ARCH-5-TEST' and payment_type is null;

alter table public.products
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null;
alter table public.customers
  alter column name set not null,
  alter column language set default 'pl', alter column language set not null,
  alter column price_list set default 'pln', alter column price_list set not null,
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null;
alter table public.orders
  alter column customer_name set not null,
  alter column status set default 'new', alter column status set not null,
  alter column warehouse_issued set default 0, alter column warehouse_issued set not null,
  alter column currency set default 'PLN', alter column currency set not null,
  alter column price_list set default 'pln', alter column price_list set not null,
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null,
  add column if not exists inpost_dispatch_order_id text;
alter table public.order_items
  alter column sku set not null,
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null;
alter table public.invoices
  alter column order_id set not null, alter column invoice_no set not null,
  alter column issue_date set not null, alter column sell_date set not null,
  alter column payment_type set not null,
  alter column total_net set default 0, alter column total_net set not null,
  alter column total_gross set default 0, alter column total_gross set not null,
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null;
alter table public.invoices add column if not exists publication_state text not null default 'complete';
alter table public.invoices alter column publication_state set default 'complete';
-- A NULL publication state on an existing fixture must be reviewed, not guessed.
alter table public.invoices alter column publication_state set not null;
create unique index if not exists staging_invoices_invoice_no_unique on public.invoices(invoice_no);
alter table public.invoice_allocations
  alter column created_at set default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  alter column created_at set not null;

alter table public.china_packages
  add column if not exists package_no text not null,
  add column if not exists status text not null default 'planned',
  add column if not exists tracking text, add column if not exists note text,
  add column if not exists created_at text not null default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),
  add column if not exists cost_amount double precision not null default 0,
  add column if not exists cost_document_no text, add column if not exists supplier text,
  add column if not exists ordered_at text, add column if not exists shipped_at text,
  add column if not exists arrived_at text, add column if not exists warehouse_received integer,
  add column if not exists warehouse_received_at text,
  add column if not exists tracking_carrier text, add column if not exists tracking_carrier_code integer,
  add column if not exists tracking_status text, add column if not exists tracking_substatus text,
  add column if not exists tracking_last_event text, add column if not exists tracking_last_update text,
  add column if not exists tracking_synced_at text, add column if not exists tracking_error text,
  add column if not exists tracking_events_json text, add column if not exists tracking_eta text,
  add column if not exists tracking_registered_at text, add column if not exists manual_status_at text,
  add column if not exists shipping_method text;
create unique index if not exists staging_china_package_no_unique on public.china_packages(package_no);
alter table public.china_items
  add column if not exists package_id bigint not null references public.china_packages(id),
  add column if not exists product_id bigint not null references public.products(id),
  add column if not exists sku text not null, add column if not exists qty integer not null,
  add column if not exists created_at text not null default to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS');

-- Nine missing bootstrap tables, matching the current IKUGO SQLite contracts.
create table if not exists public.china_stock_receipts (
  package_id bigint primary key references public.china_packages(id),
  received_at text not null, quantities_json text not null
);
create table if not exists public.china_documents (
  id bigint generated by default as identity primary key,
  package_id bigint not null references public.china_packages(id),
  original_name text not null, document_type text not null default 'order',
  stored_path text not null, size_bytes bigint not null, created_at text not null
);
create table if not exists public.pricing (
  model text primary key, net_price double precision not null default 0,
  gross_price double precision not null default 0, created_at text not null
);
create table if not exists public.pricing_eur (
  sku text primary key, ean text, price_eur double precision not null default 0,
  uvp_eur double precision not null default 0, created_at text not null, updated_at text not null
);
create table if not exists public.company_profile (
  id bigint primary key check(id=1), company_name text, address text, nip text,
  phone text, email text, bank_account text, bank_swift text, updated_at text not null
);
create table if not exists public.invoice_meta (
  invoice_id bigint primary key references public.invoices(id), pdf_path text,
  invoice_items_json text, sent_to_client integer not null default 0,
  seen_by_client integer not null default 0, payment_reminder integer not null default 0,
  paid integer not null default 0, paid_at text, seen_at text, updated_at text not null
);
create table if not exists public.ksef_documents (
  invoice_id bigint primary key references public.invoices(id),
  status text not null default 'draft', ksef_number text, xml_path text,
  last_error text, validated_at text, sent_at text, updated_at text not null
);
create table if not exists public.cash_flow_settings (
  key text primary key, value text not null default '', updated_at text not null
);
create table if not exists public.cash_flow_expenses (
  id bigint generated by default as identity primary key,
  expense_date text not null, category text not null, description text,
  document_no text not null, amount double precision not null check(amount>0), created_at text not null
);
-- These exist in the audited staging; IF NOT EXISTS also makes the contract complete.
create table if not exists public.product_images (
  id bigint generated by default as identity primary key,
  stored_path text not null unique, filename text not null, created_at text not null
);
create table if not exists public.product_image_assignments (
  product_id bigint primary key references public.products(id),
  image_id bigint not null references public.product_images(id), created_at text not null
);

-- Reuse the reviewed staging catalog prices. Never guess one model's price
-- when its test product variants disagree, or overwrite an existing price.
do $prices$
begin
  if exists (
    select p.model from public.products p
    join public.staging_product_prices s on s.product_id=p.id
    group by p.model having nullif(btrim(p.model),'') is null
      or count(distinct s.net_price)>1
  ) then
    raise exception '009 refused: staging prices need a unique nonempty model and one price per model';
  end if;
end $prices$;
insert into public.pricing(model,net_price,gross_price,created_at)
select p.model,min(s.net_price),round((min(s.net_price)*1.23)::numeric,2)::double precision,
       min(p.created_at)
from public.products p join public.staging_product_prices s on s.product_id=p.id
group by p.model on conflict(model) do nothing;
-- Explicit test seller; intentionally no real bank account, tax ID or mailbox.
insert into public.company_profile(id,company_name,address,nip,phone,email,bank_account,bank_swift,updated_at)
values(1,'Niedźwieccy STAGING — dokument testowy','Środowisko testowe — nie jest dokumentem sprzedaży',
       '','','','','',to_char(statement_timestamp() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'))
on conflict(id) do nothing;

-- No dependency on local-only internal_actors: actor IDs are immutable evidence.
-- JSON/boolean columns follow internal_audit_outbox.remote_payload, not SQLite types.
create table if not exists public.internal_audit_log (
  audit_id text primary key, occurred_at timestamptz not null,
  actor_id text, actor_type text check(actor_type is null or actor_type in ('HUMAN','SYSTEM','AI_AGENT')),
  actor_display_name text, roles_json jsonb not null default '[]'::jsonb,
  permission text, operation text not null,
  operation_version integer not null check(operation_version>0),
  audit_policy text not null check(audit_policy in ('READ_STANDARD','READ_SENSITIVE','WRITE','EXTERNAL','SECURITY')),
  entity_type text, entity_id text, request_id text, correlation_id text,
  session_id text, credential_id text,
  risk_level text not null check(risk_level in ('GREEN','YELLOW','RED')),
  reason text, approval_required boolean not null default false,
  approval_id text, approved_by text,
  result text not null check(result in ('SUCCESS','DENIED','FAILED','PENDING_APPROVAL','NOOP','CONFLICT')),
  error_code text, error_message text, external_integration text,
  external_request_id text, external_result text, idempotency_key text,
  is_replay boolean not null default false, before_state jsonb, after_state jsonb,
  entity_version_before bigint, entity_version_after bigint,
  expected_version bigint, current_version bigint, source text
);
create index if not exists staging_audit_actor_time on public.internal_audit_log(actor_id,occurred_at);
create index if not exists staging_audit_operation_time on public.internal_audit_log(operation,occurred_at);
create index if not exists staging_audit_correlation on public.internal_audit_log(correlation_id,occurred_at);
create or replace function public.staging_ikugo_reject_audit_mutation()
returns trigger language plpgsql set search_path=pg_catalog as $audit$
begin
  raise exception 'internal_audit_log is append-only';
end $audit$;
revoke all on function public.staging_ikugo_reject_audit_mutation() from public,anon,authenticated;
drop trigger if exists staging_ikugo_audit_immutable on public.internal_audit_log;
create trigger staging_ikugo_audit_immutable before update or delete on public.internal_audit_log
for each row execute function public.staging_ikugo_reject_audit_mutation();

-- New tables are server-only. Existing client policies/grants are left intact.
do $privileges$
declare table_name text; sequence_name text;
begin
  foreach table_name in array array[
    'china_stock_receipts','china_documents','pricing','pricing_eur','company_profile',
    'invoice_meta','ksef_documents','cash_flow_settings','cash_flow_expenses','internal_audit_log'
  ] loop
    execute format('alter table public.%I enable row level security',table_name);
    execute format('revoke all on table public.%I from public,anon,authenticated',table_name);
    if table_name='internal_audit_log' then
      execute format('revoke update,delete,truncate,references,trigger on table public.%I from service_role',table_name);
      execute format('grant select,insert on table public.%I to service_role',table_name);
    else
      execute format('grant select,insert,update,delete on table public.%I to service_role',table_name);
    end if;
  end loop;
  foreach table_name in array array[
    'products','stock','customers','orders','order_items','china_packages','china_items',
    'invoices','invoice_allocations','product_images','product_image_assignments'
  ] loop
    execute format('alter table public.%I enable row level security',table_name);
    execute format('grant select,insert,update,delete on table public.%I to service_role',table_name);
  end loop;
  foreach table_name in array array[
    'products','customers','orders','order_items','china_packages','china_items',
    'invoices','invoice_allocations','china_documents','cash_flow_expenses','product_images'
  ] loop
    sequence_name := pg_get_serial_sequence('public.' || table_name,'id');
    if sequence_name is not null then
      execute format('grant usage,select on sequence %s to service_role',sequence_name);
    end if;
  end loop;
end $privileges$;

-- Assert migration outcomes before committing. No fabricated business documents.
do $verify$
declare table_name text;
begin
  foreach table_name in array array[
    'products','stock','customers','orders','order_items','china_packages','china_items',
    'china_stock_receipts','china_documents','pricing','pricing_eur','company_profile',
    'invoices','invoice_meta','invoice_allocations','ksef_documents',
    'cash_flow_settings','cash_flow_expenses','product_images','product_image_assignments',
    'internal_audit_log'
  ] loop
    if to_regclass('public.' || table_name) is null
       or not has_table_privilege('service_role','public.' || table_name,'SELECT') then
      raise exception '009 contract verification failed for %',table_name;
    end if;
  end loop;
  if exists(select 1 from public.products where created_at is null)
     or exists(select 1 from public.customers where created_at is null)
     or exists(select 1 from public.orders where warehouse_issued is null)
     or exists(select 1 from public.invoices where payment_type is null) then
    raise exception '009 required field backfill failed';
  end if;
  if has_table_privilege('anon','public.internal_audit_log','SELECT')
     or has_table_privilege('authenticated','public.internal_audit_log','SELECT')
     or has_table_privilege('service_role','public.internal_audit_log','UPDATE') then
    raise exception '009 audit privileges are too broad';
  end if;
end $verify$;
notify pgrst,'reload schema';
commit;

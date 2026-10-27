-- Staging first. Apply to the existing Uchwyty project only after reviewing
-- current grants, RLS, backups and the application rollout order.
-- All new writes are performed by the trusted IKUGO backend (service_role).
begin;

create table if not exists public.architect_program_settings (
  version integer primary key,
  customer_discount_rate numeric(6,5) not null check (customer_discount_rate between 0 and 1),
  manufacturer_share numeric(6,5) not null check (manufacturer_share between 0 and 1),
  commission_rate numeric(6,5) not null check (commission_rate between 0 and 1),
  code_valid_days integer not null check (code_valid_days between 1 and 365),
  active boolean not null default false,
  created_at timestamptz not null default now()
);
insert into public.architect_program_settings
  (version, customer_discount_rate, manufacturer_share, commission_rate, code_valid_days, active)
values (1, 0.10, 0.50, 0.07, 90, true)
on conflict (version) do nothing;
create unique index if not exists architect_one_active_settings
  on public.architect_program_settings (active) where active;

create table if not exists public.architect_profiles (
  id uuid primary key references auth.users(id) on delete restrict,
  first_name text not null check (length(btrim(first_name)) between 1 and 100),
  last_name text not null check (length(btrim(last_name)) between 1 and 100),
  studio_name text not null check (length(btrim(studio_name)) between 1 and 200),
  email text not null,
  phone text not null,
  instagram text not null check (length(btrim(instagram)) between 2 and 250),
  website text,
  shipping_address text not null,
  nip text,
  status text not null default 'pending_verification'
    check (status in ('pending_verification','approved','rejected','suspended')),
  sample_entitlements smallint not null default 1
    check (sample_entitlements between 0 and 20),
  created_at timestamptz not null default now(),
  reviewed_at timestamptz,
  reviewed_by text
);
create index if not exists architect_profiles_status_created
  on public.architect_profiles(status, created_at desc);

create table if not exists public.distributor_program_memberships (
  customer_id bigint primary key references public.customers(id) on delete restrict,
  active boolean not null default false,
  store_name text not null,
  city text not null,
  store_address text,
  contact text,
  latitude numeric(9,6),
  longitude numeric(9,6),
  created_at timestamptz not null default now()
);

create table if not exists public.architect_product_details (
  product_id bigint primary key references public.products(id) on delete restrict,
  description text,
  material text not null default 'Lity mosiądz',
  mounting_spacing text,
  dimensions text,
  finish text,
  technical_data jsonb not null default '{}'::jsonb,
  model_3d_url text,
  vat_rate numeric(6,5) not null default 0.23 check (vat_rate between 0 and 1),
  retail_gross_override numeric(14,2) check (retail_gross_override > 0),
  updated_at timestamptz not null default now()
);

create table if not exists public.architect_sample_orders (
  id uuid primary key default gen_random_uuid(),
  architect_id uuid not null references public.architect_profiles(id) on delete restrict,
  entitlement_no smallint not null check (entitlement_no > 0),
  shipping_address text not null,
  catalog_included boolean not null default true check (catalog_included),
  status text not null default 'requested'
    check (status in ('requested','preparing','shipped','delivered','cancelled')),
  tracking_no text,
  carrier text,
  created_at timestamptz not null default now(),
  shipped_at timestamptz,
  unique (architect_id, entitlement_no)
);
create index if not exists architect_samples_queue
  on public.architect_sample_orders(status, created_at);
create table if not exists public.architect_sample_items (
  id bigint generated always as identity primary key,
  sample_order_id uuid not null references public.architect_sample_orders(id) on delete restrict,
  product_id bigint not null references public.products(id) on delete restrict,
  qty integer not null default 1 check (qty = 1),
  unique(sample_order_id, product_id)
);
create table if not exists public.architect_sample_stock_issues (
  sample_order_id uuid not null references public.architect_sample_orders(id) on delete restrict,
  product_id bigint not null references public.products(id) on delete restrict,
  qty integer not null check (qty > 0),
  old_stock_qty integer not null,
  new_stock_qty integer not null,
  created_at timestamptz not null default now(),
  primary key(sample_order_id,product_id)
);

create table if not exists public.architect_codes (
  id uuid primary key default gen_random_uuid(),
  architect_id uuid not null references public.architect_profiles(id) on delete restrict,
  distributor_id bigint not null references public.customers(id) on delete restrict,
  code text not null unique check (code ~ '^[A-Z0-9_]+-[0-9]{4}$'),
  status text not null default 'active'
    check (status in ('active','redeemed','expired','cancelled')),
  program_version integer not null references public.architect_program_settings(version),
  created_at timestamptz not null default now(),
  expires_at timestamptz not null,
  check (expires_at > created_at)
);
create index if not exists architect_codes_owner_created
  on public.architect_codes(architect_id, created_at desc);
create index if not exists architect_codes_distributor_status
  on public.architect_codes(distributor_id, status, expires_at);

create table if not exists public.architect_code_redemptions (
  id uuid primary key default gen_random_uuid(),
  code_id uuid not null unique references public.architect_codes(id) on delete restrict,
  order_id bigint not null unique references public.orders(id) on delete restrict,
  distributor_id bigint not null references public.customers(id) on delete restrict,
  redeemed_by_user_id uuid not null references auth.users(id) on delete restrict,
  idempotency_key uuid not null unique,
  request_items jsonb not null,
  program_version integer not null references public.architect_program_settings(version),
  created_at timestamptz not null default now()
);
create table if not exists public.architect_sale_lines (
  id bigint generated always as identity primary key,
  redemption_id uuid not null references public.architect_code_redemptions(id) on delete restrict,
  order_item_id bigint not null unique references public.order_items(id) on delete restrict,
  product_id bigint not null references public.products(id) on delete restrict,
  qty integer not null check (qty > 0),
  vat_rate numeric(6,5) not null check (vat_rate between 0 and 1),
  wholesale_net_before numeric(14,2) not null check (wholesale_net_before >= 0),
  manufacturer_discount_net numeric(14,2) not null check (manufacturer_discount_net >= 0),
  wholesale_net_after numeric(14,2) not null check (wholesale_net_after >= 0),
  retail_gross_before numeric(14,2) not null check (retail_gross_before >= 0),
  retail_gross_after numeric(14,2) not null check (retail_gross_after >= 0),
  retail_net_after numeric(14,2) not null check (retail_net_after >= 0),
  commission_estimate numeric(14,2) not null check (commission_estimate >= 0),
  program_version integer not null references public.architect_program_settings(version)
);
create table if not exists public.architect_commission_ledger (
  id uuid primary key default gen_random_uuid(),
  architect_id uuid not null references public.architect_profiles(id) on delete restrict,
  sale_line_id bigint references public.architect_sale_lines(id) on delete restrict,
  invoice_id bigint references public.invoices(id) on delete restrict,
  sold_qty integer check (sold_qty > 0),
  sale_reference text,
  sale_date date,
  retail_gross_before numeric(14,2),
  retail_gross_after numeric(14,2),
  retail_net_after numeric(14,2),
  amount_net numeric(14,2) not null,
  entry_type text not null check (entry_type in ('accrual','return_adjustment','manual_adjustment','payout')),
  status text not null check (status in ('pending','approved','settled','paid','adjusted')),
  reason text,
  reversal_of uuid references public.architect_commission_ledger(id) on delete restrict,
  approved_at timestamptz,
  settled_at timestamptz,
  created_at timestamptz not null default now(),
  created_by text not null
);
create unique index if not exists architect_commission_once_per_sale_reference
  on public.architect_commission_ledger(sale_line_id, sale_reference)
  where entry_type = 'accrual';
create index if not exists architect_commission_owner_date
  on public.architect_commission_ledger(architect_id, created_at desc);

create table if not exists public.architect_monthly_settlements (
  id uuid primary key default gen_random_uuid(),
  architect_id uuid not null references public.architect_profiles(id) on delete restrict,
  period_month date not null check (extract(day from period_month) = 1),
  batch_no integer not null default 1 check (batch_no > 0),
  status text not null default 'draft'
    check (status in ('draft','approved','paid','corrected')),
  total_net numeric(14,2) not null default 0,
  created_at timestamptz not null default now(),
  unique(architect_id, period_month, batch_no)
);
alter table public.architect_commission_ledger
  add column if not exists settlement_id uuid
  references public.architect_monthly_settlements(id) on delete restrict;
create table if not exists public.architect_payouts (
  id uuid primary key default gen_random_uuid(),
  settlement_id uuid not null unique references public.architect_monthly_settlements(id) on delete restrict,
  amount_net numeric(14,2) not null check (amount_net >= 0),
  paid_at timestamptz,
  reference text,
  created_at timestamptz not null default now()
);
create table if not exists public.architect_audit_events (
  id bigint generated always as identity primary key,
  actor_id text not null,
  action text not null,
  entity_type text not null,
  entity_id text not null,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists architect_audit_entity_date
  on public.architect_audit_events(entity_type, entity_id, created_at desc);

-- Keep all mutations behind the trusted backend. Direct authenticated reads
-- are limited to the architect's own records, and never expose stock/pricing.
do $$
declare table_name text;
begin
  foreach table_name in array array[
    'architect_program_settings','architect_profiles','distributor_program_memberships',
    'architect_product_details',
    'architect_sample_orders','architect_sample_items','architect_sample_stock_issues','architect_codes',
    'architect_code_redemptions','architect_sale_lines','architect_commission_ledger',
    'architect_monthly_settlements','architect_payouts','architect_audit_events'
  ] loop
    execute format('alter table public.%I enable row level security', table_name);
    execute format('revoke all on public.%I from anon, authenticated', table_name);
  end loop;
end $$;
grant select on public.architect_profiles, public.architect_sample_orders,
  public.architect_sample_items, public.architect_codes,
  public.architect_commission_ledger, public.architect_monthly_settlements
  to authenticated;
create policy architect_profile_own_read on public.architect_profiles
  for select to authenticated using (id = (select auth.uid()));
create policy architect_samples_own_read on public.architect_sample_orders
  for select to authenticated using (architect_id = (select auth.uid()));
create policy architect_sample_items_own_read on public.architect_sample_items
  for select to authenticated using (
    exists (select 1 from public.architect_sample_orders s
            where s.id = sample_order_id and s.architect_id = (select auth.uid()))
  );
create policy architect_codes_own_read on public.architect_codes
  for select to authenticated using (architect_id = (select auth.uid()));
create policy architect_commission_own_read on public.architect_commission_ledger
  for select to authenticated using (architect_id = (select auth.uid()));
create policy architect_settlements_own_read on public.architect_monthly_settlements
  for select to authenticated using (architect_id = (select auth.uid()));

create or replace function public.architect_submit_samples(
  p_architect_id uuid, p_product_ids bigint[], p_shipping_address text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_entitlement smallint;
  v_next smallint;
  v_order uuid;
begin
  if coalesce(array_length(p_product_ids, 1), 0) not between 1 and 3
     or (select count(distinct x) from unnest(p_product_ids) x) <> array_length(p_product_ids, 1)
     or length(btrim(coalesce(p_shipping_address,''))) < 8 then
    raise exception 'Invalid samples request';
  end if;
  select sample_entitlements into v_entitlement
    from public.architect_profiles
    where id = p_architect_id and status = 'approved' for update;
  if v_entitlement is null then raise exception 'Architect not approved'; end if;
  select coalesce(max(entitlement_no),0) + 1 into v_next
    from public.architect_sample_orders where architect_id = p_architect_id;
  if v_next > v_entitlement then raise exception 'Sample entitlement used'; end if;
  if exists (select 1 from unnest(p_product_ids) x
             left join public.products p on p.id = x
             where p.id is null or coalesce(p.archived,false)) then
    raise exception 'Inactive product';
  end if;
  insert into public.architect_sample_orders(architect_id, entitlement_no, shipping_address)
    values(p_architect_id, v_next, btrim(p_shipping_address)) returning id into v_order;
  insert into public.architect_sample_items(sample_order_id, product_id)
    select v_order, x from unnest(p_product_ids) x;
  insert into public.architect_audit_events(actor_id, action, entity_type, entity_id)
    values(p_architect_id::text, 'sample_requested', 'architect_sample_order', v_order::text);
  return v_order;
end $$;
revoke all on function public.architect_submit_samples(uuid,bigint[],text) from public, anon, authenticated;
grant execute on function public.architect_submit_samples(uuid,bigint[],text) to service_role;

create or replace function public.architect_change_sample_status(
  p_sample_id uuid, p_status text, p_tracking_no text, p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_sample public.architect_sample_orders%rowtype; v_item record; v_old_qty integer;
begin
  if length(btrim(coalesce(p_actor,''))) < 2 then raise exception 'Actor required'; end if;
  select * into v_sample from public.architect_sample_orders
    where id=p_sample_id for update;
  if not found then raise exception 'Sample order not found'; end if;
  if not ((p_status='preparing' and v_sample.status='requested')
       or (p_status='shipped' and v_sample.status='preparing'
           and length(btrim(coalesce(p_tracking_no,'')))>=5)
       or (p_status='delivered' and v_sample.status='shipped')
       or (p_status='cancelled' and v_sample.status in ('requested','preparing'))) then
    raise exception 'Invalid sample status transition';
  end if;
  if p_status='shipped' then
    for v_item in select product_id,qty from public.architect_sample_items
                  where sample_order_id=p_sample_id order by product_id loop
      update public.stock set qty=qty-v_item.qty
        where product_id=v_item.product_id and qty>=v_item.qty
        returning qty+v_item.qty into v_old_qty;
      if not found then raise exception 'Insufficient stock for sample product %',v_item.product_id; end if;
      insert into public.architect_sample_stock_issues(
        sample_order_id,product_id,qty,old_stock_qty,new_stock_qty)
      values(p_sample_id,v_item.product_id,v_item.qty,v_old_qty,v_old_qty-v_item.qty);
    end loop;
  end if;
  update public.architect_sample_orders
    set status=p_status,
        tracking_no=case when p_status='shipped' then btrim(p_tracking_no) else tracking_no end,
        shipped_at=case when p_status='shipped' then now() else shipped_at end
    where id=p_sample_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'sample_status_changed','architect_sample_order',p_sample_id::text,
           jsonb_build_object('old_status',v_sample.status,'new_status',p_status,
                              'tracking_no',case when p_status='shipped' then p_tracking_no else null end));
end $$;
revoke all on function public.architect_change_sample_status(uuid,text,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_change_sample_status(uuid,text,text,text)
  to service_role;

-- Issue the code and its audit record in one transaction. The trusted server
-- generates the four digits with a cryptographic random source and retries
-- unique-key collisions.
create or replace function public.architect_issue_code(
  p_architect_id uuid, p_distributor_id bigint, p_code text
) returns public.architect_codes language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_rules public.architect_program_settings%rowtype;
  v_code public.architect_codes%rowtype;
begin
  if p_code !~ '^[A-Z0-9_]{1,28}-[0-9]{4}$'
     or not exists(select 1 from public.architect_profiles
                   where id=p_architect_id and status='approved')
     or not exists(select 1 from public.distributor_program_memberships
                   where customer_id=p_distributor_id and active) then
    raise exception 'Code request unavailable';
  end if;
  select * into v_rules from public.architect_program_settings
    where active for update;
  if not found then raise exception 'Program settings unavailable'; end if;
  insert into public.architect_codes(architect_id,distributor_id,code,
    program_version,expires_at)
  values(p_architect_id,p_distributor_id,p_code,v_rules.version,
         now()+make_interval(days=>v_rules.code_valid_days))
  returning * into v_code;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id)
    values(p_architect_id::text,'code_created','architect_code',v_code.id::text);
  return v_code;
end $$;
revoke all on function public.architect_issue_code(uuid,bigint,text)
  from public, anon, authenticated;
grant execute on function public.architect_issue_code(uuid,bigint,text)
  to service_role;

-- One PostgreSQL transaction owns code redemption, the order, all price
-- snapshots and audit. The browser passes only product IDs and quantities to
-- IKUGO; the trusted server resolves distributor identity from client_accounts.
create or replace function public.architect_create_discounted_order(
  p_auth_user_id uuid, p_customer_id bigint, p_code text,
  p_idempotency_key uuid, p_items jsonb, p_note text default ''
) returns bigint language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_code public.architect_codes%rowtype;
  v_rules public.architect_program_settings%rowtype;
  v_customer public.customers%rowtype;
  v_order_id bigint;
  v_order_item_id bigint;
  v_line jsonb;
  v_product_id bigint;
  v_qty integer;
  v_product public.products%rowtype;
  v_price numeric;
  v_vat numeric;
  v_retail_gross numeric;
  v_retail_after numeric;
  v_net_discount numeric;
  v_manufacturer_discount numeric;
  v_wholesale_after numeric;
  v_retail_net_after numeric;
  v_redemption_id uuid;
begin
  if p_items is null or jsonb_typeof(p_items) <> 'array'
     or jsonb_array_length(p_items) not between 1 and 100 then
    raise exception 'Invalid order lines';
  end if;
  if not exists (select 1 from public.client_accounts a
                 where a.user_id = p_auth_user_id and a.customer_id = p_customer_id
                   and a.is_active) then
    raise exception 'Distributor access denied';
  end if;
  select * into v_customer from public.customers where id = p_customer_id;
  if not found or lower(coalesce(v_customer.price_list,'pln')) <> 'pln' then
    raise exception 'Only PLN distributors can use architect codes';
  end if;
  select id into v_order_id from public.orders
    where idempotency_key = p_idempotency_key::text;
  if found then
    if exists (select 1 from public.architect_code_redemptions r
               join public.architect_codes c on c.id = r.code_id
               where r.order_id = v_order_id and r.distributor_id = p_customer_id
                 and r.redeemed_by_user_id = p_auth_user_id
                 and c.code = upper(btrim(p_code)) and r.request_items = p_items) then
      return v_order_id;
    end if;
    raise exception 'Idempotency key belongs to a different request';
  end if;
  select * into v_code from public.architect_codes
    where code = upper(btrim(p_code)) for update;
  if not found or v_code.status <> 'active' or v_code.expires_at <= now()
     or v_code.distributor_id <> p_customer_id
     or not exists(select 1 from public.architect_profiles p
                   where p.id = v_code.architect_id and p.status = 'approved')
     or not exists(select 1 from public.distributor_program_memberships d
                   where d.customer_id = p_customer_id and d.active) then
    raise exception 'Architect code unavailable';
  end if;
  select * into v_rules from public.architect_program_settings
    where version = v_code.program_version;
  if not found then raise exception 'Program rules unavailable'; end if;

  insert into public.orders(order_no, customer_id, customer_name, customer_address,
    customer_phone, customer_email, status, note, created_at, qr_data_url,
    idempotency_key, currency, price_list)
  values('TEMP-' || gen_random_uuid()::text, p_customer_id, v_customer.name,
    v_customer.address, v_customer.phone, v_customer.email, 'new', p_note,
    to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'), '',
    p_idempotency_key::text, 'PLN', 'pln')
  returning id into v_order_id;
  update public.orders set order_no =
    'ZAM-' || to_char(now() at time zone 'Europe/Warsaw','YYMMDD') || v_order_id::text
    where id = v_order_id;
  insert into public.architect_code_redemptions(code_id, order_id, distributor_id,
    redeemed_by_user_id,idempotency_key, request_items, program_version)
  values(v_code.id, v_order_id, p_customer_id, p_auth_user_id,
         p_idempotency_key, p_items, v_rules.version)
  returning id into v_redemption_id;

  for v_line in select value from jsonb_array_elements(p_items) loop
    if jsonb_typeof(v_line) <> 'object'
       or coalesce(v_line->>'product_id','') !~ '^[1-9][0-9]*$'
       or coalesce(v_line->>'qty','') !~ '^[1-9][0-9]*$' then
      raise exception 'Invalid order line';
    end if;
    v_product_id := (v_line->>'product_id')::bigint;
    v_qty := (v_line->>'qty')::integer;
    if v_qty > 10000 or exists(select 1 from public.order_items
                               where order_id = v_order_id and product_id = v_product_id) then
      raise exception 'Duplicate or excessive order line';
    end if;
    select * into v_product from public.products
      where id = v_product_id and not coalesce(archived,false);
    if not found then raise exception 'Inactive product'; end if;
    select net_price into v_price from public.client_stock_catalog_v
      where product_id = v_product_id;
    if v_price is null or v_price <= 0 then raise exception 'Product price unavailable'; end if;
    select coalesce(d.vat_rate,0.23), d.retail_gross_override
      into v_vat, v_retail_gross from public.architect_product_details d
      where d.product_id = v_product_id;
    v_vat := coalesce(v_vat,0.23);
    if v_vat <> 0.23 then
      raise exception 'Nonstandard VAT requires invoice workflow review';
    end if;
    v_retail_gross := coalesce(v_retail_gross,
      round(v_price * 1.45 * (1 + v_vat),2));
    v_retail_after := v_retail_gross - round(v_retail_gross * v_rules.customer_discount_rate,2);
    v_net_discount := round((v_retail_gross - v_retail_after) / (1 + v_vat),2);
    v_manufacturer_discount := round(v_net_discount * v_rules.manufacturer_share,2);
    v_wholesale_after := round(v_price - v_manufacturer_discount,2);
    if v_wholesale_after < 0 then
      raise exception 'Discount requires manual review';
    end if;
    v_retail_net_after := round(v_retail_after / (1 + v_vat),2);
    insert into public.order_items(order_id,product_id,sku,qty,
      unit_net_price,unit_gross_price,unit_retail_price,currency,created_at)
    values(v_order_id,v_product_id,v_product.sku,v_qty,v_wholesale_after,
      round(v_wholesale_after*(1+v_vat),2),v_retail_after,'PLN',
      to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'))
    returning id into v_order_item_id;
    insert into public.architect_sale_lines(redemption_id,order_item_id,product_id,qty,
      vat_rate,wholesale_net_before,manufacturer_discount_net,wholesale_net_after,
      retail_gross_before,retail_gross_after,retail_net_after,commission_estimate,program_version)
    values(v_redemption_id,v_order_item_id,v_product_id,v_qty,v_vat,
      round(v_price*v_qty,2),round(v_manufacturer_discount*v_qty,2),
      round(v_wholesale_after*v_qty,2),round(v_retail_gross*v_qty,2),
      round(v_retail_after*v_qty,2),round(v_retail_net_after*v_qty,2),
      round(v_retail_net_after*v_rules.commission_rate*v_qty,2),v_rules.version);
  end loop;
  update public.architect_codes set status = 'redeemed' where id = v_code.id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_auth_user_id::text,'code_redeemed','order',v_order_id::text,
           jsonb_build_object('code_id',v_code.id,'distributor_id',p_customer_id));
  return v_order_id;
end $$;
revoke all on function public.architect_create_discounted_order(uuid,bigint,text,uuid,jsonb,text)
  from public, anon, authenticated;
grant execute on function public.architect_create_discounted_order(uuid,bigint,text,uuid,jsonb,text)
  to service_role;

-- An issued B2B invoice does not itself prove retail sell-through. A human
-- must enter a receipt/reference and the actually sold quantity. The function
-- bounds credited units by invoice allocations and all prior accruals.
create or replace function public.architect_record_verified_sale(
  p_sale_line_id bigint, p_invoice_id bigint, p_distributor_id bigint,
  p_sold_qty integer, p_sale_date date, p_sale_reference text,
  p_retail_gross_before numeric, p_retail_gross_after numeric, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp
set timezone = 'Europe/Warsaw' as $$
declare
  v_line public.architect_sale_lines%rowtype;
  v_architect_id uuid;
  v_invoiced integer;
  v_accrued integer;
  v_previous_amount numeric;
  v_accrued_for_invoice integer;
  v_rate numeric;
  v_ledger_id uuid;
  v_order_id bigint;
  v_distributor_id bigint;
  v_created_at timestamptz;
begin
  if p_sold_qty is null or p_sold_qty <= 0
     or length(btrim(coalesce(p_sale_reference,''))) < 3
     or length(btrim(coalesce(p_actor,''))) < 2
     or p_sale_date is null or p_sale_date > current_date then
    raise exception 'Retail sale evidence required';
  end if;
  select * into v_line from public.architect_sale_lines
    where id = p_sale_line_id for update;
  if not found then raise exception 'Sale line not found'; end if;
  select r.order_id, r.distributor_id, r.created_at
    into v_order_id, v_distributor_id, v_created_at
    from public.architect_code_redemptions r where r.id = v_line.redemption_id;
  if v_distributor_id is distinct from p_distributor_id
     or p_sale_date < v_created_at::date then
    raise exception 'Sale does not belong to this distributor or date';
  end if;
  if p_retail_gross_before is distinct from
       round(v_line.retail_gross_before / v_line.qty * p_sold_qty,2)
     or p_retail_gross_after is distinct from
       round(v_line.retail_gross_after / v_line.qty * p_sold_qty,2) then
    raise exception 'Retail receipt totals differ from the code price snapshot';
  end if;
  if not exists(select 1 from public.invoices i
                where i.id = p_invoice_id and i.order_id = v_order_id
                  and i.publication_state = 'complete') then
    raise exception 'A completed B2B invoice is required';
  end if;
  select coalesce(sum(a.qty),0) into v_invoiced
    from public.invoice_allocations a
    where a.invoice_id = p_invoice_id and a.order_item_id = v_line.order_item_id;
  if v_invoiced <= 0 then raise exception 'Invoice does not cover this order line'; end if;
  select coalesce(sum(l.sold_qty),0), coalesce(sum(l.amount_net),0)
    into v_accrued, v_previous_amount
    from public.architect_commission_ledger l
    where l.sale_line_id = p_sale_line_id and l.entry_type = 'accrual';
  select coalesce(sum(l.sold_qty),0) into v_accrued_for_invoice
    from public.architect_commission_ledger l
    where l.sale_line_id = p_sale_line_id and l.invoice_id = p_invoice_id
      and l.entry_type = 'accrual';
  if v_accrued_for_invoice + p_sold_qty > v_invoiced
     or v_accrued + p_sold_qty > v_line.qty
     or v_accrued + p_sold_qty > (
       select coalesce(sum(a.qty),0) from public.invoice_allocations a
       where a.order_item_id = v_line.order_item_id
         and exists(select 1 from public.invoices i where i.id = a.invoice_id
                    and i.publication_state = 'complete')
     ) then
    raise exception 'Sold units exceed invoiced units';
  end if;
  select c.architect_id into v_architect_id
    from public.architect_code_redemptions r
    join public.architect_codes c on c.id = r.code_id
    where r.id = v_line.redemption_id;
  select commission_rate into v_rate from public.architect_program_settings
    where version = v_line.program_version;
  -- Round the cumulative commission once, then credit only the difference.
  -- Partial invoices therefore sum to the same amount as one full invoice.
  insert into public.architect_commission_ledger(
    architect_id,sale_line_id,invoice_id,sold_qty,sale_reference,sale_date,
    retail_gross_before,retail_gross_after,retail_net_after,amount_net,
    entry_type,status,reason,created_by)
  values(v_architect_id,p_sale_line_id,p_invoice_id,p_sold_qty,btrim(p_sale_reference),
    p_sale_date,p_retail_gross_before,p_retail_gross_after,
    round(p_retail_gross_after / (1 + v_line.vat_rate),2),
    round((v_line.retail_net_after / v_line.qty) * (v_accrued + p_sold_qty) * v_rate,2)
      - v_previous_amount,
    'accrual','pending','Potwierdzona sprzedaż detaliczna',p_actor)
  returning id into v_ledger_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'retail_sale_verified','architect_commission_ledger',v_ledger_id::text,
           jsonb_build_object('sale_line_id',p_sale_line_id,'invoice_id',p_invoice_id,
                              'sold_qty',p_sold_qty,'reference',p_sale_reference));
  return v_ledger_id;
end $$;
revoke all on function public.architect_record_verified_sale(bigint,bigint,bigint,integer,date,text,numeric,numeric,text)
  from public, anon, authenticated;
grant execute on function public.architect_record_verified_sale(bigint,bigint,bigint,integer,date,text,numeric,numeric,text)
  to service_role;

create or replace function public.architect_approve_commission(
  p_ledger_id uuid, p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
begin
  if length(btrim(coalesce(p_actor,''))) < 2 then raise exception 'Actor required'; end if;
  update public.architect_commission_ledger set status='approved',approved_at=now()
    where id=p_ledger_id and status='pending';
  if not found then raise exception 'Pending commission not found'; end if;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id)
    values(p_actor,'commission_approved','architect_commission_ledger',p_ledger_id::text);
end $$;
revoke all on function public.architect_approve_commission(uuid,text)
  from public, anon, authenticated;
grant execute on function public.architect_approve_commission(uuid,text) to service_role;

-- Returns remain separate negative entries, including after the original
-- commission has been paid. A later month's settlement carries the correction.
create or replace function public.architect_record_return(
  p_accrual_id uuid, p_return_qty integer, p_return_date date,
  p_reference text, p_reason text, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp as $$
declare
  v_original public.architect_commission_ledger%rowtype;
  v_refunded_qty integer;
  v_refunded_amount numeric;
  v_amount numeric;
  v_new_id uuid;
begin
  if p_return_qty is null or p_return_qty <= 0
     or p_return_date is null or p_return_date > current_date
     or length(btrim(coalesce(p_reference,''))) < 3
     or length(btrim(coalesce(p_reason,''))) < 5
     or length(btrim(coalesce(p_actor,''))) < 2 then
    raise exception 'Return evidence required';
  end if;
  select * into v_original from public.architect_commission_ledger
    where id=p_accrual_id and entry_type='accrual' for update;
  if not found or p_return_date < v_original.sale_date then
    raise exception 'Original sale not found or return predates sale';
  end if;
  select coalesce(sum(sold_qty),0),coalesce(sum(amount_net),0)
    into v_refunded_qty,v_refunded_amount
    from public.architect_commission_ledger
    where reversal_of=p_accrual_id and entry_type='return_adjustment';
  if v_refunded_qty+p_return_qty > v_original.sold_qty then
    raise exception 'Returned units exceed sold units';
  end if;
  v_amount := case when v_refunded_qty+p_return_qty=v_original.sold_qty
    then -(v_original.amount_net+v_refunded_amount)
    else -round(v_original.amount_net/v_original.sold_qty*p_return_qty,2) end;
  insert into public.architect_commission_ledger(
    architect_id,sale_line_id,invoice_id,sold_qty,sale_reference,sale_date,
    amount_net,entry_type,status,reason,reversal_of,created_by)
  values(v_original.architect_id,v_original.sale_line_id,v_original.invoice_id,
    p_return_qty,btrim(p_reference),p_return_date,v_amount,
    'return_adjustment','pending',btrim(p_reason),p_accrual_id,p_actor)
  returning id into v_new_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'return_recorded','architect_commission_ledger',v_new_id::text,
           jsonb_build_object('original_id',p_accrual_id,'qty',p_return_qty,
                              'reference',p_reference));
  return v_new_id;
end $$;
revoke all on function public.architect_record_return(uuid,integer,date,text,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_record_return(uuid,integer,date,text,text,text)
  to service_role;

create or replace function public.architect_create_monthly_settlement(
  p_architect_id uuid, p_period_month date, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_id uuid; v_total numeric; v_count integer; v_batch integer;
begin
  if p_period_month is null or extract(day from p_period_month) <> 1
     or p_period_month > date_trunc('month',current_date)::date
     or length(btrim(coalesce(p_actor,''))) < 2 then
    raise exception 'Invalid settlement period';
  end if;
  -- A later approval for an already closed sale month becomes a supplemental
  -- batch for that same month. Historical approved or paid batches stay fixed.
  perform 1 from public.architect_profiles where id=p_architect_id for update;
  if not found then raise exception 'Architect profile unavailable'; end if;
  select coalesce(max(batch_no),0)+1 into v_batch
    from public.architect_monthly_settlements
    where architect_id=p_architect_id and period_month=p_period_month;
  insert into public.architect_monthly_settlements(architect_id,period_month,batch_no)
    values(p_architect_id,p_period_month,v_batch) returning id into v_id;
  update public.architect_commission_ledger
    set settlement_id=v_id
    where architect_id=p_architect_id and status='approved'
      and settlement_id is null and sale_date>=p_period_month
      and sale_date<(p_period_month+interval '1 month')::date;
  get diagnostics v_count=row_count;
  if v_count=0 then raise exception 'No approved entries for this month'; end if;
  select coalesce(sum(amount_net),0) into v_total
    from public.architect_commission_ledger where settlement_id=v_id;
  update public.architect_monthly_settlements set total_net=v_total where id=v_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'settlement_created','architect_monthly_settlement',v_id::text,
           jsonb_build_object('entries',v_count,'total_net',v_total,'batch_no',v_batch));
  return v_id;
end $$;
revoke all on function public.architect_create_monthly_settlement(uuid,date,text)
  from public, anon, authenticated;
grant execute on function public.architect_create_monthly_settlement(uuid,date,text)
  to service_role;

create or replace function public.architect_approve_monthly_settlement(
  p_settlement_id uuid, p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_total numeric;
begin
  if length(btrim(coalesce(p_actor,''))) < 2 then raise exception 'Actor required'; end if;
  select coalesce(sum(amount_net),0) into v_total
    from public.architect_commission_ledger where settlement_id=p_settlement_id;
  update public.architect_monthly_settlements set status='approved'
    where id=p_settlement_id and status='draft' and total_net=v_total;
  if not found then raise exception 'Draft settlement changed or unavailable'; end if;
  update public.architect_commission_ledger set status='settled',settled_at=now()
    where settlement_id=p_settlement_id and status='approved';
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id)
    values(p_actor,'settlement_approved','architect_monthly_settlement',p_settlement_id::text);
end $$;
revoke all on function public.architect_approve_monthly_settlement(uuid,text)
  from public, anon, authenticated;
grant execute on function public.architect_approve_monthly_settlement(uuid,text)
  to service_role;

-- Records an external/manual payment reference; it never initiates a transfer.
create or replace function public.architect_record_payout(
  p_settlement_id uuid, p_reference text, p_paid_at timestamptz, p_actor text
) returns uuid language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_total numeric; v_id uuid;
begin
  if length(btrim(coalesce(p_reference,''))) < 3
     or length(btrim(coalesce(p_actor,''))) < 2
     or p_paid_at is null or p_paid_at>now() then
    raise exception 'Payment reference and date required';
  end if;
  select total_net into v_total from public.architect_monthly_settlements
    where id=p_settlement_id and status='approved' for update;
  if not found or v_total<=0 then raise exception 'Approved positive settlement required'; end if;
  insert into public.architect_payouts(settlement_id,amount_net,paid_at,reference)
    values(p_settlement_id,v_total,p_paid_at,btrim(p_reference)) returning id into v_id;
  update public.architect_monthly_settlements set status='paid' where id=p_settlement_id;
  update public.architect_commission_ledger set status='paid'
    where settlement_id=p_settlement_id and status='settled';
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'payout_recorded','architect_payout',v_id::text,
           jsonb_build_object('settlement_id',p_settlement_id,
                              'amount_net',v_total,'reference',p_reference));
  return v_id;
end $$;
revoke all on function public.architect_record_payout(uuid,text,timestamptz,text)
  from public, anon, authenticated;
grant execute on function public.architect_record_payout(uuid,text,timestamptz,text)
  to service_role;

create or replace function public.architect_review_profile(
  p_architect_id uuid, p_status text, p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_old text;
begin
  if p_status not in ('approved','rejected','suspended')
     or length(btrim(coalesce(p_actor,'')))<2 then
    raise exception 'Invalid review';
  end if;
  select status into v_old from public.architect_profiles
    where id=p_architect_id for update;
  if not found or v_old=p_status then raise exception 'Profile review unavailable'; end if;
  update public.architect_profiles set status=p_status,reviewed_at=now(),reviewed_by=p_actor
    where id=p_architect_id;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'profile_reviewed','architect_profile',p_architect_id::text,
           jsonb_build_object('old_status',v_old,'new_status',p_status));
end $$;
revoke all on function public.architect_review_profile(uuid,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_review_profile(uuid,text,text) to service_role;

create or replace function public.architect_grant_extra_samples(
  p_architect_id uuid, p_reason text, p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
declare v_count integer;
begin
  if length(btrim(coalesce(p_reason,'')))<10
     or length(btrim(coalesce(p_actor,'')))<2 then
    raise exception 'Documented reason required';
  end if;
  update public.architect_profiles
    set sample_entitlements=sample_entitlements+1
    where id=p_architect_id and status='approved' and sample_entitlements<20
    returning sample_entitlements into v_count;
  if not found then raise exception 'Extra samples unavailable'; end if;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'sample_repeat_authorized','architect_profile',p_architect_id::text,
           jsonb_build_object('reason',p_reason,'new_entitlements',v_count));
end $$;
revoke all on function public.architect_grant_extra_samples(uuid,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_grant_extra_samples(uuid,text,text)
  to service_role;

create or replace function public.architect_set_distributor(
  p_customer_id bigint,p_active boolean,p_store_name text,p_city text,
  p_store_address text,p_contact text,p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
begin
  if length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Actor required'; end if;
  if p_active then
    if length(btrim(coalesce(p_store_name,'')))<2 or length(btrim(coalesce(p_city,'')))<2
       or not exists(select 1 from public.customers
                     where id=p_customer_id and lower(coalesce(price_list,'pln'))='pln') then
      raise exception 'Active PLN distributor and store details required';
    end if;
    insert into public.distributor_program_memberships(
      customer_id,active,store_name,city,store_address,contact)
    values(p_customer_id,true,btrim(p_store_name),btrim(p_city),
           p_store_address,p_contact)
    on conflict(customer_id) do update set active=true,
      store_name=excluded.store_name,city=excluded.city,
      store_address=excluded.store_address,contact=excluded.contact;
  else
    update public.distributor_program_memberships set active=false
      where customer_id=p_customer_id and active;
    if not found then raise exception 'Active distributor not found'; end if;
  end if;
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,case when p_active then 'distributor_enabled' else 'distributor_disabled' end,
           'distributor_program_membership',p_customer_id::text,
           jsonb_build_object('store_name',p_store_name,'city',p_city));
end $$;
revoke all on function public.architect_set_distributor(bigint,boolean,text,text,text,text,text)
  from public, anon, authenticated;
grant execute on function public.architect_set_distributor(bigint,boolean,text,text,text,text,text)
  to service_role;

create or replace function public.architect_upsert_product_details(
  p_product_id bigint,p_description text,p_material text,p_mounting_spacing text,
  p_dimensions text,p_finish text,p_technical_data jsonb,p_model_3d_url text,
  p_vat_rate numeric,p_retail_gross_override numeric,p_actor text
) returns void language plpgsql security definer
set search_path = public, pg_temp as $$
begin
  if not exists(select 1 from public.products where id=p_product_id and not coalesce(archived,false))
     or length(btrim(coalesce(p_material,'')))<2
     or p_vat_rate not between 0 and 1
     or (p_retail_gross_override is not null and p_retail_gross_override<=0)
     or (length(btrim(coalesce(p_model_3d_url,'')))>0 and p_model_3d_url !~ '^https://')
     or jsonb_typeof(coalesce(p_technical_data,'{}'::jsonb))<>'object'
     or length(btrim(coalesce(p_actor,'')))<2 then
    raise exception 'Invalid product details';
  end if;
  insert into public.architect_product_details(
    product_id,description,material,mounting_spacing,dimensions,finish,
    technical_data,model_3d_url,vat_rate,retail_gross_override,updated_at)
  values(p_product_id,p_description,btrim(p_material),p_mounting_spacing,
    p_dimensions,p_finish,coalesce(p_technical_data,'{}'::jsonb),
    nullif(btrim(p_model_3d_url),''),p_vat_rate,p_retail_gross_override,now())
  on conflict(product_id) do update set
    description=excluded.description,material=excluded.material,
    mounting_spacing=excluded.mounting_spacing,dimensions=excluded.dimensions,
    finish=excluded.finish,technical_data=excluded.technical_data,
    model_3d_url=excluded.model_3d_url,vat_rate=excluded.vat_rate,
    retail_gross_override=excluded.retail_gross_override,updated_at=now();
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
    values(p_actor,'product_details_updated','architect_product_details',p_product_id::text,
           jsonb_build_object('vat_rate',p_vat_rate,
                              'retail_gross_override',p_retail_gross_override));
end $$;
revoke all on function public.architect_upsert_product_details(bigint,text,text,text,text,text,jsonb,text,numeric,numeric,text)
  from public, anon, authenticated;
grant execute on function public.architect_upsert_product_details(bigint,text,text,text,text,text,jsonb,text,numeric,numeric,text)
  to service_role;
commit;

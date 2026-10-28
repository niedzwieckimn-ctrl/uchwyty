-- STAGING ONLY: current invoice workflow dependencies. No invoice is created.
-- Existing allocations initialize an idempotency ledger WITHOUT changing stock.
-- Private invoice-pdfs Storage is created by the normal application Storage API
-- (ensure_supabase_storage_bucket(public=False)), not by writing storage internals.
begin;
set local lock_timeout='5s';
set local statement_timeout='60s';
do $guard$
declare configured_url text:=current_setting('app.settings.supabase_url',true);
begin
  if configured_url is not null and configured_url<>''
     and rtrim(configured_url,'/')<>'https://irhrzdsjnwjyqfvhiclk.supabase.co' then
    raise exception '011 refused: project is not staging';
  end if;
  if to_regclass('public.staging_product_prices') is null
     or not exists(select 1 from auth.users where id='0062dcbf-717e-40a7-a820-3f79aefaa33f'::uuid
                    and email='dystrybutor.staging@example.com')
     or not exists(select 1 from auth.users where id='f887962b-23d7-4379-aebf-79a171f03089'::uuid
                    and email='cinek89_17@o2.pl') then
    raise exception '011 refused: staging fixture identity not confirmed';
  end if;
end $guard$;

alter table public.invoice_number_claims add column if not exists permanent boolean not null default false;

-- Initialize only when this table is first created, like invoice_stock.initialize.
-- Re-running a migration must not recreate deleted/cleared historical quantities.
do $initialize$
begin
  if to_regclass('public.invoice_stock_applied') is null then
    create table public.invoice_stock_applied (
      invoice_id bigint not null references public.invoices(id) on delete cascade,
      order_item_id bigint not null references public.order_items(id),
      product_id bigint not null references public.products(id),
      qty integer not null check(qty>=0),
      primary key(invoice_id,order_item_id)
    );
    insert into public.invoice_stock_applied(invoice_id,order_item_id,product_id,qty)
    select a.invoice_id,a.order_item_id,oi.product_id,sum(a.qty)::integer
    from public.invoice_allocations a join public.order_items oi on oi.id=a.order_item_id
    group by a.invoice_id,a.order_item_id,oi.product_id;
  end if;
end $initialize$;
create index if not exists staging_invoice_stock_item on public.invoice_stock_applied(order_item_id);
alter table public.invoice_stock_applied enable row level security;
revoke all on public.invoice_stock_applied from public,anon,authenticated,service_role;
grant select on public.invoice_stock_applied to service_role;

create or replace function public.apply_invoice_stock(p_invoice bigint,p_items jsonb)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $stock$
declare
  entry jsonb; wanted jsonb:='{}'::jsonb; result_stock jsonb:='{}'::jsonb;
  item_id bigint; item_qty integer; invoice_customer bigint;
  item_ids bigint[]; product_ids bigint[]; v_product_id bigint;
  row_item record; other_qty bigint;
  delta bigint; current_stock bigint; desired_qty bigint;
begin
  if p_invoice is null or p_invoice<=0 or p_items is null
     or jsonb_typeof(p_items)<>'array' or jsonb_array_length(p_items)>10000 then
    raise exception 'Invalid invoice stock request';
  end if;
  -- One invoice may be retried concurrently by several workers. Serialize before
  -- reading its earlier effect. No stock changes precede all preflight checks.
  perform pg_advisory_xact_lock(hashtextextended('invoice-stock:'||p_invoice::text,0));
  perform 1 from public.invoices where id=p_invoice for update;
  if not found then raise exception 'Invoice does not exist'; end if;
  select o.customer_id into invoice_customer from public.invoices i
    join public.orders o on o.id=i.order_id where i.id=p_invoice;
  if invoice_customer is null then raise exception 'Invoice customer cannot be confirmed'; end if;

  for entry in select value from jsonb_array_elements(p_items) loop
    if jsonb_typeof(entry)<>'object'
       or coalesce(entry->>'order_item_id','') !~ '^[1-9][0-9]{0,17}$'
       or coalesce(entry->>'qty','') !~ '^[1-9][0-9]{0,8}$' then
      raise exception 'Invalid invoice item or quantity';
    end if;
    item_id:=(entry->>'order_item_id')::bigint;
    item_qty:=(entry->>'qty')::integer;
    if wanted ? item_id::text then raise exception 'Duplicate invoice item'; end if;
    wanted:=wanted||jsonb_build_object(item_id::text,item_qty);
  end loop;
  select coalesce(array_agg(id order by id),array[]::bigint[]) into item_ids
  from (select key::bigint as id from jsonb_each(wanted)
        union select order_item_id from public.invoice_stock_applied where invoice_id=p_invoice) s;

  -- Different invoices sharing an order line serialize on that line too. This
  -- prevents their combined reservations from exceeding its ordered quantity.
  perform oi.id from public.order_items oi where oi.id=any(item_ids) order by oi.id for update;
  if exists(select 1 from unnest(item_ids) as wanted_id(value) where not exists(select 1 from public.order_items oi where oi.id=wanted_id.value)) then
    raise exception 'Invoice references a missing order item';
  end if;
  for row_item in select oi.id,oi.qty,oi.product_id,o.customer_id
                  from public.order_items oi join public.orders o on o.id=oi.order_id
                  where oi.id=any(item_ids) order by oi.id loop
    desired_qty:=coalesce((wanted->>row_item.id::text)::bigint,0);
    if row_item.customer_id is distinct from invoice_customer then
      raise exception 'Invoice item belongs to another customer';
    end if;
    select coalesce(sum(qty),0) into other_qty from public.invoice_stock_applied
      where order_item_id=row_item.id and invoice_id<>p_invoice;
    if desired_qty+other_qty>row_item.qty then
      raise exception 'Invoiced quantity exceeds order item quantity';
    end if;
    if exists(select 1 from public.invoice_stock_applied a
              where a.invoice_id=p_invoice and a.order_item_id=row_item.id
                and a.product_id<>row_item.product_id) then
      raise exception 'Invoice stock product changed';
    end if;
  end loop;

  select coalesce(array_agg(distinct oi.product_id order by oi.product_id),array[]::bigint[])
    into product_ids from public.order_items oi where oi.id=any(item_ids);
  perform s.product_id from public.stock s where s.product_id=any(product_ids) order by s.product_id for update;
  -- Validate every product first. A missing stock record is an error, never zero.
  foreach v_product_id in array product_ids loop
    select s.qty into current_stock from public.stock s where s.product_id=v_product_id;
    if not found then raise exception 'Product stock is unavailable'; end if;
    select coalesce(sum(coalesce((wanted->>oi.id::text)::bigint,0)-coalesce(a.qty,0)),0)
      into delta from public.order_items oi
      left join public.invoice_stock_applied a on a.invoice_id=p_invoice and a.order_item_id=oi.id
      where oi.id=any(item_ids) and oi.product_id=v_product_id;
    if delta>current_stock then raise exception 'Insufficient product stock'; end if;
    if current_stock-delta>2147483647 or current_stock-delta<0 then
      raise exception 'Stock quantity outside supported range';
    end if;
  end loop;
  foreach v_product_id in array product_ids loop
    select coalesce(sum(coalesce((wanted->>oi.id::text)::bigint,0)-coalesce(a.qty,0)),0)
      into delta from public.order_items oi
      left join public.invoice_stock_applied a on a.invoice_id=p_invoice and a.order_item_id=oi.id
      where oi.id=any(item_ids) and oi.product_id=v_product_id;
    update public.stock s set qty=s.qty-delta where s.product_id=v_product_id returning qty into current_stock;
    result_stock:=result_stock||jsonb_build_object(v_product_id::text,current_stock);
  end loop;
  insert into public.invoice_stock_applied(invoice_id,order_item_id,product_id,qty)
  select p_invoice,oi.id,oi.product_id,coalesce((wanted->>oi.id::text)::integer,0)
  from public.order_items oi where oi.id=any(item_ids)
  on conflict(invoice_id,order_item_id) do update set qty=excluded.qty,product_id=excluded.product_id;
  return jsonb_build_object('stock',result_stock);
end $stock$;
revoke all on function public.apply_invoice_stock(bigint,jsonb) from public,anon,authenticated;
grant execute on function public.apply_invoice_stock(bigint,jsonb) to service_role;

do $verify$
begin
  if to_regprocedure('public.apply_invoice_stock(bigint,jsonb)') is null
     or not has_function_privilege('service_role','public.apply_invoice_stock(bigint,jsonb)','EXECUTE')
     or has_function_privilege('anon','public.apply_invoice_stock(bigint,jsonb)','EXECUTE')
     or has_function_privilege('authenticated','public.apply_invoice_stock(bigint,jsonb)','EXECUTE') then
    raise exception '011 stock RPC permissions failed verification';
  end if;
end $verify$;
notify pgrst,'reload schema';
commit;

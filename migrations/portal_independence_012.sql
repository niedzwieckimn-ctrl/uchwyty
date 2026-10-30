-- STAGING FIRST. Existing business tables and service-only transaction routines
-- remain authoritative. No credentials, background HTTP, or Render dependency.
begin;
create schema if not exists architect_portal;
revoke all on schema architect_portal from public, anon, authenticated;
grant usage on schema architect_portal to authenticated, service_role;

create table if not exists architect_portal.rate_windows (
  actor uuid not null, action text not null, window_start timestamptz not null,
  attempts integer not null, primary key(actor,action)
);
revoke all on architect_portal.rate_windows from public, anon, authenticated;
alter table architect_portal.rate_windows enable row level security;
create table if not exists architect_portal.order_requests (
  request_key uuid primary key, actor uuid not null, customer_id bigint not null,
  request_body jsonb not null, order_id bigint not null unique references public.orders(id),
  created_at timestamptz not null default now()
);
revoke all on architect_portal.order_requests from public, anon, authenticated;
alter table architect_portal.order_requests enable row level security;
create table if not exists public.search_analytics_records (
  id text primary key, kind text not null, created_at timestamptz not null, payload jsonb not null
);
alter table public.search_analytics_records enable row level security;
revoke all on public.search_analytics_records from public, anon, authenticated;
grant select,insert,update on public.search_analytics_records to service_role;

-- Explicit opt-in for all existing distributors; keep all historical relations.
with disabled as (
  update public.distributor_program_memberships set active=false where active returning customer_id
)
insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
select 'migration:012','program_opt_in_required','distributor_program_membership',customer_id::text,
  '{"reason":"Explicit administrator activation required; history preserved"}'::jsonb from disabled;

create or replace function architect_portal.take_rate(p_actor uuid,p_action text,p_limit integer,p_seconds integer)
returns boolean language plpgsql security definer set search_path='' as $$
declare n integer;
begin
  insert into architect_portal.rate_windows as r(actor,action,window_start,attempts)
  values(p_actor,p_action,clock_timestamp(),1)
  on conflict(actor,action) do update set
    attempts=case when r.window_start < clock_timestamp()-make_interval(secs=>p_seconds) then 1 else r.attempts+1 end,
    window_start=case when r.window_start < clock_timestamp()-make_interval(secs=>p_seconds) then clock_timestamp() else r.window_start end
  returning attempts into n;
  return n<=p_limit;
end $$;

-- Shared catalog: transaction prices come from the same view as IKUGO.
-- Reservation = ordered minus invoiced units in open orders; backorders remain
-- allowed just as in the existing B2B path. Stock is issued only by invoicing.
create or replace function architect_portal.catalog(p_price_list text,p_architect boolean)
returns jsonb language sql stable security definer set search_path='' as $$
with reserved as (
  select oi.product_id,sum(greatest(0,oi.qty-coalesce(a.qty,0))) qty
  from public.order_items oi join public.orders o on o.id=oi.order_id
  left join (select order_item_id,sum(qty) qty from public.invoice_allocations group by order_item_id) a on a.order_item_id=oi.id
  where coalesce(o.warehouse_issued,0)=0 and lower(coalesce(o.status,'new')) in
    ('new','pending','unconfirmed','confirmed','packed','packed_partial','in_delivery','shipped','partially_shipped') group by oi.product_id
), incoming as (
  select ci.product_id,sum(ci.qty) qty from public.china_items ci
  join public.china_packages cp on cp.id=ci.package_id
  where cp.status in ('ordered','shipped','problem') group by ci.product_id
), data as (
  select c.*, p.ean, d.description,d.material,d.mounting_spacing,d.dimensions,d.finish,
    d.technical_data,d.model_3d_url,coalesce(d.vat_rate,0.23) vat_rate,
    coalesce(d.retail_gross_override,round(c.net_price::numeric*1.45*(1+coalesce(d.vat_rate,0.23)),2)) retail_pln,
    case when p_price_list='eu_eur' then e.price_eur::numeric else c.net_price::numeric end net,
    case when p_price_list='eu_eur' then e.price_eur::numeric else c.gross_price::numeric end gross,
    e.uvp_eur,coalesce(r.qty,0) reserved,coalesce(i.qty,0) incoming,
    img.id image_id,img.stored_path
  from public.client_stock_catalog_v c join public.products p on p.id=c.product_id and not coalesce(p.archived,false)
  left join public.architect_product_details d on d.product_id=p.id
  left join public.pricing_eur e on lower(e.sku)=lower(p.sku)
  left join reserved r on r.product_id=p.id left join incoming i on i.product_id=p.id
  left join lateral (select pi.id,pi.stored_path from public.product_image_assignments a join public.product_images pi on pi.id=a.image_id
    where a.product_id=p.id order by pi.id limit 1) img on true
)
select coalesce(jsonb_agg(
  jsonb_build_object('product_id',product_id,'sku',sku,'model',model,'name',name,
    'description',coalesce(description,''),'material',coalesce(material,'Lity mosiądz'),
    'mounting_spacing',coalesce(mounting_spacing,''),'dimensions',coalesce(dimensions,''),
    'finish',coalesce(finish,''),'technical_data',coalesce(technical_data,'{}'::jsonb),
    'model_3d_url',coalesce(model_3d_url,''),'retail_gross_pln',retail_pln,'vat_rate',vat_rate,
    'image_id',image_id,'image_ref',coalesce(stored_path,''),'image_url','')
  || case when p_architect then '{}'::jsonb else jsonb_build_object(
    'ean',coalesce(ean,''),'net_price',net,'gross_price',gross,
    'retail_price',case when p_price_list='eu_eur' then uvp_eur else retail_pln end,
    'currency',case when p_price_list='eu_eur' then 'EUR' else 'PLN' end,'price_list',p_price_list,
    'price_available',coalesce(net,0)>0,'qty_physical',greatest(0,qty_physical),
    'qty_reserved',reserved,'qty_on_stock',greatest(0,qty_physical-reserved),
    'qty_in_delivery',greatest(0,incoming-greatest(0,reserved-qty_physical)),
    'qty_available_in_delivery',greatest(0,incoming-greatest(0,reserved-qty_physical))) end
  order by sku),'[]'::jsonb) from data where not p_architect or coalesce(net,0)>0;
$$;

create or replace function architect_portal.quote(p_customer bigint,p_code text,p_items jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare c public.architect_codes%rowtype; rules public.architect_program_settings%rowtype;
  item jsonb; cat jsonb; rowdata jsonb; lines jsonb='[]'; pid bigint; qty integer;
  net numeric; vat numeric; retail numeric; after_retail numeric; discount numeric; after_net numeric;
  total numeric=0; discount_total numeric=0;
begin
  if jsonb_typeof(p_items) is distinct from 'array' or jsonb_array_length(p_items) not between 1 and 100 then raise exception 'Invalid order lines'; end if;
  if not exists(select 1 from public.customers where id=p_customer and coalesce(language,'pl')='pl') then raise exception 'Only PLN distributors can use architect codes'; end if;
  perform 1 from public.distributor_program_memberships where customer_id=p_customer and active for share;
  if not found then raise exception 'Program architektów jest wyłączony dla tego dystrybutora'; end if;
  select * into c from public.architect_codes where code=upper(btrim(p_code));
  if not found or c.status<>'active' or c.expires_at<=now() or c.distributor_id<>p_customer
    or not exists(select 1 from public.architect_profiles where id=c.architect_id and status='approved') then raise exception 'Kod nie jest dostępny dla tego sklepu'; end if;
  select * into rules from public.architect_program_settings where version=c.program_version and active;
  if not found then raise exception 'Program jest niedostępny'; end if;
  cat:=architect_portal.catalog('pln',false);
  for item in select value from jsonb_array_elements(p_items) loop
    if jsonb_typeof(item)<>'object' or coalesce(item->>'product_id','')!~'^[1-9][0-9]*$' or coalesce(item->>'qty','')!~'^[1-9][0-9]*$' then raise exception 'Invalid order line'; end if;
    pid:=(item->>'product_id')::bigint; qty:=(item->>'qty')::integer;
    if qty>10000 or exists(select 1 from jsonb_array_elements(lines) l where (l->>'product_id')::bigint=pid) then raise exception 'Duplicate or excessive order line'; end if;
    select value into rowdata from jsonb_array_elements(cat) where (value->>'product_id')::bigint=pid;
    net:=(rowdata->>'net_price')::numeric; vat:=(rowdata->>'vat_rate')::numeric; retail:=(rowdata->>'retail_gross_pln')::numeric;
    if coalesce(net,0)<=0 then raise exception 'Product price unavailable'; end if;
    if vat<>0.23 then raise exception 'Nonstandard VAT requires invoice workflow review'; end if;
    after_retail:=retail-round(retail*rules.customer_discount_rate,2);
    discount:=round(round((retail-after_retail)/(1+vat),2)*rules.manufacturer_share,2);
    after_net:=round(net-discount,2);
    if after_net<0 then raise exception 'Discount requires manual review'; end if;
    lines:=lines||jsonb_build_array(jsonb_build_object('product_id',pid,'sku',rowdata->>'sku','qty',qty,
      'unit_net_price',after_net,'unit_gross_price',round(after_net*(1+vat),2),'retail_gross_after',after_retail,
      'manufacturer_discount_net',discount*qty,'line_net',after_net*qty,'line_gross',round(after_net*qty*(1+vat),2)));
    total:=total+after_net*qty; discount_total:=discount_total+discount*qty;
  end loop;
  return jsonb_build_object('ok',true,'code',c.code,'program_version',rules.version,'lines',lines,
    'total_net',total,'total_gross',round(total*1.23,2),'manufacturer_discount_net',discount_total);
end $$;

create or replace function architect_portal.create_order(p_actor uuid,p_customer bigint,p_data jsonb)
returns jsonb language plpgsql security definer set search_path='' as $$
declare key uuid; canonical jsonb; existing architect_portal.order_requests%rowtype;
  customer public.customers%rowtype; code text; items jsonb; line jsonb; cat jsonb; price jsonb;
  oid bigint; pid bigint; qty integer; plist text; curr text; note text; stamp text;
begin
  key:=(p_data->>'idempotency_key')::uuid;
  if key is null then raise exception 'Idempotency-Key required'; end if;
  code:=upper(btrim(coalesce(p_data->>'architect_code',''))); items:=p_data->'items'; note:=btrim(coalesce(p_data->>'note',''));
  if length(note)>2000 or jsonb_typeof(items) is distinct from 'array' or jsonb_array_length(items) not between 1 and 100 then raise exception 'Invalid order lines'; end if;
  if exists(select 1 from jsonb_array_elements(items) i where jsonb_typeof(i)<>'object' or coalesce(i->>'product_id','')!~'^[1-9][0-9]*$' or coalesce(i->>'qty','')!~'^[1-9][0-9]*$' or (i->>'qty')::numeric>10000)
    or (select count(distinct i->>'product_id') from jsonb_array_elements(items) i)<>jsonb_array_length(items) then raise exception 'Duplicate or invalid order lines'; end if;
  select jsonb_agg(jsonb_build_object('product_id',(i->>'product_id')::bigint,'qty',(i->>'qty')::integer) order by (i->>'product_id')::bigint) into items from jsonb_array_elements(items) i;
  canonical:=jsonb_build_object('items',items,'code',code,'note',note);
  perform pg_advisory_xact_lock(hashtextextended(key::text,12));
  select * into existing from architect_portal.order_requests where request_key=key;
  if found then
    if existing.actor<>p_actor or existing.customer_id<>p_customer or existing.request_body<>canonical then raise exception 'Idempotency key belongs to a different request'; end if;
    select order_no into stamp from public.orders where id=existing.order_id;
    return jsonb_build_object('ok',true,'duplicate',true,'order',jsonb_build_object('id',existing.order_id,'order_no',stamp),'email',jsonb_build_object('ok',false,'pending_retry',true,'queued',true));
  end if;
  if exists(select 1 from public.orders where idempotency_key=key::text) then raise exception 'Idempotency key belongs to an existing request'; end if;
  select * into strict customer from public.customers where id=p_customer;
  plist:=case when coalesce(customer.language,'pl')='pl' then 'pln' else 'eu_eur' end;
  curr:=case when plist='pln' then 'PLN' else 'EUR' end;
  if code<>'' then
    perform architect_portal.quote(p_customer,code,items);
    oid:=public.architect_create_discounted_order(p_actor,p_customer,code,key,items,note);
  else
    cat:=architect_portal.catalog(plist,false); stamp:=to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS');
    insert into public.orders(order_no,customer_id,customer_name,customer_address,customer_phone,customer_email,status,note,created_at,qr_data_url,idempotency_key,currency,price_list)
    values('TEMP-'||key::text,p_customer,customer.name,customer.address,customer.phone,customer.email,'new',note,stamp,'',key::text,curr,plist) returning id into oid;
    update public.orders set order_no='ZAM-'||to_char(now() at time zone 'Europe/Warsaw','YYMMDD')||oid::text where id=oid;
    for line in select value from jsonb_array_elements(items) loop
      pid:=(line->>'product_id')::bigint; qty:=(line->>'qty')::integer;
      select value into price from jsonb_array_elements(cat) where (value->>'product_id')::bigint=pid;
      if coalesce((price->>'net_price')::numeric,0)<=0 then raise exception 'Product price unavailable'; end if;
      insert into public.order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,unit_retail_price,currency,created_at)
      values(oid,pid,price->>'sku',qty,(price->>'net_price')::numeric,(price->>'gross_price')::numeric,(price->>'retail_price')::numeric,curr,stamp);
    end loop;
  end if;
  insert into architect_portal.order_requests values(key,p_actor,p_customer,canonical,oid,now());
  insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
  values(p_actor::text,'portal_order_created','order',oid::text,jsonb_build_object('customer_id',p_customer,'idempotency_key',key));
  select order_no into stamp from public.orders where id=oid;
  return jsonb_build_object('ok',true,'order',jsonb_build_object('id',oid,'order_no',stamp),'email',jsonb_build_object('ok',false,'pending_retry',true,'queued',true));
end $$;

-- Queue ordinary orders as well, using the existing immutable outbox trigger.
create constraint trigger portal_queue_order_email after insert on architect_portal.order_requests
deferrable initially deferred for each row execute function public.architect_queue_order_email();

revoke all on all functions in schema architect_portal from public, anon, authenticated;
commit;

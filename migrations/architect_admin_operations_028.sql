-- Run manually after 001..027. No existing account or package is changed.
begin;
alter table public.architect_profiles add column if not exists removed_at timestamptz;
alter table public.architect_profiles add column if not exists removed_by text;
alter table public.architect_profiles add column if not exists removal_reason text;
alter table public.architect_sample_items add column if not exists cancelled_at timestamptz;
alter table public.architect_sample_orders add column if not exists fulfillment_order_id bigint unique references public.orders(id) on delete restrict;

-- A closed profile is a permanent record, never a way to reset entitlements.
create or replace function public.architect_closed_profile_guard()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if tg_op='DELETE' then
   if old.removed_at is not null then raise exception 'Historia zamkniętego konta musi pozostać zachowana'; end if;
   return old;
 end if;
 if tg_op='UPDATE' and old.removed_at is not null then
   if new.status<>'suspended' or new.removed_at is distinct from old.removed_at
      or new.email is distinct from old.email or new.sample_entitlements<>old.sample_entitlements then
     raise exception 'Konto zostało trwale usunięte z programu';
   end if;
 end if;
 if tg_op='INSERT' or (tg_op='UPDATE' and new.email is distinct from old.email) then
   perform pg_advisory_xact_lock(hashtextextended(lower(btrim(new.email)),28));
   if exists(select 1 from public.architect_profiles p where lower(btrim(p.email))=lower(btrim(new.email))
             and p.removed_at is not null and p.id<>new.id) then
     raise exception 'Ten adres e-mail jest powiązany z zamkniętym kontem programu';
   end if;
 end if;
 return new;
end $$;
drop trigger if exists architect_closed_profile_guard on public.architect_profiles;
create trigger architect_closed_profile_guard before insert or update or delete on public.architect_profiles
for each row execute function public.architect_closed_profile_guard();
revoke all on function public.architect_closed_profile_guard() from public,anon,authenticated;

create or replace function public.architect_remove_from_program(p_architect_id uuid,p_reason text,p_actor text)
returns void language plpgsql security definer set search_path=public,pg_temp as $$
declare p public.architect_profiles%rowtype;
begin
 if length(btrim(coalesce(p_reason,'')))<10 or length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Podaj powód usunięcia konta'; end if;
 select * into strict p from public.architect_profiles where id=p_architect_id for update;
 if p.removed_at is not null then return; end if;
 perform pg_advisory_xact_lock(hashtextextended(lower(btrim(p.email)),28));
 update public.architect_profiles set status='suspended',removed_at=now(),removed_by=p_actor,
 removal_reason=btrim(p_reason),reviewed_at=now(),reviewed_by=p_actor where id=p_architect_id;
 update public.architect_codes set status='cancelled' where architect_id=p_architect_id and status='active';
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'profile_removed','architect_profile',p_architect_id::text,jsonb_build_object('reason',btrim(p_reason)));
end $$;

create or replace function public.architect_cancel_sample_item(p_sample_id uuid,p_product_id bigint,p_actor text)
returns void language plpgsql security definer set search_path=public,pg_temp as $$
declare s public.architect_sample_orders%rowtype;
begin
 if length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Brak operatora'; end if;
 select * into strict s from public.architect_sample_orders where id=p_sample_id for update;
 if s.status not in ('requested','preparing') or s.fulfillment_order_id is not null then
   raise exception 'Pakiet przekazano do wysyłki albo zakończono. Nie zmieniono jego zawartości'; end if;
 if not exists(select 1 from public.architect_sample_items where sample_order_id=p_sample_id and product_id=p_product_id) then raise exception 'Nie znaleziono próbki'; end if;
 update public.architect_sample_items set cancelled_at=now() where sample_order_id=p_sample_id and product_id=p_product_id and cancelled_at is null;
 if not found then return; end if;
 if not exists(select 1 from public.architect_sample_items where sample_order_id=p_sample_id and cancelled_at is null) then
   update public.architect_sample_orders set status='cancelled' where id=p_sample_id;
 end if;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'sample_item_cancelled','architect_sample_order',p_sample_id::text,jsonb_build_object('product_id',p_product_id));
end $$;

-- One atomic stock issue and one internal, zero-value warehouse order per pack.
-- The existing packing/carrier/label/pickup screens operate on this order.
create or replace function public.architect_prepare_sample_shipping(p_sample_id uuid,p_actor text)
returns bigint language plpgsql security definer set search_path=public,pg_temp as $$
declare s public.architect_sample_orders%rowtype; a public.architect_profiles%rowtype;
 i record; oid bigint; oldqty integer;
begin
 if length(btrim(coalesce(p_actor,'')))<2 then raise exception 'Brak operatora'; end if;
 select * into strict s from public.architect_sample_orders where id=p_sample_id for update;
 if s.fulfillment_order_id is not null then return s.fulfillment_order_id; end if;
 if s.status not in ('requested','preparing') then raise exception 'Ten pakiet nie oczekuje na przygotowanie'; end if;
 select * into strict a from public.architect_profiles where id=s.architect_id for update;
 if a.status<>'approved' or a.removed_at is not null then raise exception 'Konto architekta nie jest aktywne'; end if;
 if not exists(select 1 from public.architect_sample_items where sample_order_id=p_sample_id and cancelled_at is null) then raise exception 'Pakiet nie zawiera próbek do wysyłki'; end if;
 insert into public.orders(order_no,customer_name,customer_address,customer_phone,customer_email,status,note,created_at,qr_data_url,idempotency_key,currency,price_list,warehouse_issued)
 values('TEMP-SAMPLE-'||p_sample_id,a.first_name||' '||a.last_name,s.shipping_address,a.phone,a.email,'confirmed',
 'Pakiet architekta: próbki i katalog drukowany. Bez sprzedaży i faktury.',
 to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'),'','architect-sample:'||p_sample_id,'PLN','pln',1) returning id into oid;
 update public.orders set order_no='PROBKI-'||oid where id=oid;
 for i in select si.product_id,si.qty,p.sku from public.architect_sample_items si join public.products p on p.id=si.product_id
          where si.sample_order_id=p_sample_id and si.cancelled_at is null order by si.product_id loop
   update public.stock set qty=qty-i.qty where product_id=i.product_id and qty>=i.qty returning qty+i.qty into oldqty;
   if not found then raise exception 'Brak stanu magazynowego dla próbki %',i.sku; end if;
   insert into public.architect_sample_stock_issues(sample_order_id,product_id,qty,old_stock_qty,new_stock_qty)
   values(p_sample_id,i.product_id,i.qty,oldqty,oldqty-i.qty);
   insert into public.order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,unit_retail_price,currency,created_at)
   values(oid,i.product_id,i.sku,i.qty,0,0,0,'PLN',to_char(now() at time zone 'Europe/Warsaw','YYYY-MM-DD HH24:MI:SS'));
 end loop;
 update public.architect_sample_orders set fulfillment_order_id=oid,status='preparing' where id=p_sample_id;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(p_actor,'sample_shipping_prepared','architect_sample_order',p_sample_id::text,jsonb_build_object('order_id',oid,'catalog_included',true));
 return oid;
end $$;

-- Standard warehouse shipping writes the parcel metadata. No second stock issue.
create or replace function public.architect_sample_shipping_sync()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if new.status in ('shipped','completed') and length(btrim(coalesce(new.tracking_no,'')))>0 then
   update public.architect_sample_orders set status=case when status='delivered' then status else 'shipped' end,
     tracking_no=new.tracking_no,carrier=new.carrier,shipped_at=coalesce(shipped_at,now())
   where fulfillment_order_id=new.id and status in ('preparing','shipped','delivered');
 end if;
 return new;
end $$;
drop trigger if exists architect_sample_shipping_sync on public.orders;
create trigger architect_sample_shipping_sync after update on public.orders
for each row when (new.idempotency_key like 'architect-sample:%') execute function public.architect_sample_shipping_sync();
revoke all on function public.architect_sample_shipping_sync() from public,anon,authenticated;

-- A free pack must not be invoiced through a direct API or a combined invoice.
create or replace function public.architect_sample_no_invoice()
returns trigger language plpgsql security definer set search_path=public,pg_temp as $$
begin
 if exists(select 1 from public.architect_sample_orders where fulfillment_order_id=new.order_id) then
   raise exception 'Pakiet próbek jest bezpłatny i nie podlega fakturowaniu';
 end if;
 return new;
end $$;
drop trigger if exists architect_sample_no_invoice on public.invoices;
create trigger architect_sample_no_invoice before insert or update on public.invoices
for each row execute function public.architect_sample_no_invoice();
drop trigger if exists architect_sample_no_invoice_allocation on public.invoice_allocations;
create trigger architect_sample_no_invoice_allocation before insert or update on public.invoice_allocations
for each row execute function public.architect_sample_no_invoice();
revoke all on function public.architect_sample_no_invoice() from public,anon,authenticated;

-- Keep original status function for unlinked historical packs. Linked packs
-- cannot be cancelled/issued independently from their warehouse shipment.
do $$ begin
 if to_regprocedure('public.architect_change_sample_status_legacy_028(uuid,text,text,text)') is null then
   alter function public.architect_change_sample_status(uuid,text,text,text) rename to architect_change_sample_status_legacy_028;
 end if;
end $$;
revoke all on function public.architect_change_sample_status_legacy_028(uuid,text,text,text) from public,anon,authenticated,service_role;
create or replace function public.architect_change_sample_status(p_sample_id uuid,p_status text,p_tracking_no text,p_actor text)
returns void language plpgsql security definer set search_path=public,pg_temp as $$
declare s public.architect_sample_orders%rowtype;
begin
 select * into strict s from public.architect_sample_orders where id=p_sample_id for update;
 if s.fulfillment_order_id is not null and not (p_status='delivered' and s.status='shipped') then
   raise exception 'Ten pakiet jest obsługiwany w module przesyłek magazynu'; end if;
 if p_status='shipped' and exists(select 1 from public.architect_sample_items where sample_order_id=p_sample_id and cancelled_at is not null) then
   raise exception 'Przygotuj tę paczkę przez magazyn, aby pominąć anulowane próbki'; end if;
 perform public.architect_change_sample_status_legacy_028(p_sample_id,p_status,p_tracking_no,p_actor);
end $$;

revoke all on function public.architect_remove_from_program(uuid,text,text),public.architect_cancel_sample_item(uuid,bigint,text),public.architect_prepare_sample_shipping(uuid,text),public.architect_change_sample_status(uuid,text,text,text) from public,anon,authenticated;
grant execute on function public.architect_remove_from_program(uuid,text,text),public.architect_cancel_sample_item(uuid,bigint,text),public.architect_prepare_sample_shipping(uuid,text),public.architect_change_sample_status(uuid,text,text,text) to service_role;
notify pgrst,'reload schema';
commit;

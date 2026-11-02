-- Anulowanie zachowuje zamówienie, ceny i każde poprzednie użycie kodu.
-- Nie wykonuje żadnego anulowania ani wysyłki podczas instalacji.
begin;
alter table public.architect_code_redemptions
 add column if not exists cancelled_at timestamptz,
 add column if not exists cancelled_by text,
 add column if not exists cancellation_reason text;

-- Replace only the single-column uniqueness constraint on code_id. The order
-- and request idempotency constraints must remain unique across ALL history.
do $$ declare c record; begin
 for c in select conname from pg_constraint
 where conrelid='public.architect_code_redemptions'::regclass and contype='u'
 and conkey=array[(select attnum from pg_attribute where
 attrelid='public.architect_code_redemptions'::regclass and attname='code_id')]::smallint[]
 loop execute format('alter table public.architect_code_redemptions drop constraint %I',c.conname); end loop;
end $$;
create unique index if not exists architect_one_live_redemption_per_code
 on public.architect_code_redemptions(code_id) where cancelled_at is null;

create or replace function public.architect_cancel_order(p_order_id bigint,p_reason text,p_actor text)
returns jsonb language plpgsql security definer set search_path=pg_catalog,public as $$
declare r public.architect_code_redemptions%rowtype; c public.architect_codes%rowtype;
 o public.orders%rowtype; result_status text;
begin
 if length(btrim(coalesce(p_reason,''))) not between 5 and 1000
 or length(btrim(coalesce(p_actor,''))) not between 2 and 200 then
   raise exception 'Podaj powód anulowania (5–1000 znaków).'; end if;
 select * into r from public.architect_code_redemptions where order_id=p_order_id;
 if not found then raise exception 'Brak zamówienia z kodem architekta.'; end if;
 -- Same code lock as redemption: a new use cannot race with cancellation.
 select * into c from public.architect_codes where id=r.code_id for update;
 perform pg_advisory_xact_lock(p_order_id);
 select * into r from public.architect_code_redemptions where order_id=p_order_id for update;
 select * into o from public.orders where id=p_order_id for update;
 if not found then raise exception 'Nie znaleziono zamówienia.'; end if;
 if r.cancelled_at is not null then
   return jsonb_build_object('ok',true,'duplicate',true,'order_id',p_order_id,
    'status','cancelled','cancelled_at',r.cancelled_at,'code',c.code,'code_status',c.status);
 end if;
 if coalesce(o.warehouse_issued,0)<>0 then
   raise exception 'Najpierw wycofaj wydanie magazynowe. Kod nie został zwolniony.'; end if;
 if exists(select 1 from public.fulfillment_shipping_claims where order_id=p_order_id)
 or exists(select 1 from public.fulfillment_reconciliation where order_id=p_order_id and
  (jsonb_array_length(coalesce(payload->'fulfillment_shipping_attempts','[]'::jsonb))>0
   or jsonb_array_length(coalesce(payload->'fulfillment_document_intents','[]'::jsonb))>0)) then
   raise exception 'Trwa lub wymaga wyjaśnienia operacja dokumentu albo przesyłki. Najpierw sprawdź jej wynik.'; end if;
 if lower(coalesce(o.status,'')) in ('shipped','partially_shipped','completed','issued','in_delivery')
 or nullif(btrim(coalesce(o.tracking_no,'')),'') is not null
 or nullif(btrim(coalesce(o.inpost_shipment_id::text,'')),'') is not null then
   raise exception 'Najpierw rozlicz lub wycofaj przesyłkę. Kod nie został zwolniony.'; end if;
 if exists(select 1 from public.invoices where order_id=p_order_id)
 or exists(select 1 from public.invoice_allocations where order_id=p_order_id
   or order_item_id in(select id from public.order_items where order_id=p_order_id)) then
   raise exception 'Zamówienie ma fakturę. Najpierw wycofaj ją właściwą operacją. Kod nie został zwolniony.'; end if;
 if exists(select 1 from public.invoice_stock_applied s join public.order_items i
 on i.id=s.order_item_id where i.order_id=p_order_id and s.qty>0) then
   raise exception 'Pozostało wydanie magazynowe faktury. Najpierw rozlicz magazyn.'; end if;
 if exists(select 1 from public.architect_commission_ledger l join public.architect_sale_lines s
 on s.id=l.sale_line_id where s.redemption_id=r.id) then
   raise exception 'Zamówienie ma rozliczenia sprzedaży lub prowizji. Najpierw rozlicz zwrot.'; end if;
 if c.status<>'redeemed' then raise exception 'Stan kodu wymaga sprawdzenia. Nic nie zmieniono.'; end if;
 update public.architect_code_redemptions set cancelled_at=now(),cancelled_by=btrim(p_actor),
 cancellation_reason=btrim(p_reason) where id=r.id returning * into r;
 update public.orders set status='cancelled' where id=p_order_id;
 -- Cancellation does not silently extend the original validity period.
 result_status:=case when c.expires_at<=now() then 'expired'
 when not exists(select 1 from public.architect_profiles where id=c.architect_id and status='approved')
 or not exists(select 1 from public.distributor_program_memberships where customer_id=c.distributor_id and active)
 then 'cancelled' else 'active' end;
 update public.architect_codes set status=result_status where id=c.id;
 insert into public.architect_audit_events(actor_id,action,entity_type,entity_id,details)
 values(btrim(p_actor),'order_cancelled_code_released','order',p_order_id::text,
 jsonb_build_object('redemption_id',r.id,'code_id',c.id,'reason',btrim(p_reason),
 'previous_status',o.status,'code_status',result_status,'expires_at',c.expires_at));
 return jsonb_build_object('ok',true,'duplicate',false,'order_id',p_order_id,
 'status','cancelled','cancelled_at',r.cancelled_at,'code',c.code,'code_status',result_status);
end $$;
revoke all on function public.architect_cancel_order(bigint,text,text) from public,anon,authenticated;
grant execute on function public.architect_cancel_order(bigint,text,text) to service_role;

-- All fulfillment paths (including older servers) must respect a cancellation.
-- Locking the order also serializes cancellation against new invoice/sale rows.
create or replace function public.architect_require_live_order(p_order_id bigint)
returns void language plpgsql security definer set search_path=pg_catalog,public as $$
begin
 perform 1 from public.orders where id=p_order_id for update;
 if exists(select 1 from public.architect_code_redemptions
 where order_id=p_order_id and cancelled_at is not null) then
   raise exception 'Zamówienie anulowane. Utwórz nowe zamówienie, aby ponownie użyć kodu.';
 end if;
end $$;
revoke all on function public.architect_require_live_order(bigint) from public,anon,authenticated,service_role;

create or replace function public.architect_cancelled_write_guard()
returns trigger language plpgsql security definer set search_path=pg_catalog,public as $$
declare oid bigint; previous_oid bigint;
begin
 if TG_TABLE_NAME='orders' then
   if TG_OP='UPDATE' and new.status='cancelled' and exists(
    select 1 from public.architect_code_redemptions where order_id=old.id and cancelled_at is null) then
     raise exception 'Użyj anulowania zamówienia ze zwolnieniem kodu.';
   end if;
   if exists(select 1 from public.architect_code_redemptions
    where order_id=old.id and cancelled_at is not null) then
     if TG_OP='DELETE' then raise exception 'Anulowane zamówienie pozostaje w historii.'; end if;
     if new.status is distinct from 'cancelled' or coalesce(new.warehouse_issued,0)<>0
      or nullif(btrim(coalesce(new.tracking_no,'')),'') is not null
      or nullif(btrim(coalesce(new.inpost_shipment_id::text,'')),'') is not null then
       raise exception 'Nie można wznowić anulowanego zamówienia.'; end if;
     if (to_jsonb(new)-'status') is distinct from (to_jsonb(old)-'status') then
       raise exception 'Dane anulowanego zamówienia pozostają w historii bez zmian.'; end if;
   end if;
 else
   if TG_TABLE_NAME='architect_commission_ledger' then
     select r.order_id into oid from public.architect_sale_lines s
      join public.architect_code_redemptions r on r.id=s.redemption_id where s.id=new.sale_line_id;
     if TG_OP='UPDATE' then
       select r.order_id into previous_oid from public.architect_sale_lines s
        join public.architect_code_redemptions r on r.id=s.redemption_id where s.id=old.sale_line_id;
     end if;
   elsif TG_TABLE_NAME in ('invoice_stock_applied','invoice_allocations') then
     select order_id into oid from public.order_items where id=new.order_item_id;
     if TG_OP='UPDATE' then
       select order_id into previous_oid from public.order_items where id=old.order_item_id;
     end if;
     if TG_TABLE_NAME='invoice_allocations' then
       perform public.architect_require_live_order(new.order_id);
       if TG_OP='UPDATE' then perform public.architect_require_live_order(old.order_id); end if;
     end if;
   else
     oid:=case when TG_OP='DELETE' then old.order_id else new.order_id end;
     if TG_OP='UPDATE' then previous_oid:=old.order_id; end if;
   end if;
   perform public.architect_require_live_order(oid);
   if previous_oid is distinct from oid then perform public.architect_require_live_order(previous_oid); end if;
 end if;
 if TG_OP='DELETE' then return old; else return new; end if;
end $$;
revoke all on function public.architect_cancelled_write_guard() from public,anon,authenticated,service_role;
drop trigger if exists architect_cancel_guard on public.orders;
create trigger architect_cancel_guard before update or delete on public.orders
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.order_items;
create trigger architect_cancel_guard before insert or update or delete on public.order_items
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.invoices;
create trigger architect_cancel_guard before insert or update on public.invoices
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.invoice_allocations;
create trigger architect_cancel_guard before insert or update on public.invoice_allocations
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.invoice_stock_applied;
create trigger architect_cancel_guard before insert or update on public.invoice_stock_applied
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.architect_commission_ledger;
create trigger architect_cancel_guard before insert or update on public.architect_commission_ledger
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.fulfillment_shipping_claims;
create trigger architect_cancel_guard before insert or update on public.fulfillment_shipping_claims
 for each row execute function public.architect_cancelled_write_guard();
drop trigger if exists architect_cancel_guard on public.fulfillment_reconciliation;
create trigger architect_cancel_guard before insert or update on public.fulfillment_reconciliation
 for each row execute function public.architect_cancelled_write_guard();
notify pgrst,'reload schema';
commit;

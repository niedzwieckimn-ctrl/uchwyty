-- Production preflight: confirm all client reads use client_accounts and no
-- external integrations depend on anonymous stock or China delivery tables.
-- Apply after the matching IKUGO backend is live and a backup is verified.
begin;

-- Existing production policies tmp_read_anon_* currently expose stock and
-- incoming package quantities to anyone holding the public anon key.
revoke select on public.stock, public.china_items, public.china_packages from anon;
drop policy if exists tmp_read_anon_stock on public.stock;
drop policy if exists tmp_read_anon_china_items on public.china_items;
drop policy if exists tmp_read_anon_china_packages on public.china_packages;

-- B2B order creation is performed by the trusted backend. Existing browser
-- code reads orders but never writes orders/order_items directly.
revoke insert, update, delete on public.orders, public.order_items from anon, authenticated;
revoke select on public.orders, public.order_items from anon;
grant select on public.orders, public.order_items to authenticated;

-- Email equality alone is not a distributor authorization. Preserve B2B
-- read access through the existing active user-to-customer mapping.
drop policy if exists orders_select_own on public.orders;
drop policy if exists client_read_own_orders on public.orders;
create policy orders_select_active_distributor on public.orders
  for select to authenticated using (
    exists (select 1 from public.client_accounts ca
            where ca.user_id = (select auth.uid())
              and ca.customer_id = orders.customer_id and ca.is_active)
  );

drop policy if exists order_items_select_own on public.order_items;
drop policy if exists client_read_own_order_items on public.order_items;
create policy order_items_select_active_distributor on public.order_items
  for select to authenticated using (
    exists (select 1 from public.orders o
            join public.client_accounts ca on ca.customer_id = o.customer_id
            where o.id = order_items.order_id and ca.user_id = (select auth.uid())
              and ca.is_active)
  );

drop policy if exists "Klient widzi realizacje swoich pozycji" on public.invoice_allocations;
create policy invoice_allocations_select_active_distributor on public.invoice_allocations
  for select to authenticated using (
    exists (select 1 from public.orders o
            join public.client_accounts ca on ca.customer_id = o.customer_id
            where o.id = invoice_allocations.order_id
              and ca.user_id = (select auth.uid()) and ca.is_active)
  );
commit;

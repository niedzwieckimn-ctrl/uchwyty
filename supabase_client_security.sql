-- Uruchom w Supabase: SQL Editor -> New query -> Run.
-- Skrypt pozwala zalogowanemu klientowi odczytać wyłącznie jego zamówienia
-- i ich pozycje. Zapisy zamówień wykonuje wyłącznie backend przez service_role.

alter table public.orders enable row level security;
alter table public.order_items enable row level security;

revoke insert, update, delete on table public.orders from anon, authenticated;
revoke insert, update, delete on table public.order_items from anon, authenticated;
grant select on table public.orders to authenticated;
grant select on table public.order_items to authenticated;

drop policy if exists client_read_own_orders on public.orders;
create policy client_read_own_orders
on public.orders for select
to authenticated
using (
  lower(coalesce(customer_email, '')) = lower(coalesce(auth.jwt() ->> 'email', ''))
);

drop policy if exists client_read_own_order_items on public.order_items;
create policy client_read_own_order_items
on public.order_items for select
to authenticated
using (
  exists (
    select 1
    from public.orders o
    where o.id = order_items.order_id
      and lower(coalesce(o.customer_email, '')) = lower(coalesce(auth.jwt() ->> 'email', ''))
  )
);


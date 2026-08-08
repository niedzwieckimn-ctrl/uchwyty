-- Uruchom jednorazowo w Supabase SQL Editor przed wdrożeniem nowego backendu.
alter table public.orders
  add column if not exists idempotency_key text;

create unique index if not exists orders_idempotency_key_unique
  on public.orders (idempotency_key)
  where idempotency_key is not null;


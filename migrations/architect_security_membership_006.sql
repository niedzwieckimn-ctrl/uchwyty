-- Apply after architect_security_002 on existing installations. The order
-- SELECT policies query client_accounts as the authenticated user. Grant only
-- own-row reads so a valid distributor can pass that membership check.
begin;
alter table public.client_accounts enable row level security;
revoke select on public.client_accounts from anon;
grant select on public.client_accounts to authenticated;
drop policy if exists client_accounts_select_own on public.client_accounts;
create policy client_accounts_select_own on public.client_accounts
  for select to authenticated using (user_id = (select auth.uid()));
commit;

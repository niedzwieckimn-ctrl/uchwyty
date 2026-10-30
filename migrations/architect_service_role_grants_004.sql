-- The new program tables had no service_role table grants in the isolated
-- staging project. The trusted IKUGO backend uses it through PostgREST.
-- Grant only architect-program objects; existing IKUGO tables keep their
-- independently managed privileges. No anon/authenticated grants change.
begin;

grant usage on schema public to service_role;
grant select, insert, update on table
  public.architect_program_settings,
  public.architect_profiles,
  public.distributor_program_memberships,
  public.architect_product_details,
  public.architect_sample_orders,
  public.architect_sample_items,
  public.architect_sample_stock_issues,
  public.architect_codes,
  public.architect_code_redemptions,
  public.architect_sale_lines,
  public.architect_commission_ledger,
  public.architect_monthly_settlements,
  public.architect_payouts,
  public.architect_audit_events
to service_role;

do $$
declare sequence_name text;
begin
  foreach sequence_name in array array[
    pg_get_serial_sequence('public.architect_sample_items', 'id'),
    pg_get_serial_sequence('public.architect_sale_lines', 'id'),
    pg_get_serial_sequence('public.architect_audit_events', 'id')
  ] loop
    if sequence_name is not null then
      execute format('grant usage, select on sequence %s to service_role', sequence_name);
    end if;
  end loop;
end $$;

commit;

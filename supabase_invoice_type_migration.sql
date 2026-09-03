-- Nullable on purpose: historical invoices remain untouched and are resolved
-- by the application from their persisted currency/item VAT data.
alter table public.invoices
  add column if not exists invoice_type text;

alter table public.invoices
  drop constraint if exists invoices_invoice_type_check;

alter table public.invoices
  add constraint invoices_invoice_type_check
  check (invoice_type is null or invoice_type in ('domestic', 'wdt', 'export'));


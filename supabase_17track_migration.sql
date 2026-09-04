-- Bezpieczne rozszerzenie P/O. Nie zmienia wartości istniejących rekordów.
alter table public.china_packages add column if not exists supplier text;
alter table public.china_packages add column if not exists ordered_at text;
alter table public.china_packages add column if not exists shipped_at text;
alter table public.china_packages add column if not exists arrived_at text;
alter table public.china_packages add column if not exists warehouse_received integer;
alter table public.china_packages add column if not exists warehouse_received_at text;
alter table public.china_packages add column if not exists tracking_carrier text;
alter table public.china_packages add column if not exists tracking_carrier_code bigint;
alter table public.china_packages add column if not exists tracking_status text;
alter table public.china_packages add column if not exists tracking_substatus text;
alter table public.china_packages add column if not exists tracking_last_event text;
alter table public.china_packages add column if not exists tracking_last_update text;
alter table public.china_packages add column if not exists tracking_synced_at text;
alter table public.china_packages add column if not exists tracking_error text;
alter table public.china_packages add column if not exists tracking_events_json text;
alter table public.china_packages add column if not exists tracking_eta text;
alter table public.china_packages add column if not exists tracking_registered_at text;
alter table public.china_packages add column if not exists manual_status_at text;

create index if not exists idx_china_packages_tracking on public.china_packages(tracking);
create index if not exists idx_china_packages_tracking_status on public.china_packages(tracking_status);

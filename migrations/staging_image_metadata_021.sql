-- STAGING ONLY. Repair incomplete synthetic image tables created before 009.
-- Production already has these fields; never use this fixture repair there.
begin;
do $$begin
 if exists(select from public.products where sku not like 'STAGING-%') then
  raise exception 'Refusing fixture repair: non-staging products found';
 end if;
 if not exists(select from public.products where id=503 and sku='STAGING-MOS-003') then
  raise exception 'Expected staging fixture absent';
 end if;
end$$;
alter table public.product_images add column if not exists filename text;
alter table public.product_images add column if not exists created_at text;
alter table public.product_image_assignments add column if not exists created_at text;
-- Timestamp records metadata repair time, not an inferred upload date.
update public.product_images set filename=regexp_replace(stored_path,'^.*/','') where filename is null;
update public.product_images set created_at=current_timestamp::text where created_at is null;
update public.product_image_assignments set created_at=current_timestamp::text where created_at is null;
alter table public.product_images alter column filename set not null;
alter table public.product_images alter column created_at set not null;
alter table public.product_image_assignments alter column created_at set not null;
notify pgrst,'reload schema';
commit;

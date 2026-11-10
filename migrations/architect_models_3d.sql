begin;
insert into storage.buckets(id,name,public,file_size_limit)
values ('architect-models-3d','architect-models-3d',false,52428800)
on conflict(id) do update set public=false,file_size_limit=excluded.file_size_limit;

create or replace function architect_portal.may_download_models()
returns boolean language sql stable security definer set search_path='' as $$
    select exists(select 1 from public.architect_profiles p join auth.users u on u.id=p.id
        where p.id=auth.uid() and p.status='approved' and u.email_confirmed_at is not null)
$$;
revoke all on function architect_portal.may_download_models() from public,anon;
grant execute on function architect_portal.may_download_models() to authenticated;
drop policy if exists architect_models_read on storage.objects;
create policy architect_models_read on storage.objects for select to authenticated
using (bucket_id='architect-models-3d' and (select architect_portal.may_download_models()));
commit;

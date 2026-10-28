-- READ ONLY. Save results before the owner-approved production cutover.
begin read only;
set local statement_timeout = '15s';
select current_database(), current_user, version(), pg_database_size(current_database()) as database_bytes;
with required(table_name,column_name) as (values
 ('client_accounts','user_id'),('client_accounts','customer_id'),('client_accounts','is_active'),
 ('orders','idempotency_key'),('order_items','unit_net_price'),('order_items','unit_gross_price'),
 ('invoice_allocations','order_item_id'),('invoices','publication_state'),('stock','qty'))
select r.*,c.data_type,c.is_nullable,c.column_default,c.column_name is not null as present
from required r left join information_schema.columns c
on c.table_schema='public' and c.table_name=r.table_name and c.column_name=r.column_name order by 1,2;
select n.nspname,c.relname,c.relrowsecurity,c.relforcerowsecurity
from pg_class c join pg_namespace n on n.oid=c.relnamespace
where n.nspname in ('public','architect_portal','storage') and c.relkind='r' order by 1,2;
select schemaname,tablename,policyname,roles,cmd,qual,with_check
from pg_policies where schemaname in ('public','architect_portal','storage') order by 1,2,3;
select table_schema,table_name,grantee,privilege_type from information_schema.role_table_grants
where table_schema in ('public','architect_portal','storage') and grantee in ('anon','authenticated','service_role') order by 1,2,3,4;
select n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) as args,
 p.prosecdef,p.proconfig,p.proacl,md5(pg_get_functiondef(p.oid)) as definition_md5
from pg_proc p join pg_namespace n on n.oid=p.pronamespace
where n.nspname in ('public','architect_portal') and (p.proname like 'architect_%' or p.proname like 'portal_%' or n.nspname='architect_portal') order by 1,2,3;
select table_name,column_name,data_type,is_nullable,column_default from information_schema.columns
where table_schema='public' and table_name in ('pricing_eur','china_packages','product_images','product_image_assignments','orders') order by 1,ordinal_position;
-- Optional data-dependent checks must use counts/aggregates, never copy customer rows into a public PR.
rollback;

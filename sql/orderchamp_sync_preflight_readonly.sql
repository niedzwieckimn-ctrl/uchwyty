-- Read-only preparation. No stock, schema, configuration or scheduler writes.
BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT table_name,column_name,data_type,is_nullable,column_default,is_identity
FROM information_schema.columns
WHERE table_schema='public' AND table_name IN
 ('products','stock','orders','order_items','invoice_allocations','customers')
ORDER BY table_name,ordinal_position;
SELECT tablename,indexname,indexdef FROM pg_indexes
WHERE schemaname='public' AND tablename IN
 ('products','stock','orders','order_items','invoice_allocations','customers');
SELECT c.relname AS table_name,k.conname,pg_get_constraintdef(k.oid) AS definition
FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relname IN ('products','stock','orders','order_items','invoice_allocations','customers');
SELECT c.relname AS table_name,pg_get_triggerdef(t.oid) AS definition
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND NOT t.tgisinternal
  AND c.relname IN ('orders','order_items','customers');
-- Validate existing identity sequences without incrementing or resetting them.
WITH targets AS (
  SELECT 'orders' AS table_name,COALESCE(max(id),0) AS max_id FROM public.orders
  UNION ALL SELECT 'order_items',COALESCE(max(id),0) FROM public.order_items
  UNION ALL SELECT 'customers',COALESCE(max(id),0) FROM public.customers
) SELECT t.table_name,t.max_id,pg_get_serial_sequence('public.'||t.table_name,'id') AS identity_sequence,s.last_value
FROM targets t LEFT JOIN pg_sequences s
ON format('%I.%I',s.schemaname,s.sequencename)=pg_get_serial_sequence('public.'||t.table_name,'id');
SELECT extname,extversion FROM pg_extension WHERE extname IN ('pg_cron','pg_net','supabase_vault');
COMMIT;

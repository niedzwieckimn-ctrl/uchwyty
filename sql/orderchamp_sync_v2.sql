-- Orderchamp v2, prepared for review; DO NOT run as an application startup migration.
-- No existing inventory values are changed by installing this migration.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS public.orderchamp_sync_job (
  id integer PRIMARY KEY CHECK (id=1),
  enabled boolean NOT NULL DEFAULT false,
  requested boolean NOT NULL DEFAULT false,
  lease_token uuid, lease_until timestamptz,
  next_at timestamptz NOT NULL DEFAULT now(),
  data jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO public.orderchamp_sync_job(id) VALUES(1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS public.orderchamp_order_links (
  external_id text PRIMARY KEY,
  order_id bigint UNIQUE NOT NULL REFERENCES public.orders(id),
  external_updated_at timestamptz NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orderchamp_order_links_order ON public.orderchamp_order_links(order_id);
CREATE TABLE IF NOT EXISTS public.orderchamp_sync_events (
  id bigserial PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now(),
  action text NOT NULL,details jsonb NOT NULL
);
ALTER TABLE public.orderchamp_sync_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orderchamp_order_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orderchamp_sync_events ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.orderchamp_sync_job, public.orderchamp_order_links, public.orderchamp_sync_events FROM PUBLIC, anon, authenticated;
GRANT ALL ON public.orderchamp_sync_job, public.orderchamp_order_links, public.orderchamp_sync_events TO service_role;

-- Exactly the same stock and outstanding-allocation rule as stock_availability.py.
-- Missing data is an error in the caller, never a successful empty/zero snapshot.
CREATE OR REPLACE FUNCTION public.orderchamp_availability_v2()
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public
AS $function$
  WITH selected AS (
    SELECT id,sku FROM public.products WHERE NOT COALESCE(archived,false)
  ), eligible AS (
    SELECT i.id,i.product_id,i.qty
    FROM public.order_items i JOIN public.orders o ON o.id=i.order_id
    JOIN selected p ON p.id=i.product_id
    WHERE COALESCE(o.warehouse_issued,0)=0 AND lower(COALESCE(o.status,'')) IN
      ('new','pending','unconfirmed','confirmed','packed','packed_partial','in_delivery','shipped','partially_shipped')
  ), allocated AS (
    SELECT a.order_item_id,SUM(a.qty) AS qty FROM public.invoice_allocations a
    JOIN eligible i ON i.id=a.order_item_id GROUP BY a.order_item_id
  ), reserved AS (
    SELECT i.product_id,SUM(GREATEST(0,i.qty-COALESCE(a.qty,0))) AS qty
    FROM eligible i LEFT JOIN allocated a ON a.order_item_id=i.id
    GROUP BY i.product_id
  ), oc AS (
    SELECT line->>'sku' AS sku,
      SUM((line->>'qty')::bigint) AS ordered,
      SUM((line->>'unshipped_qty')::bigint) AS unshipped
    FROM public.orderchamp_order_links l CROSS JOIN LATERAL jsonb_array_elements(l.payload->'items') line
    WHERE NOT (l.payload->>'cancelled')::boolean
    GROUP BY line->>'sku'
  ), conflicts AS (
    SELECT l.order_id FROM public.orderchamp_order_links l JOIN public.orders o ON o.id=l.order_id
    WHERE (NOT (l.payload->>'cancelled')::boolean AND lower(COALESCE(o.status,'')) IN
      ('cancelled','canceled','deleted','usuniete','anulowane'))
  ) SELECT jsonb_build_object('read_at',now(),'conflicts',(SELECT COALESCE(jsonb_agg(order_id),'[]') FROM conflicts),
    'rows',COALESCE(jsonb_agg(jsonb_build_object(
      'id',p.id,'sku',p.sku,
      'available_qty',GREATEST(0,COALESCE(s.qty,0)-GREATEST(0,COALESCE(r.qty,0))),
      'shortage_qty',GREATEST(0,GREATEST(0,COALESCE(r.qty,0))-COALESCE(s.qty,0)),
      'oc_ordered',COALESCE(oc.ordered,0),'oc_unshipped',COALESCE(oc.unshipped,0)
    ) ORDER BY p.sku),'[]'::jsonb))
  FROM selected p LEFT JOIN public.stock s ON s.product_id=p.id
  LEFT JOIN reserved r ON r.product_id=p.id LEFT JOIN oc ON oc.sku=p.sku;
$function$;

-- One durable job and lease across all workers/deployments. Never hold this lock over HTTP.
CREATE OR REPLACE FUNCTION public.orderchamp_job_v2(p_action text,p_token uuid DEFAULT NULL,p_data jsonb DEFAULT '{}')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public
AS $function$
DECLARE j public.orderchamp_sync_job%ROWTYPE;
BEGIN
  SELECT * INTO STRICT j FROM public.orderchamp_sync_job WHERE id=1 FOR UPDATE;
  IF p_action='status' THEN
    RETURN jsonb_build_object('enabled',j.enabled,'requested',j.requested,'next_at',j.next_at,
      'updated_at',j.updated_at,'running',COALESCE(j.lease_until>now(),false),
      'report',j.data->'report','error',j.data->>'error','phase',j.data->>'phase',
      'review_required',COALESCE((j.data->>'review_required')::boolean,false));
  ELSIF p_action='pause' THEN
    UPDATE public.orderchamp_sync_job SET enabled=false,requested=false,updated_at=now() WHERE id=1;
    RETURN jsonb_build_object('ok',true);
  ELSIF p_action='reconcile' THEN
    IF j.lease_until>now() THEN RAISE EXCEPTION 'JOB_RUNNING'; END IF;
    IF NOT COALESCE((j.data->>'review_required')::boolean,false) OR NOT j.data ? 'intent' THEN
      RAISE EXCEPTION 'NO_UNCERTAIN_WRITE'; END IF;
    IF (j.data->'intent'->>'at')::timestamptz>now()-interval '5 minutes' THEN
      RAISE EXCEPTION 'WAIT_BEFORE_RECONCILIATION'; END IF;
    INSERT INTO public.orderchamp_sync_events(action,details) VALUES('reconcile_requested',j.data->'intent');
    -- Forget only uncertain anchors. The worker imports current orders and reads
    -- current inventory before calculating a NEW delta. Never replay the old intent.
    UPDATE public.orderchamp_sync_job SET requested=true,lease_token=NULL,lease_until=NULL,next_at=now(),
      data=((data-'intent'-'plan')||jsonb_build_object('phase','idle','review_required',false,'error',NULL,
        'catalog_checked_at',NULL,'deferred','{}'::jsonb,
        'anchors',COALESCE(data->'anchors','{}')-ARRAY(SELECT r->>'sku' FROM jsonb_array_elements(j.data->'intent'->'rows') r))),
      updated_at=now() WHERE id=1;
    RETURN jsonb_build_object('ok',true);
  ELSIF p_action IN ('queue','enable','wake','compare') THEN
    IF COALESCE((j.data->>'review_required')::boolean,false) THEN
      RAISE EXCEPTION 'ORDERCHAMP_REVIEW_REQUIRED';
    END IF;
    IF p_action IN ('queue','enable','compare') AND j.lease_until>now() THEN RAISE EXCEPTION 'JOB_RUNNING'; END IF;
    IF p_action='queue' AND p_data->>'only_sku' IS NOT NULL AND j.enabled THEN RAISE EXCEPTION 'PAUSE_BEFORE_SKU_TEST'; END IF;
    UPDATE public.orderchamp_sync_job SET
      enabled=CASE WHEN p_action='enable' THEN true ELSE enabled END,
      requested=CASE WHEN p_action='wake' THEN requested OR enabled ELSE true END,
      next_at=CASE WHEN p_action='wake' AND data->>'error' IS NOT NULL THEN next_at ELSE now() END,
      data=CASE WHEN p_action='compare' THEN data-'catalog_checked_at'
                WHEN p_action='queue' THEN data||jsonb_build_object('only_sku',p_data->'only_sku')
                WHEN p_action='enable' THEN (data-'catalog_checked_at')||jsonb_build_object('only_sku',NULL)
                ELSE data END,updated_at=now() WHERE id=1;
    RETURN jsonb_build_object('ok',true);
  ELSIF p_action='claim' THEN
    IF p_token IS NULL THEN RAISE EXCEPTION 'TOKEN_REQUIRED'; END IF;
    IF j.lease_until>now() OR j.next_at>now() OR NOT (j.enabled OR j.requested)
      OR COALESCE((j.data->>'review_required')::boolean,false) THEN RETURN NULL; END IF;
    IF j.data ? 'intent' THEN
      UPDATE public.orderchamp_sync_job SET data=data||'{"review_required":true,"error":"MUTATION_OUTCOME_UNKNOWN"}'::jsonb,
        lease_token=NULL,lease_until=NULL,updated_at=now() WHERE id=1;
      RETURN NULL;
    END IF;
    UPDATE public.orderchamp_sync_job SET lease_token=p_token,lease_until=now()+interval '120 seconds',
      updated_at=now() WHERE id=1;
    RETURN j.data;
  ELSIF p_action IN ('save','release','fail') THEN
    IF j.lease_token IS DISTINCT FROM p_token OR j.lease_until<=now() THEN RAISE EXCEPTION 'LEASE_LOST'; END IF;
    IF p_action='save' AND NOT (j.enabled OR j.requested) THEN RAISE EXCEPTION 'JOB_PAUSED'; END IF;
    IF p_action='save' AND p_data ? 'intent' AND NOT j.data ? 'intent' THEN
      INSERT INTO public.orderchamp_sync_events(action,details) VALUES('adjust_intent',p_data->'intent');
    ELSIF p_action='save' AND j.data ? 'intent' AND NOT p_data ? 'intent' THEN
      INSERT INTO public.orderchamp_sync_events(action,details) VALUES('adjust_confirmed',j.data->'intent');
    ELSIF p_action='fail' THEN
      INSERT INTO public.orderchamp_sync_events(action,details) VALUES('job_error',jsonb_build_object('code',p_data->>'error','intent',p_data->'intent'));
    END IF;
    UPDATE public.orderchamp_sync_job SET data=p_data,
      requested=CASE WHEN p_action='release' THEN false ELSE requested END,
      next_at=CASE WHEN p_action='fail' THEN now()+interval '5 minutes'
                   WHEN p_action='release' THEN now()+interval '60 seconds' ELSE next_at END,
      lease_token=CASE WHEN p_action='save' THEN p_token ELSE NULL END,
      lease_until=CASE WHEN p_action='save' THEN now()+interval '120 seconds' ELSE NULL END,
      updated_at=now() WHERE id=1;
    RETURN jsonb_build_object('ok',true);
  END IF;
  RAISE EXCEPTION 'INVALID_ACTION';
END;
$function$;

-- Atomic import, guarded by the job lease. The mapping is the idempotency boundary.
-- No stock is issued and no invoice/email/payment/KSeF action is executed here.
CREATE OR REPLACE FUNCTION public.orderchamp_import_v2(p_token uuid,p_orders jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public
AS $function$
DECLARE item jsonb; line jsonb; old public.orderchamp_order_links%ROWTYPE;
  oid bigint; cid bigint; pid bigint; stamp timestamptz; created timestamptz;
  affected bigint[]:='{}'; changed boolean; has_allocations boolean; applied integer:=0;
BEGIN
  PERFORM 1 FROM public.orderchamp_sync_job WHERE id=1 AND lease_token=p_token
    AND lease_until>now() AND (enabled OR requested) FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION 'LEASE_LOST'; END IF;
  IF jsonb_typeof(p_orders)<>'array' OR jsonb_array_length(p_orders)>10 THEN RAISE EXCEPTION 'INVALID_BATCH'; END IF;
  FOR item IN SELECT value FROM jsonb_array_elements(p_orders) LOOP
    IF COALESCE(item->>'id','')='' OR COALESCE(item->>'company','')=''
      OR item->>'currency' NOT IN ('PLN','EUR') OR jsonb_typeof(item->'items')<>'array'
      OR jsonb_array_length(item->'items')=0 THEN RAISE EXCEPTION 'INVALID_ORDER'; END IF;
    stamp:=(item->>'updated_at')::timestamptz;
    created:=item->>'created_at';
    SELECT * INTO old FROM public.orderchamp_order_links WHERE external_id=item->>'id' FOR UPDATE;
    IF FOUND AND old.external_updated_at>=stamp THEN
      affected:=array_append(affected,old.order_id);
      CONTINUE; -- Return cached rows too: repairs a lost local mirror acknowledgement.
    END IF;
    oid:=old.order_id;
    -- Reject every unknown/ambiguous SKU before modifying an order or customer.
    FOR line IN SELECT value FROM jsonb_array_elements(item->'items') LOOP
      IF (line->>'qty')::bigint<=0 OR (line->>'unshipped_qty')::bigint<0
        OR (line->>'unshipped_qty')::bigint>(line->>'qty')::bigint THEN RAISE EXCEPTION 'INVALID_QUANTITY'; END IF;
      SELECT CASE WHEN count(*)=1 THEN min(id) ELSE NULL END INTO pid
        FROM public.products WHERE sku=line->>'sku' AND NOT COALESCE(archived,false);
      IF pid IS NULL THEN RAISE EXCEPTION 'ORDER_SKU_NOT_FOUND'; END IF;
    END LOOP;
    IF oid IS NULL THEN
      -- A cancelled order never seen locally has no reservation to release.
      IF (item->>'cancelled')::boolean THEN CONTINUE; END IF;
      INSERT INTO public.customers(name,address,phone,email,nip,price_list,created_at)
        VALUES(item->>'company',item->>'billing_address',item->>'phone',item->>'email',item->>'vat_number',
          CASE WHEN item->>'currency'='EUR' THEN 'eu_eur' ELSE 'pln' END,created) RETURNING id INTO cid;
      INSERT INTO public.orders(order_no,customer_id,customer_name,customer_address,customer_phone,customer_email,
        status,note,created_at,warehouse_issued,currency,price_list)
        VALUES('OC-'||md5(item->>'id'),cid,item->>'company',item->>'billing_address',item->>'phone',item->>'email',
          'new','Orderchamp — '||(item->>'number')||E'\nDostawa: '||(item->>'shipping_address'),created,0,
          item->>'currency',CASE WHEN item->>'currency'='EUR' THEN 'eu_eur' ELSE 'pln' END) RETURNING id INTO oid;
      changed:=true;
    ELSE
      -- Quantities/prices/cancellation are immutable after local invoicing or shipping begins.
      -- unshipped_qty may change remotely without changing local order lines.
      changed:=(SELECT jsonb_agg(x-'unshipped_qty' ORDER BY x->>'line_id') FROM jsonb_array_elements(item->'items') x)
        IS DISTINCT FROM (SELECT jsonb_agg(x-'unshipped_qty' ORDER BY x->>'line_id') FROM jsonb_array_elements(old.payload->'items') x)
        OR (item->>'currency') IS DISTINCT FROM (old.payload->>'currency');
      SELECT EXISTS(SELECT 1 FROM public.invoice_allocations a JOIN public.order_items i ON i.id=a.order_item_id
        WHERE i.order_id=oid) OR EXISTS(SELECT 1 FROM public.orders WHERE id=oid
        AND (COALESCE(warehouse_issued,0)<>0 OR COALESCE(tracking_no,'')<>'' OR
          lower(COALESCE(status,'')) NOT IN ('new','pending','unconfirmed','confirmed','cancelled'))) INTO has_allocations;
      IF has_allocations AND (changed OR (item->>'cancelled')::boolean IS DISTINCT FROM (old.payload->>'cancelled')::boolean)
        THEN RAISE EXCEPTION 'ORDER_ALREADY_IN_FULFILLMENT'; END IF;
      IF (item->>'cancelled')::boolean THEN
        UPDATE public.orders SET status='cancelled' WHERE id=oid;
      ELSIF (old.payload->>'cancelled')::boolean THEN
        UPDATE public.orders SET status='new' WHERE id=oid;
      END IF;
      IF changed THEN
        UPDATE public.orders SET currency=item->>'currency',
          price_list=CASE WHEN item->>'currency'='EUR' THEN 'eu_eur' ELSE 'pln' END WHERE id=oid;
      END IF;
    END IF;
    IF changed THEN
      DELETE FROM public.order_items WHERE order_id=oid;
      FOR line IN SELECT value FROM jsonb_array_elements(item->'items') LOOP
        SELECT id INTO STRICT pid FROM public.products WHERE sku=line->>'sku' AND NOT COALESCE(archived,false);
        INSERT INTO public.order_items(order_id,product_id,sku,qty,unit_net_price,unit_gross_price,currency,created_at)
          VALUES(oid,pid,line->>'sku',(line->>'qty')::bigint,(line->>'unit_net')::numeric,
            (line->>'unit_gross')::numeric,item->>'currency',created);
      END LOOP;
    END IF;
    INSERT INTO public.orderchamp_order_links(external_id,order_id,external_updated_at,payload)
      VALUES(item->>'id',oid,stamp,item) ON CONFLICT(external_id) DO UPDATE
      SET external_updated_at=excluded.external_updated_at,payload=excluded.payload;
    affected:=array_append(affected,oid);
    applied:=applied+1;
    INSERT INTO public.orderchamp_sync_events(action,details) VALUES('order_import',
      jsonb_build_object('external_id',item->>'id','order_id',oid,'updated_at',stamp,'cancelled',item->'cancelled'));
  END LOOP;
  RETURN jsonb_build_object('order_ids',to_jsonb(affected),'changed_count',applied,
    'orders',COALESCE((SELECT jsonb_agg(to_jsonb(o)) FROM public.orders o WHERE id=ANY(affected)),'[]'),
    'order_items',COALESCE((SELECT jsonb_agg(to_jsonb(i)) FROM public.order_items i WHERE order_id=ANY(affected)),'[]'),
    'customers',COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM public.customers c WHERE id IN
      (SELECT customer_id FROM public.orders WHERE id=ANY(affected))),'[]'));
END;
$function$;

REVOKE ALL ON FUNCTION public.orderchamp_availability_v2() FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.orderchamp_job_v2(text,uuid,jsonb) FROM PUBLIC,anon,authenticated;
REVOKE ALL ON FUNCTION public.orderchamp_import_v2(uuid,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.orderchamp_availability_v2() TO service_role;
GRANT EXECUTE ON FUNCTION public.orderchamp_job_v2(text,uuid,jsonb) TO service_role;
GRANT EXECUTE ON FUNCTION public.orderchamp_import_v2(uuid,jsonb) TO service_role;
COMMIT;

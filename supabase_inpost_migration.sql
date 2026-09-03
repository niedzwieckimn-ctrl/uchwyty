ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS inpost_shipment_id text;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS inpost_label_format text;
ALTER TABLE public.orders ADD COLUMN IF NOT EXISTS inpost_dispatch_order_id text;

CREATE INDEX IF NOT EXISTS idx_orders_inpost_shipment_id
ON public.orders(inpost_shipment_id);

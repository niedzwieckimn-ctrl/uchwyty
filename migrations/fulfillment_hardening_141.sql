-- Apply manually as an administrator after fulfillment_shipping_claims.sql.
-- No customer policies or roles are changed.
CREATE TABLE IF NOT EXISTS public.invoice_number_counters(period text PRIMARY KEY,last_number bigint NOT NULL);
CREATE TABLE IF NOT EXISTS public.invoice_number_claims(invoice_no text PRIMARY KEY,created_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.invoice_number_claims(invoice_no) SELECT invoice_no FROM public.invoices WHERE invoice_no IS NOT NULL ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION public.retain_invoice_number() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
    INSERT INTO public.invoice_number_claims(invoice_no) VALUES(NEW.invoice_no) ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS retain_invoice_number ON public.invoices;
CREATE TRIGGER retain_invoice_number AFTER INSERT ON public.invoices FOR EACH ROW EXECUTE FUNCTION public.retain_invoice_number();
CREATE OR REPLACE FUNCTION public.reserve_invoice_number(p_period text,p_custom text DEFAULT '',p_requested_min bigint DEFAULT 0) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE n bigint; result text;
BEGIN
    IF p_period !~ '^(0[1-9]|1[0-2])/[0-9]{4}$' THEN RAISE EXCEPTION 'Invalid period'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('invoice-number:' || p_period,0));
    IF coalesce(p_custom,'') <> '' THEN
        result := trim(p_custom);
        PERFORM pg_advisory_xact_lock(hashtextextended('custom-invoice-number:' || lower(result),0));
        IF EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result)) THEN
            RAISE EXCEPTION 'Invoice number was already used';
        END IF;
    ELSE
        SELECT coalesce(max((regexp_match(invoice_no,'^FVAT ([0-9]+)/' || p_period || '$','i'))[1]::bigint),0)
            INTO n FROM public.invoice_number_claims;
        SELECT greatest(greatest(n,coalesce((SELECT last_number FROM public.invoice_number_counters WHERE period=p_period),0))+1,p_requested_min) INTO n;
        INSERT INTO public.invoice_number_counters VALUES(p_period,n) ON CONFLICT(period) DO UPDATE SET last_number=excluded.last_number;
        result := 'FVAT ' || n || '/' || p_period;
    END IF;
    INSERT INTO public.invoice_number_claims(invoice_no) VALUES(result);
    RETURN result;
END $$;

CREATE TABLE IF NOT EXISTS public.fulfillment_reconciliation(
    order_id bigint PRIMARY KEY,revision bigint NOT NULL,payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now());
CREATE OR REPLACE FUNCTION public.save_fulfillment_reconciliation(p_order_id bigint,p_expected_revision bigint,p_payload jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE current_revision bigint;
BEGIN
    IF p_order_id<=0 OR p_expected_revision<0 OR jsonb_typeof(p_payload)<>'object' THEN RAISE EXCEPTION 'Invalid metadata'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('fulfillment-reconciliation:' || p_order_id,0));
    SELECT revision INTO current_revision FROM public.fulfillment_reconciliation WHERE order_id=p_order_id;
    IF coalesce(current_revision,0)<>p_expected_revision THEN RETURN jsonb_build_object('saved',false,'revision',current_revision); END IF;
    INSERT INTO public.fulfillment_reconciliation VALUES(p_order_id,p_expected_revision+1,p_payload,now())
        ON CONFLICT(order_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,updated_at=now();
    RETURN jsonb_build_object('saved',true,'revision',p_expected_revision+1);
END $$;
ALTER TABLE public.invoice_number_counters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.invoice_number_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.fulfillment_reconciliation ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.invoice_number_counters,public.invoice_number_claims,public.fulfillment_reconciliation FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.invoice_number_counters,public.invoice_number_claims,public.fulfillment_reconciliation TO service_role;
REVOKE ALL ON FUNCTION public.reserve_invoice_number(text,text,bigint),public.save_fulfillment_reconciliation(bigint,bigint,jsonb),public.retain_invoice_number() FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_invoice_number(text,text,bigint),public.save_fulfillment_reconciliation(bigint,bigint,jsonb) TO service_role;

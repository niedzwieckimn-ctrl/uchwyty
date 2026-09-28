-- ZIP47 repair. Apply after fulfillment_hardening_141.sql and, when present,
-- invoice_numbering_cursor.sql. This migration does not rename any invoice.
-- Automatic sequence = max existing invoice in the period + 1. A live race
-- claim blocks that candidate; protected KSeF numbers remain reserved forever.
BEGIN;

ALTER TABLE public.invoice_number_claims
    ADD COLUMN IF NOT EXISTS permanent boolean NOT NULL DEFAULT false;
DROP TRIGGER IF EXISTS retain_invoice_number ON public.invoices;
DROP TRIGGER IF EXISTS maintain_invoice_number_cursor ON public.invoices;
DROP TRIGGER IF EXISTS retain_final_invoice_number ON public.ksef_documents;
DROP TRIGGER IF EXISTS consume_reserved_invoice_number ON public.invoices;

INSERT INTO public.invoice_number_claims(invoice_no,permanent)
SELECT i.invoice_no,true FROM public.invoices i
JOIN public.ksef_documents k ON k.invoice_id=i.id
WHERE coalesce(k.ksef_number,'')<>'' OR k.sent_at IS NOT NULL
   OR lower(coalesce(k.status,'')) IN ('sending','processing','unknown','sent','accepted')
ON CONFLICT(invoice_no) DO UPDATE SET permanent=true;

DELETE FROM public.invoice_number_claims c WHERE NOT c.permanent
AND (EXISTS(SELECT 1 FROM public.invoices i WHERE lower(trim(i.invoice_no))=lower(trim(c.invoice_no)))
     OR c.created_at < now()-interval '10 minutes');

CREATE OR REPLACE FUNCTION public.consume_reserved_invoice_number() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
    DELETE FROM public.invoice_number_claims
     WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no)) AND NOT permanent;
    RETURN NEW;
END $$;
CREATE TRIGGER consume_reserved_invoice_number AFTER INSERT OR UPDATE OF invoice_no ON public.invoices
FOR EACH ROW EXECUTE FUNCTION public.consume_reserved_invoice_number();

CREATE OR REPLACE FUNCTION public.retain_final_invoice_number() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
    IF coalesce(NEW.ksef_number,'')<>'' OR NEW.sent_at IS NOT NULL
       OR lower(coalesce(NEW.status,'')) IN ('sending','processing','unknown','sent','accepted') THEN
        INSERT INTO public.invoice_number_claims(invoice_no,permanent)
        SELECT invoice_no,true FROM public.invoices WHERE id=NEW.invoice_id
        ON CONFLICT(invoice_no) DO UPDATE SET permanent=true;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER retain_final_invoice_number AFTER INSERT OR UPDATE ON public.ksef_documents
FOR EACH ROW EXECUTE FUNCTION public.retain_final_invoice_number();

CREATE OR REPLACE FUNCTION public.reserve_invoice_number(
    p_period text,p_custom text DEFAULT '',p_requested_min bigint DEFAULT 0
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE n bigint; result text;
BEGIN
    IF p_period !~ '^(0[1-9]|1[0-2])/[0-9]{4}$' THEN RAISE EXCEPTION 'Invalid period'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('invoice-number:' || p_period,0));
    IF coalesce(trim(p_custom),'')<>'' THEN
        result := trim(p_custom);
        IF EXISTS(SELECT 1 FROM public.invoices WHERE lower(trim(invoice_no))=lower(result))
           OR EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result) AND permanent) THEN
            RAISE EXCEPTION 'Invoice number was already used';
        END IF;
    ELSE
        -- Form suggestions and the legacy cursor never advance the sequence.
        SELECT coalesce(max((regexp_match(trim(invoice_no),'^FVAT ([0-9]+)/' || p_period || '$','i'))[1]::bigint),0)+1
          INTO n FROM public.invoices;
        LOOP
            result := 'FVAT ' || n || '/' || p_period;
            EXIT WHEN NOT EXISTS(SELECT 1 FROM public.invoices WHERE lower(trim(invoice_no))=lower(result))
                AND NOT EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result) AND permanent);
            n := n+1;
        END LOOP;
    END IF;
    -- A manual number can name a different period than its issue date, and
    -- letter case must not create a second reservation of the same number.
    -- Automatic and manual paths therefore also share this normalized lock.
    PERFORM pg_advisory_xact_lock(hashtextextended('custom-invoice-number:' || lower(result),0));
    IF EXISTS(SELECT 1 FROM public.invoices WHERE lower(trim(invoice_no))=lower(result))
       OR EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result) AND permanent) THEN
        RAISE EXCEPTION 'Invoice number was already used';
    END IF;
    DELETE FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result)
        AND NOT permanent AND created_at < now()-interval '10 minutes';
    IF EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result)) THEN
        RAISE EXCEPTION 'Invoice number is currently reserved';
    END IF;
    INSERT INTO public.invoice_number_claims(invoice_no,created_at,permanent) VALUES(result,now(),false);
    RETURN result;
END $$;

ALTER TABLE public.invoice_number_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.invoice_number_claims FROM PUBLIC,anon,authenticated;
GRANT ALL ON public.invoice_number_claims TO service_role;
REVOKE ALL ON FUNCTION public.reserve_invoice_number(text,text,bigint),
    public.consume_reserved_invoice_number(),public.retain_final_invoice_number()
FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_invoice_number(text,text,bigint) TO service_role;
COMMIT;

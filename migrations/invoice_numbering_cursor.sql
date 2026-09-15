-- Apply once as service/database administrator before deploying this release.
-- Converts permanent draft-number history into a current cursor plus transient claims.
BEGIN;

DROP TRIGGER IF EXISTS retain_invoice_number ON public.invoices;
DROP TRIGGER IF EXISTS maintain_invoice_number_cursor ON public.invoices;
DROP TRIGGER IF EXISTS retain_final_invoice_number ON public.ksef_documents;

TRUNCATE public.invoice_number_claims;
TRUNCATE public.invoice_number_counters;

INSERT INTO public.invoice_number_counters(period,last_number)
SELECT matched[2],max((matched[1])::bigint)
FROM (
    SELECT regexp_match(trim(invoice_no),'^FVAT ([0-9]+)/((?:0[1-9]|1[0-2])/[0-9]{4})$','i') matched
    FROM public.invoices
) parsed
WHERE matched IS NOT NULL
GROUP BY matched[2];

CREATE OR REPLACE FUNCTION public.maintain_invoice_number_cursor() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE old_match text[]; new_match text[]; reserved boolean;
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        old_match := regexp_match(trim(OLD.invoice_no),'^FVAT ([0-9]+)/((?:0[1-9]|1[0-2])/[0-9]{4})$','i');
        IF old_match IS NOT NULL AND (TG_OP='DELETE' OR lower(trim(OLD.invoice_no))<>lower(trim(NEW.invoice_no))) THEN
            UPDATE public.invoice_number_counters
               SET last_number=(old_match[1])::bigint-1
             WHERE period=old_match[2] AND last_number=(old_match[1])::bigint;
        END IF;
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        SELECT EXISTS(SELECT 1 FROM public.invoice_number_claims
                       WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no))) INTO reserved;
        new_match := regexp_match(trim(NEW.invoice_no),'^FVAT ([0-9]+)/((?:0[1-9]|1[0-2])/[0-9]{4})$','i');
        IF new_match IS NOT NULL AND NOT reserved THEN
            INSERT INTO public.invoice_number_counters(period,last_number)
            VALUES(new_match[2],(new_match[1])::bigint)
            ON CONFLICT(period) DO UPDATE
            SET last_number=greatest(invoice_number_counters.last_number,excluded.last_number);
        END IF;
        DELETE FROM public.invoice_number_claims
         WHERE lower(trim(invoice_no))=lower(trim(NEW.invoice_no))
           AND NOT EXISTS(SELECT 1 FROM public.ksef_documents k WHERE k.invoice_id=NEW.id
             AND (coalesce(k.ksef_number,'')<>'' OR k.sent_at IS NOT NULL
               OR lower(coalesce(k.status,'')) IN ('sending','processing','unknown','sent','accepted')));
    END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER maintain_invoice_number_cursor
AFTER INSERT OR UPDATE OR DELETE ON public.invoices
FOR EACH ROW EXECUTE FUNCTION public.maintain_invoice_number_cursor();

CREATE OR REPLACE FUNCTION public.retain_final_invoice_number() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
BEGIN
    IF coalesce(NEW.ksef_number,'')<>'' OR NEW.sent_at IS NOT NULL
       OR lower(coalesce(NEW.status,'')) IN ('sending','processing','unknown','sent','accepted') THEN
        INSERT INTO public.invoice_number_claims(invoice_no)
        SELECT invoice_no FROM public.invoices WHERE id=NEW.invoice_id
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER retain_final_invoice_number
AFTER INSERT OR UPDATE ON public.ksef_documents
FOR EACH ROW EXECUTE FUNCTION public.retain_final_invoice_number();

INSERT INTO public.invoice_number_claims(invoice_no)
SELECT i.invoice_no
FROM public.invoices i JOIN public.ksef_documents k ON k.invoice_id=i.id
WHERE coalesce(k.ksef_number,'')<>'' OR k.sent_at IS NOT NULL
   OR lower(coalesce(k.status,'')) IN ('sending','processing','unknown','sent','accepted')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION public.reserve_invoice_number(
    p_period text,p_custom text DEFAULT '',p_requested_min bigint DEFAULT 0
) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public AS $$
DECLARE n bigint; result text; custom_match text[];
BEGIN
    IF p_period !~ '^(0[1-9]|1[0-2])/[0-9]{4}$' THEN
        RAISE EXCEPTION 'Invalid period';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('invoice-number:' || p_period,0));
    IF coalesce(trim(p_custom),'')<>'' THEN
        result := trim(p_custom);
        PERFORM pg_advisory_xact_lock(hashtextextended('custom-invoice-number:' || lower(result),0));
        IF EXISTS(SELECT 1 FROM public.invoices WHERE lower(trim(invoice_no))=lower(result))
           OR EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result)) THEN
            RAISE EXCEPTION 'Invoice number was already used';
        END IF;
        custom_match := regexp_match(result,'^FVAT ([0-9]+)/' || p_period || '$','i');
        IF custom_match IS NOT NULL THEN
            INSERT INTO public.invoice_number_counters(period,last_number)
            VALUES(p_period,(custom_match[1])::bigint)
            ON CONFLICT(period) DO UPDATE SET last_number=excluded.last_number;
        END IF;
    ELSE
        SELECT greatest(coalesce((SELECT last_number FROM public.invoice_number_counters
                                  WHERE period=p_period),0)+1,coalesce(p_requested_min,0),1) INTO n;
        LOOP
            result := 'FVAT ' || n || '/' || p_period;
            EXIT WHEN NOT EXISTS(SELECT 1 FROM public.invoices WHERE lower(trim(invoice_no))=lower(result))
                       AND NOT EXISTS(SELECT 1 FROM public.invoice_number_claims WHERE lower(trim(invoice_no))=lower(result));
            n := n+1;
        END LOOP;
        INSERT INTO public.invoice_number_counters(period,last_number) VALUES(p_period,n)
        ON CONFLICT(period) DO UPDATE SET last_number=excluded.last_number;
    END IF;
    INSERT INTO public.invoice_number_claims(invoice_no) VALUES(result);
    RETURN result;
END $$;

REVOKE ALL ON FUNCTION public.reserve_invoice_number(text,text,bigint),
    public.maintain_invoice_number_cursor(),public.retain_final_invoice_number()
FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.reserve_invoice_number(text,text,bigint) TO service_role;

COMMIT;

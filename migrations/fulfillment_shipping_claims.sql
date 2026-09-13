-- Service-role-only durable deduplication. No customer policy is changed.
CREATE TABLE IF NOT EXISTS public.fulfillment_shipping_claims (
    order_id bigint PRIMARY KEY,
    reference text NOT NULL,
    payload jsonb NOT NULL,
    content_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('SENDING','UNKNOWN','SUCCESS')),
    provider_json jsonb,
    created_at text NOT NULL
);
ALTER TABLE public.fulfillment_shipping_claims ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.fulfillment_shipping_claims FROM anon, authenticated;
GRANT ALL ON public.fulfillment_shipping_claims TO service_role;

CREATE OR REPLACE FUNCTION public.claim_fulfillment_shipment(
    p_order_ids bigint[], p_reference text, p_payload jsonb,
    p_content_hash text, p_created_at text
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE oid bigint; existing public.fulfillment_shipping_claims;
BEGIN
    IF cardinality(p_order_ids) IS NULL OR cardinality(p_order_ids) = 0 OR cardinality(p_order_ids) > 100
       OR p_reference IS NULL OR length(p_reference) > 100 THEN
        RAISE EXCEPTION 'Invalid shipment claim';
    END IF;
    FOR oid IN SELECT DISTINCT unnest(p_order_ids) ORDER BY 1 LOOP
        IF oid IS NULL OR oid <= 0 THEN RAISE EXCEPTION 'Invalid order'; END IF;
        PERFORM pg_advisory_xact_lock(oid);
    END LOOP;
    SELECT * INTO existing FROM public.fulfillment_shipping_claims
        WHERE order_id = ANY(p_order_ids) ORDER BY order_id LIMIT 1;
    IF FOUND THEN
        -- Only the existing, provider-verified prepare_next history can close
        -- a previous shipment cycle. Mere timeouts or missing IDs never do.
        IF existing.state = 'SUCCESS' AND existing.provider_json->>'id' IS NOT NULL
           AND (SELECT array_agg(order_id ORDER BY order_id) FROM public.fulfillment_shipping_claims WHERE reference=existing.reference)
               = (SELECT array_agg(DISTINCT x ORDER BY x) FROM unnest(p_order_ids) x)
           AND NOT EXISTS (
               SELECT 1 FROM public.fulfillment_shipping_claims c WHERE c.reference=existing.reference AND (
                   NOT EXISTS (SELECT 1 FROM public.inpost_shipment_history h WHERE h.order_id=c.order_id AND h.shipment_id=existing.provider_json->>'id')
                   OR EXISTS (SELECT 1 FROM public.orders o WHERE o.id=c.order_id AND o.inpost_shipment_id=existing.provider_json->>'id')
               )
           ) THEN
            DELETE FROM public.fulfillment_shipping_claims WHERE reference=existing.reference;
        ELSE
            RETURN jsonb_build_object('acquired', false, 'claim', to_jsonb(existing));
        END IF;
    END IF;
    INSERT INTO public.fulfillment_shipping_claims(order_id,reference,payload,content_hash,state,created_at)
        SELECT DISTINCT unnest(p_order_ids),p_reference,p_payload,p_content_hash,'SENDING',p_created_at;
    SELECT * INTO existing FROM public.fulfillment_shipping_claims WHERE order_id = p_order_ids[1];
    RETURN jsonb_build_object('acquired', true, 'claim', to_jsonb(existing));
END;
$$;
REVOKE ALL ON FUNCTION public.claim_fulfillment_shipment(bigint[],text,jsonb,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_fulfillment_shipment(bigint[],text,jsonb,text,text) TO service_role;

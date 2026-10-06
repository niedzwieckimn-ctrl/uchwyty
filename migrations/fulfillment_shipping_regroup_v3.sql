-- V3: replace only the claim function; preserve existing tables and data.
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
    -- Inspect ALL intersecting older groups before changing any guard. A new
    -- parcel may combine remainders of several shipments plus new orders.
    FOR existing IN SELECT * FROM public.fulfillment_shipping_claims
        WHERE order_id = ANY(p_order_ids) ORDER BY order_id LOOP
        IF NOT (existing.state = 'SUCCESS' AND COALESCE(existing.provider_json->>'id','') <> ''
           AND EXISTS (SELECT 1 FROM public.inpost_shipment_history h
                       WHERE h.order_id=existing.order_id AND h.shipment_id=existing.provider_json->>'id')
           AND EXISTS (SELECT 1 FROM public.orders o WHERE o.id=existing.order_id
                       AND COALESCE(o.inpost_shipment_id,'') <> existing.provider_json->>'id')) THEN
            RETURN jsonb_build_object('acquired', false, 'claim', to_jsonb(existing));
        END IF;
    END LOOP;
    -- Keep protection for every old member that is NOT in this parcel.
    DELETE FROM public.fulfillment_shipping_claims WHERE order_id = ANY(p_order_ids);
    INSERT INTO public.fulfillment_shipping_claims(order_id,reference,payload,content_hash,state,created_at)
        SELECT DISTINCT unnest(p_order_ids),p_reference,p_payload,p_content_hash,'SENDING',p_created_at;
    SELECT * INTO existing FROM public.fulfillment_shipping_claims WHERE order_id = p_order_ids[1];
    RETURN jsonb_build_object('acquired', true, 'claim', to_jsonb(existing));
END;
$$;
REVOKE ALL ON FUNCTION public.claim_fulfillment_shipment(bigint[],text,jsonb,text,text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.claim_fulfillment_shipment(bigint[],text,jsonb,text,text) TO service_role;

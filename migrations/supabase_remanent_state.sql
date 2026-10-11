-- Private durable Remanent state.  One row holds global source data and one row
-- each count session.  The server writes with service_role; browsers get no
-- grants or policies.
CREATE TABLE IF NOT EXISTS public.internal_remanent_state (
    scope_id text PRIMARY KEY,
    state_json jsonb NOT NULL,
    checksum text NOT NULL CHECK (length(checksum) = 64),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE public.internal_remanent_state ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE public.internal_remanent_state FROM PUBLIC, anon, authenticated;
GRANT ALL ON TABLE public.internal_remanent_state TO service_role;

CREATE OR REPLACE FUNCTION public.persist_internal_remanent_state(
    p_scope_id text,
    p_state_json jsonb,
    p_checksum text,
    p_expected_revision bigint
) RETURNS bigint
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_revision bigint;
BEGIN
    IF p_scope_id IS NULL OR p_scope_id = '' OR p_state_json IS NULL
       OR p_checksum !~ '^[0-9a-f]{64}$' OR p_expected_revision < 0 THEN
        RAISE EXCEPTION 'invalid remanent state';
    END IF;

    IF p_expected_revision = 0 THEN
        INSERT INTO public.internal_remanent_state(
            scope_id,state_json,checksum,schema_version,revision,updated_at
        ) VALUES (p_scope_id,p_state_json,p_checksum,1,1,now())
        ON CONFLICT (scope_id) DO NOTHING
        RETURNING revision INTO v_revision;
    ELSE
        UPDATE public.internal_remanent_state
           SET state_json=p_state_json,
               checksum=p_checksum,
               schema_version=1,
               revision=revision+1,
               updated_at=now()
         WHERE scope_id=p_scope_id AND revision=p_expected_revision
         RETURNING revision INTO v_revision;
    END IF;

    IF v_revision IS NULL THEN
        RAISE EXCEPTION 'remanent revision conflict for %', p_scope_id
            USING ERRCODE = '40001';
    END IF;
    RETURN v_revision;
END;
$$;

REVOKE ALL ON FUNCTION public.persist_internal_remanent_state(text,jsonb,text,bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.persist_internal_remanent_state(text,jsonb,text,bigint)
    TO service_role;

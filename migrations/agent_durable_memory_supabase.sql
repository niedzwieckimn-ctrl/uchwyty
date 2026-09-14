BEGIN;

CREATE TABLE IF NOT EXISTS public.internal_agent_memory (
    memory_id text PRIMARY KEY,
    memory_key text NOT NULL,
    category text NOT NULL CHECK (category IN ('work_preferences', 'procedures')),
    scope text NOT NULL CHECK (scope IN ('company', 'user')),
    human_actor_id text NOT NULL DEFAULT '',
    content text NOT NULL,
    relevance_terms jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(relevance_terms) = 'array'),
    source_run_id text NOT NULL,
    confirmed_by_actor_id text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    updated_at timestamptz NOT NULL,
    CONSTRAINT internal_agent_memory_user_scope CHECK (
        (scope = 'company' AND human_actor_id = '')
        OR (scope = 'user' AND human_actor_id <> '')
    )
);

-- RBAC actors are authoritative in the internal runtime SQLite database and
-- are not replicated to Supabase. Keep the verified actor id as mandatory
-- audit metadata without requiring a remote parent row.
ALTER TABLE public.internal_agent_memory
    DROP CONSTRAINT IF EXISTS internal_agent_memory_confirmed_by_actor_id_fkey;

CREATE UNIQUE INDEX IF NOT EXISTS internal_agent_memory_identity
    ON public.internal_agent_memory (
        category,
        scope,
        human_actor_id,
        lower(memory_key)
    );

CREATE INDEX IF NOT EXISTS idx_internal_agent_memory_updated
    ON public.internal_agent_memory (updated_at DESC);

ALTER TABLE public.internal_agent_memory ENABLE ROW LEVEL SECURITY;

REVOKE ALL PRIVILEGES ON TABLE public.internal_agent_memory
    FROM anon, authenticated;

COMMIT;

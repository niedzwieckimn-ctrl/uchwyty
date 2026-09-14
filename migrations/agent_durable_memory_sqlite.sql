-- Versioned local cache migration. Applied idempotently by agent_conversation.initialize_schema.
CREATE TABLE IF NOT EXISTS internal_agent_memory(
 memory_id TEXT PRIMARY KEY,
 memory_key TEXT NOT NULL COLLATE NOCASE,
 category TEXT NOT NULL CHECK(category IN ('work_preferences','procedures')),
 scope TEXT NOT NULL CHECK(scope IN ('company','user')),
 human_actor_id TEXT NOT NULL DEFAULT '',
 content TEXT NOT NULL,
 relevance_terms_json TEXT NOT NULL DEFAULT '[]',
 source_run_id TEXT NOT NULL,
 confirmed_by_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id),
 version INTEGER NOT NULL CHECK(version>0),
 updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS internal_agent_memory_identity
 ON internal_agent_memory(category,scope,human_actor_id,memory_key COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_internal_agent_memory_updated
 ON internal_agent_memory(updated_at DESC);

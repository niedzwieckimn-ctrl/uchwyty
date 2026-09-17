-- Additive local metadata. No business data or remote schema changes.
CREATE TABLE IF NOT EXISTS internal_agent_turn_owners (
 conversation_id TEXT PRIMARY KEY REFERENCES internal_agent_conversations(conversation_id),
 run_id TEXT NOT NULL,
 owner_json TEXT NOT NULL
);

-- Internal AI tables only. Applied by the existing local init_db on deployment.
-- No customer tables, remote migrations or historical text reconstruction.
CREATE TABLE IF NOT EXISTS internal_agent_conversations(
 conversation_id TEXT PRIMARY KEY,
 human_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id),
 ai_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id),
 state_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,last_active_at TEXT NOT NULL,expires_at TEXT NOT NULL,reset_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_internal_agent_conversations_owner ON internal_agent_conversations(human_actor_id,last_active_at);
CREATE INDEX IF NOT EXISTS idx_internal_agent_conversations_expiry ON internal_agent_conversations(expires_at);
CREATE TABLE IF NOT EXISTS internal_agent_turns(
 turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
 run_id TEXT NOT NULL UNIQUE,
 conversation_id TEXT NOT NULL REFERENCES internal_agent_conversations(conversation_id),
 user_text TEXT NOT NULL,assistant_text TEXT,evidence_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_internal_agent_turns_history ON internal_agent_turns(conversation_id,turn_id);
CREATE TABLE IF NOT EXISTS internal_agent_turn_leases(
 conversation_id TEXT PRIMARY KEY REFERENCES internal_agent_conversations(conversation_id),
 run_id TEXT NOT NULL,expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS internal_agent_terminology(
 term TEXT PRIMARY KEY COLLATE NOCASE,meaning TEXT NOT NULL,
 scope TEXT NOT NULL CHECK(scope='company'),source TEXT NOT NULL CHECK(source='confirmed_by_user'),
 source_run_id TEXT NOT NULL,confirmed_by_actor_id TEXT NOT NULL REFERENCES internal_actors(actor_id),
 version INTEGER NOT NULL CHECK(version>0),updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS internal_agent_user_style(
 human_actor_id TEXT PRIMARY KEY REFERENCES internal_actors(actor_id),
 preferences_json TEXT NOT NULL DEFAULT '{}'
);

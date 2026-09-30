"""Schema migrations, applied in order. ``PRAGMA user_version`` records the
number of migrations applied. Never edit a released migration; append a new
one instead.

Timestamps are Unix seconds (INTEGER) unless noted.
"""

_V1_FOUNDATIONS = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every chat the bot has seen. status is the owner's decision; membership is
-- what Telegram last reported about the bot itself.
CREATE TABLE chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    type TEXT NOT NULL DEFAULT 'group',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'enabled', 'disabled')),
    membership TEXT NOT NULL DEFAULT 'unknown',
    added_by_user_id INTEGER,
    added_by_name TEXT,
    can_pin INTEGER,
    can_delete INTEGER,
    rights_checked_at INTEGER,
    owner_notified_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    status_changed_at INTEGER,
    last_activity_at INTEGER
);

-- Old chat IDs that now point at another chat (basic group -> supergroup).
CREATE TABLE chat_id_aliases (
    alias_chat_id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX chat_id_aliases_chat ON chat_id_aliases (chat_id);

-- Per-chat roster.
CREATE TABLE members (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    username TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live',
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

-- Nicknames set with /alias.
CREATE TABLE member_aliases (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id, alias)
);

-- Live and imported messages. chat_id is the current (logical) chat;
-- origin_chat_id is the Telegram chat the message was recorded in, which
-- differs from chat_id after a group upgrade. message_id and
-- reply_to_message_id are in the origin chat's ID space (Telegram IDs for
-- live messages, export IDs for imported ones).
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    origin_chat_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('live', 'import')),
    message_id INTEGER NOT NULL,
    import_id INTEGER,
    thread_id INTEGER,
    sender_id INTEGER,
    sender_name TEXT NOT NULL,
    sender_username TEXT,
    from_bot INTEGER NOT NULL DEFAULT 0,
    date INTEGER NOT NULL,
    edit_date INTEGER,
    text TEXT NOT NULL DEFAULT '',
    media_kind TEXT,
    media_file_id TEXT,
    media_file_unique_id TEXT,
    media_meta TEXT,
    forwarded_from TEXT,
    reply_to_message_id INTEGER,
    reply_to_row_id INTEGER,
    reply_to_snippet TEXT,
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX messages_live_key
    ON messages (origin_chat_id, message_id) WHERE source = 'live';
CREATE UNIQUE INDEX messages_import_key
    ON messages (import_id, message_id) WHERE source = 'import';
CREATE INDEX messages_chat_order ON messages (chat_id, date, id);
CREATE INDEX messages_chat_sender ON messages (chat_id, sender_id);

CREATE VIRTUAL TABLE messages_fts USING fts5(
    text,
    content = 'messages',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER messages_fts_delete AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER messages_fts_update AFTER UPDATE OF text ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    INSERT INTO messages_fts (rowid, text) VALUES (new.id, new.text);
END;

-- Settings that differ from the code default (JSON values).
CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL
);

-- NULL old/new value means "default".
CREATE TABLE settings_history (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at INTEGER NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE INDEX settings_history_key ON settings_history (key, id);

-- Application log records (created_at is fractional seconds).
CREATE TABLE logs (
    id INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    level INTEGER NOT NULL,
    logger TEXT NOT NULL,
    chat_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX logs_created ON logs (created_at);
CREATE INDEX logs_chat ON logs (chat_id, id);
"""

_V2_AGENT_RUNS = """
-- One row per bot response: what was sent to the model and what came back.
-- Prompts contain chat content, so these follow the retention setting.
-- Times are fractional seconds.
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    trigger_row_id INTEGER,
    trigger_message_id INTEGER,
    user_id INTEGER,
    skill TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'empty', 'error')),
    model TEXT,
    prompt TEXT,
    prompt_tokens INTEGER,
    window_size INTEGER,
    dropped INTEGER,
    image_count INTEGER,
    reasoning TEXT,
    response TEXT,
    reply_message_ids TEXT,
    usage TEXT,
    latency_ms INTEGER,
    finish_reason TEXT,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX agent_runs_chat ON agent_runs (chat_id, id);
CREATE INDEX agent_runs_started ON agent_runs (started_at);
"""

_V3_IMPORTS = """
-- Telegram Desktop history imports. The uploaded file is kept only until
-- the import finishes or is discarded.
CREATE TABLE imports (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    status TEXT NOT NULL
        CHECK (status IN ('preview', 'running', 'done', 'failed', 'replaced', 'discarded')),
    file_name TEXT NOT NULL,
    file_path TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    export_name TEXT NOT NULL DEFAULT '',
    export_type TEXT NOT NULL DEFAULT '',
    export_id INTEGER,
    preview TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped_overlap INTEGER NOT NULL DEFAULT 0,
    skipped_retention INTEGER NOT NULL DEFAULT 0,
    skipped_service INTEGER NOT NULL DEFAULT 0,
    first_date INTEGER,
    last_date INTEGER,
    error TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER
);
CREATE INDEX imports_chat ON imports (chat_id, id);
"""

MIGRATIONS: list[str] = [
    _V1_FOUNDATIONS,
    _V2_AGENT_RUNS,
    _V3_IMPORTS,
]

# Tables whose rows belong to one chat and move with it on a group upgrade.
# Add new chat-scoped tables here when a migration creates them.
CHAT_SCOPED_TABLES = ("messages", "members", "member_aliases", "imports", "agent_runs")

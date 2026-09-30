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

_V4_PEOPLE = """
-- A person the owner knows, across every chat. Telegram user IDs are global,
-- so each account belongs to one person; someone with two accounts is one
-- person with two accounts. name is chosen by the owner; NULL shows the
-- account's Telegram name.
CREATE TABLE people (
    id INTEGER PRIMARY KEY,
    name TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Telegram accounts. telegram_name/username are the latest seen live;
-- export_name is the name a Telegram Desktop export used (the exporting
-- account's contact name for them).
CREATE TABLE accounts (
    user_id INTEGER PRIMARY KEY,
    person_id INTEGER NOT NULL REFERENCES people (id),
    telegram_name TEXT,
    username TEXT,
    export_name TEXT,
    is_bot INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
);
CREATE INDEX accounts_person ON accounts (person_id);
CREATE INDEX accounts_username ON accounts (username COLLATE NOCASE);

-- Nicknames, shared by every chat the person is in.
CREATE TABLE person_aliases (
    person_id INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (person_id, alias)
);

-- Existing roster: one person per account. The first person IDs equal the
-- user IDs; person IDs are internal and later ones are allocated normally.
INSERT INTO people (id, name, created_at, updated_at)
    SELECT user_id, NULL, MIN(first_seen_at), MAX(last_seen_at)
    FROM members WHERE user_id > 0 GROUP BY user_id;
INSERT INTO accounts (user_id, person_id, telegram_name, username, export_name, is_bot,
                      first_seen_at, last_seen_at)
    SELECT m.user_id, m.user_id,
        COALESCE(
            (SELECT display_name FROM members l WHERE l.user_id = m.user_id
                AND l.source = 'live' ORDER BY l.last_seen_at DESC LIMIT 1),
            (SELECT sender_name FROM messages x WHERE x.sender_id = m.user_id
                AND x.source = 'live' ORDER BY x.date DESC LIMIT 1)),
        (SELECT username FROM members l WHERE l.user_id = m.user_id
            AND l.source = 'live' AND l.username IS NOT NULL ORDER BY l.last_seen_at DESC LIMIT 1),
        COALESCE(
            (SELECT sender_name FROM messages x WHERE x.sender_id = m.user_id
                AND x.source = 'import' ORDER BY x.date DESC LIMIT 1),
            (SELECT display_name FROM members i WHERE i.user_id = m.user_id
                AND i.source = 'import' ORDER BY i.last_seen_at DESC LIMIT 1)),
        MAX(m.is_bot), MIN(m.first_seen_at), MAX(m.last_seen_at)
    FROM members m WHERE m.user_id > 0 GROUP BY m.user_id;
INSERT OR IGNORE INTO person_aliases (person_id, alias, created_at)
    SELECT user_id, alias, MIN(created_at) FROM member_aliases
    WHERE user_id IN (SELECT user_id FROM accounts) GROUP BY user_id, alias;
DROP TABLE member_aliases;

-- members is now only the per-chat roster; names live on accounts.
ALTER TABLE members DROP COLUMN display_name;
ALTER TABLE members DROP COLUMN username;
ALTER TABLE members DROP COLUMN is_bot;
"""

_V5_AGENT_TOOLS = """
-- The agent loop: every model request and tool call of a run, in order.
ALTER TABLE agent_runs ADD COLUMN steps TEXT;
ALTER TABLE agent_runs ADD COLUMN model_requests INTEGER;
ALTER TABLE agent_runs ADD COLUMN tool_calls INTEGER;

-- The pinned board: one per chat, edited in place. sections is a JSON object
-- of section -> [{"text": ..., "done": bool}]. message_chat_id is the
-- Telegram chat the board message lives in (it differs from chat_id after a
-- group upgrade, and then a new message is needed).
CREATE TABLE boards (
    chat_id INTEGER PRIMARY KEY,
    sections TEXT NOT NULL DEFAULT '{}',
    message_id INTEGER,
    message_chat_id INTEGER,
    format TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    published_at INTEGER,
    publish_error TEXT
);

-- Plans the bot proposed with Confirm / Change buttons.
CREATE TABLE plans (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    items TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'confirmed', 'cancelled')),
    message_id INTEGER,
    message_chat_id INTEGER,
    run_id INTEGER,
    proposed_for_user_id INTEGER,
    created_at INTEGER NOT NULL,
    decided_at INTEGER,
    decided_by_user_id INTEGER,
    decided_by_name TEXT
);
CREATE INDEX plans_chat ON plans (chat_id, id);
"""

_V6_MEMORY = """
-- "What's going on now" per chat, rewritten as messages arrive. The cursor
-- (last_message_date, last_row_id) is the newest message it has read.
CREATE TABLE digests (
    chat_id INTEGER PRIMARY KEY,
    text TEXT NOT NULL DEFAULT '',
    last_row_id INTEGER,
    last_message_date INTEGER,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    error TEXT,
    failed_at INTEGER
);

-- Group memory: durable facts that outlive message retention. person_id is
-- who the note is about (people.id), if anyone.
CREATE TABLE memory_notes (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    person_id INTEGER,
    source_row_ids TEXT,
    created_by TEXT NOT NULL,
    created_by_user_id INTEGER,
    locked INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX memory_notes_chat ON memory_notes (chat_id, id);

-- Every change to a note, including deletion (the last content is kept).
CREATE TABLE memory_note_history (
    id INTEGER PRIMARY KEY,
    note_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    content TEXT,
    category TEXT,
    person_id INTEGER,
    changed_at INTEGER NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE INDEX memory_note_history_note ON memory_note_history (note_id, id);

CREATE TABLE reminders (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    due_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'cancelled', 'failed')),
    created_by_user_id INTEGER,
    created_by TEXT NOT NULL,
    run_id INTEGER,
    created_at INTEGER NOT NULL,
    sent_at INTEGER,
    sent_message_id INTEGER,
    error TEXT
);
CREATE INDEX reminders_due ON reminders (status, due_at);
CREATE INDEX reminders_chat ON reminders (chat_id, id);

-- Image descriptions made on demand; they go with their message.
CREATE TABLE media_descriptions (
    message_row_id INTEGER PRIMARY KEY REFERENCES messages (id) ON DELETE CASCADE,
    chat_id INTEGER NOT NULL,
    description TEXT NOT NULL,
    model TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX media_descriptions_chat ON media_descriptions (chat_id);

-- Group memory distilled from an import (after its messages are stored).
ALTER TABLE imports ADD COLUMN distill_status TEXT;
ALTER TABLE imports ADD COLUMN distill_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN distill_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN notes_added INTEGER NOT NULL DEFAULT 0;
ALTER TABLE imports ADD COLUMN distill_error TEXT;
"""

_V7_REMINDER_RETRIES = """
-- A reminder Telegram couldn't take for a passing reason (network, time-out,
-- flood limit) stays pending and is tried again after next_attempt_at.
ALTER TABLE reminders ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE reminders ADD COLUMN next_attempt_at INTEGER;
"""

MIGRATIONS: list[str] = [
    _V1_FOUNDATIONS,
    _V2_AGENT_RUNS,
    _V3_IMPORTS,
    _V4_PEOPLE,
    _V5_AGENT_TOOLS,
    _V6_MEMORY,
    _V7_REMINDER_RETRIES,
]

# Tables whose rows belong to one chat and move with it on a group upgrade.
# Add new chat-scoped tables here when a migration creates them.
CHAT_SCOPED_TABLES = ("messages", "members", "imports", "agent_runs", "boards", "plans",
                      "digests", "memory_notes", "memory_note_history", "reminders",
                      "media_descriptions")

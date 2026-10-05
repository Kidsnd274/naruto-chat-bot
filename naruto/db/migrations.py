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

_V8_CHAT_SETTINGS = """
-- Per-chat overrides of settings marked per_chat in the registry; a chat
-- without a row uses the global value.
CREATE TABLE chat_settings (
    chat_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    PRIMARY KEY (chat_id, key)
);
"""

_V9_STABLE_NOTE_IDS = """
-- Note IDs are never reused. Without AUTOINCREMENT, SQLite gave a new note
-- the ID of the most recently deleted one, so it inherited that note's
-- history (even from another chat) and old [n12] references reached it.
-- The sequence starts above every ID used so far, deleted notes included.
CREATE TABLE memory_notes_v9 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
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
INSERT INTO memory_notes_v9 (id, chat_id, content, category, person_id, source_row_ids,
                             created_by, created_by_user_id, locked, created_at, updated_at)
    SELECT id, chat_id, content, category, person_id, source_row_ids, created_by,
           created_by_user_id, locked, created_at, updated_at FROM memory_notes;
DROP TABLE memory_notes;
ALTER TABLE memory_notes_v9 RENAME TO memory_notes;
CREATE INDEX memory_notes_chat ON memory_notes (chat_id, id);
DELETE FROM sqlite_sequence WHERE name IN ('memory_notes', 'memory_notes_v9');
INSERT INTO sqlite_sequence (name, seq) VALUES ('memory_notes', MAX(
    COALESCE((SELECT MAX(id) FROM memory_notes), 0),
    COALESCE((SELECT MAX(note_id) FROM memory_note_history), 0)));

-- Every change to a digest's text bumps its revision, so a background
-- update can tell that the owner edited (or deleted) it meanwhile.
ALTER TABLE digests ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
"""

_V10_MODEL_QUEUE = """
-- Every model request, for the web admin's queue page: what it was for,
-- its priority and how long it waited and ran. No prompts. Times are
-- fractional seconds. Follows the agent-run retention.
CREATE TABLE model_requests (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    task TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('foreground', 'background')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'retrying', 'done', 'failed',
                                         'cancelled', 'expired', 'interrupted')),
    run_id INTEGER,
    import_id INTEGER,
    period_id INTEGER,
    chunk INTEGER,
    attempts INTEGER NOT NULL DEFAULT 1,
    queued_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    error TEXT
);
CREATE INDEX model_requests_state ON model_requests (state);
CREATE INDEX model_requests_chat ON model_requests (chat_id, id);
CREATE INDEX model_requests_queued ON model_requests (queued_at);
"""

_V11_HISTORY = """
-- When the bot started recording a chat live. Imports stop here, so live
-- and imported history (and their summaries) never overlap. Unlike the
-- first stored live message, it doesn't move when retention deletes
-- messages. Backfilled from the earliest live message.
ALTER TABLE chats ADD COLUMN recording_since INTEGER;
UPDATE chats SET recording_since = (
    SELECT MIN(date) FROM messages WHERE messages.chat_id = chats.chat_id AND source = 'live');
UPDATE chats SET recording_since = status_changed_at
    WHERE recording_since IS NULL AND status = 'enabled';

-- Imports get separate stages (raw messages, history summaries, memory
-- notes), each with its own status, and can pause and resume. Rebuilt
-- because SQLite can't change a CHECK constraint.
CREATE TABLE imports_v11 (
    id INTEGER PRIMARY KEY,
    chat_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('preview', 'running', 'paused', 'done', 'partial',
                                           'failed', 'replaced', 'discarded')),
    file_name TEXT NOT NULL,
    file_path TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    export_name TEXT NOT NULL DEFAULT '',
    export_type TEXT NOT NULL DEFAULT '',
    export_id INTEGER,
    preview TEXT,
    options TEXT,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped_overlap INTEGER NOT NULL DEFAULT 0,
    skipped_retention INTEGER NOT NULL DEFAULT 0,
    skipped_service INTEGER NOT NULL DEFAULT 0,
    skipped_range INTEGER NOT NULL DEFAULT 0,
    first_date INTEGER,
    last_date INTEGER,
    error TEXT,
    raw_status TEXT,
    archive_status TEXT,
    archive_total INTEGER NOT NULL DEFAULT 0,
    archive_done INTEGER NOT NULL DEFAULT 0,
    archive_error TEXT,
    distill_status TEXT,
    distill_total INTEGER NOT NULL DEFAULT 0,
    distill_done INTEGER NOT NULL DEFAULT 0,
    notes_added INTEGER NOT NULL DEFAULT 0,
    distill_error TEXT,
    limitations TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    paused_at INTEGER,
    source_expires_at INTEGER
);
INSERT INTO imports_v11 (id, chat_id, status, file_name, file_path, file_size, export_name,
                         export_type, export_id, preview, total, processed, imported,
                         skipped_overlap, skipped_retention, skipped_service, first_date,
                         last_date, error, raw_status, distill_status, distill_total,
                         distill_done, notes_added, distill_error, created_at, started_at,
                         finished_at)
    SELECT id, chat_id, status, file_name, file_path, file_size, export_name, export_type,
           export_id, preview, total, processed, imported, skipped_overlap, skipped_retention,
           skipped_service, first_date, last_date, error,
           CASE status WHEN 'done' THEN 'done' WHEN 'replaced' THEN 'done'
                       WHEN 'failed' THEN 'failed' WHEN 'running' THEN 'running' END,
           distill_status, distill_total, distill_done, notes_added, distill_error, created_at,
           started_at, finished_at
    FROM imports;
DROP TABLE imports;
ALTER TABLE imports_v11 RENAME TO imports;
CREATE INDEX imports_chat ON imports (chat_id, id);

-- Dated summaries of past periods (a month, a week or a chosen range). They
-- outlive raw messages and uploads, so they keep their own provenance.
-- period_end is exclusive; first/last_message_at is what was actually read.
CREATE TABLE history_digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'staged', 'replaced')),
    source TEXT NOT NULL CHECK (source IN ('export', 'live')),
    grouping TEXT NOT NULL CHECK (grouping IN ('month', 'week', 'range')),
    timezone TEXT NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    first_message_at INTEGER,
    last_message_at INTEGER,
    message_count INTEGER NOT NULL DEFAULT 0,
    import_id INTEGER,
    period_id INTEGER,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    limitations TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE INDEX history_digests_chat ON history_digests (chat_id, status, period_start);

CREATE VIRTUAL TABLE history_digests_fts USING fts5(
    text,
    content = 'history_digests',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER history_digests_fts_insert AFTER INSERT ON history_digests BEGIN
    INSERT INTO history_digests_fts (rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER history_digests_fts_delete AFTER DELETE ON history_digests BEGIN
    INSERT INTO history_digests_fts (history_digests_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER history_digests_fts_update AFTER UPDATE OF text ON history_digests BEGIN
    INSERT INTO history_digests_fts (history_digests_fts, rowid, text)
        VALUES ('delete', old.id, old.text);
    INSERT INTO history_digests_fts (rowid, text) VALUES (new.id, new.text);
END;

-- The text before every owner edit.
CREATE TABLE history_digest_edits (
    id INTEGER PRIMARY KEY,
    digest_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    changed_at INTEGER NOT NULL,
    changed_by TEXT NOT NULL
);
CREATE INDEX history_digest_edits_digest ON history_digest_edits (digest_id, id);

-- Work on one period: its progress, so an interrupted or failed summary
-- resumes where it stopped. consumed is how many of the period's messages
-- (in date order) have been read; partial is the summary so far.
CREATE TABLE history_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('export', 'live')),
    import_id INTEGER,
    grouping TEXT NOT NULL,
    timezone TEXT NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('waiting', 'running', 'done', 'reused', 'failed',
                                           'cancelled')),
    message_count INTEGER NOT NULL DEFAULT 0,
    consumed INTEGER NOT NULL DEFAULT 0,
    chunks_done INTEGER NOT NULL DEFAULT 0,
    partial TEXT,
    fingerprint TEXT,
    replaces TEXT,
    digest_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX history_periods_import ON history_periods (import_id, period_start);
CREATE INDEX history_periods_chat ON history_periods (chat_id, source, period_start);
"""

_V12_LAB = """
-- The prompt lab (plans/done/SELF_LEARNING_LOOP_TECH_PLAN.md). Runs test
-- candidate configurations on scenarios in sandboxes; nothing here changes
-- the live bot until a candidate is activated. Times are Unix seconds.

-- Which lab attempt a model request belongs to (task 'lab').
ALTER TABLE model_requests ADD COLUMN lab_attempt_id INTEGER;

-- API tokens for external agents. Only a hash is kept. chats: the chats
-- whose real messages and traces the token may read (JSON list).
CREATE TABLE lab_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    chats TEXT NOT NULL DEFAULT '[]',
    may_activate INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);

-- One tuning run: an objective, its scope and budget, the model it tests
-- and a frozen copy of every setting when it started (the baseline).
CREATE TABLE lab_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    objective TEXT NOT NULL,
    spec TEXT NOT NULL,
    model_endpoint TEXT NOT NULL,
    model_name TEXT NOT NULL,
    model_reported TEXT,
    baseline TEXT NOT NULL,
    baseline_history_id INTEGER NOT NULL DEFAULT 0,
    code_fingerprint TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'finished')),
    stop_reason TEXT,
    recommendation TEXT,
    summary TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    token_id INTEGER,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);

-- A candidate configuration: changes on top of its parent (NULL: the
-- baseline). Never edited; a revision is a new candidate.
CREATE TABLE lab_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    number INTEGER NOT NULL,
    name TEXT NOT NULL,
    parent_id INTEGER,
    changes TEXT NOT NULL,
    hypothesis TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    settings_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, number)
);

-- Scenarios, versioned: an edit adds a version with a reason. chat_id is
-- the chat a scenario was made from (real chat content), if any.
CREATE TABLE lab_scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    origin TEXT NOT NULL,
    focused INTEGER NOT NULL DEFAULT 0,
    chat_id INTEGER,
    reason TEXT,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (slug, version)
);

-- Sets of scenarios with a purpose. Removing one keeps the row (removed_at,
-- reason), so reports can show what changed.
CREATE TABLE lab_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK (purpose IN ('tuning', 'validation', 'regression')),
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, name)
);
CREATE TABLE lab_set_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    set_id INTEGER NOT NULL,
    slug TEXT NOT NULL,
    added_at INTEGER NOT NULL,
    removed_at INTEGER,
    reason TEXT
);
CREATE INDEX lab_set_items_set ON lab_set_items (set_id);

-- A batch of attempts started together (a suite, or one request).
CREATE TABLE lab_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'cancelled',
                                           'interrupted', 'budget_exhausted')),
    owner_request TEXT,
    created_at INTEGER NOT NULL,
    finished_at INTEGER
);

-- One scenario under one configuration (candidate_id NULL: the baseline).
CREATE TABLE lab_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    batch_id INTEGER,
    scenario_id INTEGER NOT NULL,
    candidate_id INTEGER,
    repeat INTEGER NOT NULL DEFAULT 1,
    continue_from INTEGER,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'cancelled',
                                           'interrupted', 'skipped')),
    outcome TEXT,
    reason TEXT,
    result TEXT,
    conditions TEXT,
    model_requests INTEGER NOT NULL DEFAULT 0,
    model_ms INTEGER NOT NULL DEFAULT 0,
    wait_ms INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    queued_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER,
    first_viewed_at INTEGER,
    state_path TEXT
);
CREATE INDEX lab_attempts_run ON lab_attempts (run_id, id);
CREATE INDEX lab_attempts_batch ON lab_attempts (batch_id);

-- AI and owner judgments of one turn of an attempt, against a rubric
-- criterion. evidence: quotes from the attempt (JSON list).
CREATE TABLE lab_judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('ai', 'owner')),
    criterion TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'score')),
    score REAL,
    evidence TEXT NOT NULL DEFAULT '[]',
    comment TEXT,
    judge TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX lab_judgments_attempt ON lab_judgments (attempt_id);

-- The run's rubric, versioned; proposed until the owner confirms it.
CREATE TABLE lab_rubrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    criteria TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed')),
    confirmation TEXT,
    reason TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, version)
);

-- A/B comparisons for the owner. mapping: label -> attempt id, chosen at
-- random by the server.
CREATE TABLE lab_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    scenario_id INTEGER NOT NULL,
    turn INTEGER NOT NULL,
    mapping TEXT NOT NULL,
    presentation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'answered', 'withdrawn')),
    revealed_before_answer INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    answered_at INTEGER
);
CREATE TABLE lab_choices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id INTEGER NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('A', 'B', 'C', 'D', 'both_good', 'both_bad',
                                           'no_preference', 'skip', 'combination')),
    comment TEXT,
    channel TEXT NOT NULL,
    supersedes INTEGER,
    created_at INTEGER NOT NULL
);

-- The owner's preferences as understood so far, versioned.
CREATE TABLE lab_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    body TEXT NOT NULL,
    edited_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE (run_id, version)
);

-- Notes the agent files for the report: suspected defects, observations,
-- assumptions.
CREATE TABLE lab_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('defect', 'observation', 'assumption')),
    text TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

-- A candidate applied to the live settings, and its undoing.
CREATE TABLE lab_activations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    model_endpoint TEXT NOT NULL,
    model_name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('changes', 'full')),
    previous TEXT NOT NULL,
    applied TEXT NOT NULL,
    drift TEXT,
    evidence TEXT,
    authorized_by TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    reverted_at INTEGER,
    reverted_by TEXT
);

-- What happened in a run, for the report: budget hits, refused calls,
-- reveals, scenario and rubric changes, drift.
CREATE TABLE lab_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL
);
CREATE INDEX lab_events_run ON lab_events (run_id, id);
"""

_V13_KEEP_MESSAGES = """
-- plans/done/MEMORY_SIMPLIFICATION_AND_STABILITY_PLAN.md: messages are kept until
-- the owner deletes them, imports no longer distill memory notes, and a
-- history period has at most one summary.

ALTER TABLE imports DROP COLUMN skipped_retention;
ALTER TABLE imports DROP COLUMN distill_status;
ALTER TABLE imports DROP COLUMN distill_total;
ALTER TABLE imports DROP COLUMN distill_done;
ALTER TABLE imports DROP COLUMN notes_added;
ALTER TABLE imports DROP COLUMN distill_error;

-- A crash between saving a summary and finishing its period could leave two
-- summaries of one period. Keep the one the period points at; drop unedited
-- extras and detach edited ones, so no owner edit is lost.
DELETE FROM history_digests
 WHERE period_id IS NOT NULL AND edited = 0
   AND id NOT IN (SELECT digest_id FROM history_periods WHERE digest_id IS NOT NULL)
   AND EXISTS (SELECT 1 FROM history_digests AS other
                WHERE other.period_id = history_digests.period_id
                  AND other.id != history_digests.id);
UPDATE history_digests SET period_id = NULL
 WHERE period_id IS NOT NULL
   AND id NOT IN (SELECT digest_id FROM history_periods WHERE digest_id IS NOT NULL)
   AND EXISTS (SELECT 1 FROM history_digests AS other
                WHERE other.period_id = history_digests.period_id
                  AND other.id != history_digests.id);
CREATE UNIQUE INDEX history_digests_period ON history_digests (period_id)
    WHERE period_id IS NOT NULL;

-- What a period's partial summary was made with: the settings that shape it,
-- and the messages read so far (in order). A resumed period whose hashes no
-- longer match starts over. Unfinished periods from before have neither, so
-- they start over too.
ALTER TABLE history_periods ADD COLUMN settings_hash TEXT;
ALTER TABLE history_periods ADD COLUMN source_hash TEXT;
"""

_V14_BOARD_TITLE = """
-- The board's first line, also shown in the pin bar. NULL: built from the
-- plan names. (Decisions now live under their plan; boards that still have
-- a "decided" section are folded into a "Decided" plan when read.)
ALTER TABLE boards ADD COLUMN title TEXT;
"""

_V15_DELETED_MESSAGES = """
-- plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md: when the bot deletes one
-- of its own messages in Telegram, its row stays (history and summaries stay
-- consistent) but leaves the model's recent messages and searches.
ALTER TABLE messages ADD COLUMN deleted_at INTEGER;
"""

_V16_NO_PLAN_CARDS = """
-- plans/TELEGRAM_PERMISSIONS_AND_CHAT_CLUTTER_PLAN.md §7: plans are kept on the
-- board, and the bot no longer posts plan cards to confirm. Open proposals are
-- closed; the table stays as history.
UPDATE plans SET status = 'cancelled', decided_at = CAST(strftime('%s', 'now') AS INTEGER),
       decided_by_name = 'plan cards removed'
 WHERE status = 'proposed';
"""

MIGRATIONS: list[str] = [
    _V1_FOUNDATIONS,
    _V2_AGENT_RUNS,
    _V3_IMPORTS,
    _V4_PEOPLE,
    _V5_AGENT_TOOLS,
    _V6_MEMORY,
    _V7_REMINDER_RETRIES,
    _V8_CHAT_SETTINGS,
    _V9_STABLE_NOTE_IDS,
    _V10_MODEL_QUEUE,
    _V11_HISTORY,
    _V12_LAB,
    _V13_KEEP_MESSAGES,
    _V14_BOARD_TITLE,
    _V15_DELETED_MESSAGES,
    _V16_NO_PLAN_CARDS,
]

# Tables whose rows belong to one chat and move with it on a group upgrade.
# Add new chat-scoped tables here when a migration creates them.
CHAT_SCOPED_TABLES = ("messages", "members", "imports", "agent_runs", "boards", "plans",
                      "digests", "memory_notes", "memory_note_history", "reminders",
                      "media_descriptions", "chat_settings", "model_requests",
                      "history_digests", "history_digest_edits", "history_periods")

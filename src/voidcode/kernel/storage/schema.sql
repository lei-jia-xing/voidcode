-- Kernel schema v2. The store verifies an existing database's identity BEFORE
-- setting pragmas or executing this DDL; foreign/legacy files are never changed
-- and a v1 database is refused read-only instead of being migrated in place.
-- Future plugin-state/tasks/wakeups tables are added with their actual owners.

CREATE TABLE kernel_identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    magic TEXT NOT NULL CHECK (magic = 'voidcode-kernel-v1'),
    schema_version INTEGER NOT NULL CHECK (schema_version = 2)
) STRICT;

CREATE TABLE revisions (
    revision_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT;

-- Workspace-level capability pointer: the revision a restarted kernel restores.
-- Only the activation transaction (and the first adoption of a database) writes
-- it, and every session ref in the workspace moves with it in that one commit.
CREATE TABLE workspace_state (
    workspace TEXT PRIMARY KEY,
    active_revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('idle', 'running', 'waiting', 'completed', 'failed', 'interrupted')),
    active_revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    run_id TEXT,
    turn INTEGER NOT NULL DEFAULT 0 CHECK (turn >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0)
) STRICT;

CREATE TABLE session_events (
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    run_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    PRIMARY KEY (session_id, sequence)
) STRICT;

-- Checkpoints contain event references, not replacement transcript bodies.
-- Event append + session projection + checkpoint commit in ONE transaction.
CREATE TABLE checkpoints (
    session_id TEXT PRIMARY KEY REFERENCES sessions (session_id),
    kind TEXT NOT NULL CHECK (kind IN ('ready', 'approval_wait', 'terminal', 'interrupted')),
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    run_id TEXT NOT NULL,
    last_event_sequence INTEGER NOT NULL CHECK (last_event_sequence >= 0),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE plugin_state (
    scope TEXT NOT NULL CHECK (scope IN ('session', 'workspace')),
    scope_key TEXT NOT NULL,
    owner TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope, scope_key, owner)
) STRICT;

CREATE TABLE action_confirmations (
    request_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    owner TEXT NOT NULL,
    command_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL CHECK (json_valid(arguments_json)),
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    expected_state_version INTEGER,
    ui_type TEXT NOT NULL CHECK (ui_type IN ('notice', 'markdown', 'fields', 'choices', 'confirmation', 'form', 'diff', 'status')),
    ui_data_json TEXT NOT NULL CHECK (json_valid(ui_data_json)),
    status TEXT NOT NULL CHECK (status IN ('pending', 'allowed', 'denied', 'stale')),
    created_at INTEGER NOT NULL,
    decided_at INTEGER
) STRICT;

CREATE TABLE continuations (
    continuation_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    idempotency_key TEXT,
    root_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX continuations_claim_idx ON continuations (session_id, status, created_at);

-- One finite action budget per root action chain (a user run, or the first
-- continuation of a chain). Reservations happen atomically with the dispatch
-- intent, so a crash after dispatch still consumes the step and restarts,
-- extra drains, and sibling continuations cannot extend the root budget.
CREATE TABLE root_budgets (
    root_id TEXT PRIMARY KEY,
    budget INTEGER NOT NULL CHECK (budget >= 0),
    reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE session_budgets (
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    owner TEXT NOT NULL,
    since_sequence INTEGER NOT NULL DEFAULT 0 CHECK (since_sequence >= 0),
    max_input_tokens INTEGER,
    max_output_tokens INTEGER,
    max_total_tokens INTEGER,
    max_cost_usd REAL,
    max_elapsed_seconds REAL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, owner)
) STRICT;

-- One delivery receipt per (source fact event, owner). Eligibility is derived
-- from the source event's OWN revision (its manifest declarations), not from
-- whoever is subscribed now, so a reload can neither re-deliver an old fact to
-- a new subscriber nor drop one an eligible owner already received.
CREATE TABLE observer_receipts (
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    event_sequence INTEGER NOT NULL,
    owner TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, event_sequence, owner)
) STRICT;

CREATE TRIGGER revisions_no_update BEFORE UPDATE ON revisions
BEGIN SELECT RAISE(ABORT, 'capability revisions are immutable'); END;
CREATE TRIGGER revisions_no_delete BEFORE DELETE ON revisions
BEGIN SELECT RAISE(ABORT, 'capability revisions are immutable'); END;
CREATE TRIGGER session_events_no_update BEFORE UPDATE ON session_events
BEGIN SELECT RAISE(ABORT, 'session events are immutable'); END;
CREATE TRIGGER session_events_no_delete BEFORE DELETE ON session_events
BEGIN SELECT RAISE(ABORT, 'session events are immutable'); END;

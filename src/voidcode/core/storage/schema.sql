-- Core schema v6. Identity lives in the SQLite file header itself
-- (PRAGMA application_id + user_version), checked on the single connection
-- before any write pragma; foreign files are never changed.
-- This build opens exactly version 6; anything else is refused.
-- This file is the whole schema: tables are added here together with the owner that uses them.

CREATE TABLE revisions (
    revision_id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
) STRICT;

-- Workspace-level capability pointer: the revision a restarted core restores.
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
    last_event_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
    -- Delegated child lineage: the chain root, the nesting depth, and an optional per-session
    -- tool visibility scope (NULL = the whole admitted catalog). A child can only narrow.
    parent_session_id TEXT REFERENCES sessions (session_id),
    root_session_id TEXT NOT NULL,
    depth INTEGER NOT NULL DEFAULT 0 CHECK (depth >= 0),
    tool_allowlist_json TEXT CHECK (tool_allowlist_json IS NULL OR json_valid(tool_allowlist_json))
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
    payload_bytes INTEGER NOT NULL,
    PRIMARY KEY (session_id, sequence)
) STRICT;
CREATE INDEX session_events_lookup_idx ON session_events (session_id, event_type, sequence);

-- Indexable answer keys: one row per answered ask, written atomically with the
-- ASK_ANSWERED event. The exactly-once answer guard reads here, never scans events.
CREATE TABLE ask_answers (
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, request_id)
) STRICT;

-- Indexable continuation starts: one row per started continuation, written atomically
-- with the CONTINUATION_STARTED event. Unstarted checks read here, never scan events.
CREATE TABLE continuation_starts (
    session_id TEXT NOT NULL,
    continuation_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, continuation_id)
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
    root_id TEXT,
    approval_sequence INTEGER,
    ask_sequence INTEGER,
    ask_request_id TEXT,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX checkpoints_session_info_idx ON checkpoints (session_id, run_id, approval_sequence, root_id);

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
    kind TEXT,
    run_id TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    idempotency_key TEXT,
    root_id TEXT,
    role TEXT NOT NULL DEFAULT 'system' CHECK (role IN ('system', 'user')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX continuations_claim_idx ON continuations (session_id, status, created_at);

-- Plugin-owned background actions: their own table, never mixed into continuations.
-- payload_json is the verbatim user action (write-once, never rewritten); the claim
-- identity, captured tool-wait authority, and settle error each have their own column.
CREATE TABLE background_actions (
    action_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    revision_id TEXT NOT NULL REFERENCES revisions (revision_id),
    idempotency_key TEXT,
    root_id TEXT NOT NULL,
    root_action_budget INTEGER,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    authority_json TEXT CHECK (authority_json IS NULL OR json_valid(authority_json)),
    claim_json TEXT CHECK (claim_json IS NULL OR json_valid(claim_json)),
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX background_actions_claim_idx ON background_actions (status, created_at);

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

-- Consumed root-action budget grants: one row per grant id, written atomically with
-- the budget extension it authorizes. Retries with the same id dedupe here instead
-- of scanning session_events for the RUN_RESUMED record.
CREATE TABLE root_grants (
    grant_id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

-- Durable, owner-scoped wakeups: a plugin schedules a future turn and the core claims it
-- atomically with the authentic run it creates. There is no TTL/reaper: a claimed row is
-- completed by the run's terminal path, and a crashed claim stays visible instead of replaying.
CREATE TABLE wakeups (
    wakeup_id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    workspace TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    root_id TEXT NOT NULL,
    root_action_budget INTEGER,
    role TEXT NOT NULL DEFAULT 'system' CHECK (role IN ('system', 'user')),
    due_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'completed', 'failed', 'cancelled')),
    claim_run_id TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX wakeups_due_idx ON wakeups (status, due_at);

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

-- Owner-owned restrictive overlays (plan mode and similar). Only tightening tiers exist, an
-- owner can revoke only its own row, and the core policy's frozen read_only is never touched
-- by them; they survive capability reloads because they are keyed by session, not revision.
CREATE TABLE session_overlays (
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    owner TEXT NOT NULL,
    tier TEXT NOT NULL CHECK (tier IN ('read-only')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_id, owner)
) STRICT;

-- At most one verified window projection per session: a plugin declares which committed
-- prefix its summary replaces, the core verifies the boundary and measures the accounting,
-- and assembly substitutes the replacement for that prefix. Events are never rewritten.
CREATE TABLE window_projections (
    session_id TEXT PRIMARY KEY REFERENCES sessions (session_id),
    owner TEXT NOT NULL,
    covered_from_sequence INTEGER NOT NULL CHECK (covered_from_sequence >= 1),
    covered_to_sequence INTEGER NOT NULL CHECK (covered_to_sequence >= covered_from_sequence),
    replacement TEXT NOT NULL,
    covered_bytes INTEGER NOT NULL CHECK (covered_bytes >= 0),
    dropped_bytes INTEGER NOT NULL CHECK (dropped_bytes >= 0),
    created_at INTEGER NOT NULL
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

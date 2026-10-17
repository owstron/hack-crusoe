-- Unified run store. One database for every run, simulated or live. Source of record; HTML reports are generated.
-- event_id is unique and monotonically ordered within a run, shared by actions and model_calls (runs.next_event_id).

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('sim', 'live')),
    schema_version INTEGER NOT NULL,
    playbook_version TEXT,
    config_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    due_at TEXT,
    max_total_spend REAL,
    campaign_ref TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',   -- runner state needed to resume (plan applied, next due checkpoint, ...)
    next_event_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    checkpoint_idx INTEGER NOT NULL,
    read_no INTEGER NOT NULL DEFAULT 1,
    period_start TEXT, period_end TEXT,
    observation_json TEXT NOT NULL,
    raw_json TEXT,                           -- raw platform response (live), for auditability
    provisional INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    PRIMARY KEY (run_id, checkpoint_idx, read_no)
);

CREATE TABLE IF NOT EXISTS actions (
    event_id INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    checkpoint_idx INTEGER NOT NULL,
    read_no INTEGER NOT NULL DEFAULT 1,
    action_json TEXT NOT NULL,               -- plans are stored with "_kind": "plan"; errors with "_kind": "error"
    result_ok INTEGER,
    error TEXT,
    ts TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS model_calls (
    event_id INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    checkpoint_idx INTEGER NOT NULL DEFAULT 0,
    read_no INTEGER NOT NULL DEFAULT 0,
    role TEXT, model TEXT, temperature REAL,
    prompt TEXT, raw_response TEXT, parsed_json TEXT, error TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    ts TEXT NOT NULL,
    PRIMARY KEY (run_id, event_id)
);

CREATE TABLE IF NOT EXISTS creatives (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    line_item TEXT NOT NULL,
    creative_id TEXT NOT NULL,
    content_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'live', 'retired')),
    meta_id TEXT,
    PRIMARY KEY (run_id, line_item, creative_id)
);

CREATE TABLE IF NOT EXISTS external_ids (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    kind TEXT NOT NULL CHECK (kind IN ('campaign', 'adset', 'ad')),
    line_item TEXT NOT NULL DEFAULT '',
    meta_id TEXT NOT NULL,
    PRIMARY KEY (run_id, kind, line_item, meta_id)
);

-- Simulator only. debrief_json holds hidden truth; read it only through store.truth.
CREATE TABLE IF NOT EXISTS scores (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    split TEXT NOT NULL,
    score REAL NOT NULL,
    debrief_json TEXT,
    PRIMARY KEY (run_id, split)
);

-- An improvement session is one `run_improvement.py --name X` execution.
CREATE TABLE IF NOT EXISTS sessions (
    name TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT,
    final_eval_json TEXT
);

CREATE TABLE IF NOT EXISTS playbooks (
    session TEXT NOT NULL,
    version TEXT NOT NULL,
    parent TEXT,
    text TEXT NOT NULL,
    diff TEXT,
    gate_result_json TEXT,
    research_urls TEXT,
    PRIMARY KEY (session, version)
);

CREATE TABLE IF NOT EXISTS improvement (
    session TEXT NOT NULL,
    seq INTEGER NOT NULL,                    -- log order within the session
    round INTEGER NOT NULL,
    base_version TEXT, candidate_version TEXT,
    stats_json TEXT NOT NULL,                -- the full change-log entry
    accepted INTEGER,
    PRIMARY KEY (session, seq)
);

-- Live backend bookkeeping (campaign/ad set ids, period boundaries, spend to date, ...). Separate from
-- runs.state_json, which belongs to the agent runner.
CREATE TABLE IF NOT EXISTS backend_state (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

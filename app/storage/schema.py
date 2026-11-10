"""SQLite schema for research projects, sources, concepts and aliases.

Deliberately small: four tables plus a lightweight thread-history table.
Migrations are additive (see db.py).
"""

SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel TEXT NOT NULL,
    city TEXT DEFAULT '',
    country TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    request TEXT NOT NULL,
    slack_channel TEXT DEFAULT '',
    slack_thread_ts TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    status TEXT DEFAULT 'running',   -- running | completed | failed
    hotel_slug TEXT DEFAULT '',
    research_state TEXT DEFAULT '{}',
    concept_state TEXT DEFAULT '{}',
    design_state TEXT DEFAULT '{}',
    artifact_state TEXT DEFAULT '{}',
    orchestration_state TEXT DEFAULT '{}',
    research_snapshot TEXT DEFAULT '',  -- JSON: verified observations
    property_dna TEXT DEFAULT '',       -- JSON: DNA elements
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT DEFAULT ''
);

-- Verified facts per hotel, reused across projects so a new thread does not
-- re-research a hotel from scratch. retrieved_at is when the fact was last
-- verified from its source, not when it was last reused.
CREATE TABLE IF NOT EXISTS hotel_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hotel_key TEXT NOT NULL,
    fact TEXT NOT NULL,
    fact_key TEXT NOT NULL,
    dimension TEXT DEFAULT '',
    confidence TEXT NOT NULL,
    evidence_level TEXT DEFAULT '',
    source_url TEXT NOT NULL,
    source_title TEXT DEFAULT '',
    source_type TEXT DEFAULT 'other',
    private INTEGER NOT NULL DEFAULT 0,
    project_id INTEGER,
    retrieved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hotel_facts_key ON hotel_facts(hotel_key, fact_key);
CREATE INDEX IF NOT EXISTS idx_hotel_facts_hotel ON hotel_facts(hotel_key, retrieved_at);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    url_normalized TEXT NOT NULL,
    title TEXT DEFAULT '',
    source_type TEXT NOT NULL DEFAULT 'other',
    -- official | brand | publication | destination | editorial_press | review_signal | other
    evidence TEXT DEFAULT '',           -- extracted facts, plain text
    confidence TEXT NOT NULL DEFAULT 'medium',  -- high | medium | low
    fetched_ok INTEGER NOT NULL DEFAULT 0,
    retrieved_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sources_project ON sources(project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_project_url ON sources(project_id, url_normalized);

CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    hotel TEXT NOT NULL,
    city TEXT DEFAULT '',
    country TEXT DEFAULT '',
    brand TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    name TEXT NOT NULL,
    proposition TEXT DEFAULT '',
    central_tension TEXT DEFAULT '',
    territory TEXT DEFAULT '',
    visual_language TEXT DEFAULT '',
    story_structure TEXT DEFAULT '',
    keywords TEXT DEFAULT '[]',         -- JSON array of strings
    ranking TEXT DEFAULT '',            -- strongest | alternative | experimental
    status TEXT DEFAULT 'generated',    -- generated | shortlisted | approved | published | rejected
    experience TEXT DEFAULT '',
    narrative_device TEXT DEFAULT '',
    visual_device TEXT DEFAULT '',
    source_project_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_concepts_hotel ON concepts(hotel);

CREATE TABLE IF NOT EXISTS aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL UNIQUE,
    canonical TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS thread_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL,           -- "channel:thread_ts"
    role TEXT NOT NULL,                 -- user | assistant
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_thread_messages_key ON thread_messages(thread_key);

CREATE TABLE IF NOT EXISTS thread_sessions (
    thread_key TEXT PRIMARY KEY,
    thread_ts TEXT NOT NULL,
    channel_id TEXT DEFAULT '',
    user_id TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'idle',
    current_property TEXT DEFAULT '',
    original_request TEXT DEFAULT '',
    current_goal TEXT DEFAULT '',
    pending_question_type TEXT DEFAULT '',
    pending_question_text TEXT DEFAULT '',
    conversation_summary TEXT DEFAULT '',
    research_project_id INTEGER,
    latest_concept_ids TEXT DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_thread_sessions_thread_ts ON thread_sessions(thread_ts);

CREATE TABLE IF NOT EXISTS slack_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    thread_key TEXT DEFAULT '',
    goal TEXT DEFAULT '',
    step_id TEXT DEFAULT '',
    specialist TEXT DEFAULT '',
    brief_summary TEXT DEFAULT '',
    result_status TEXT DEFAULT '',
    evaluation TEXT DEFAULT '{}',
    next_action TEXT DEFAULT '',
    response_mode TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_workflow_traces_project ON workflow_traces(project_id, id);
CREATE INDEX IF NOT EXISTS idx_workflow_traces_thread ON workflow_traces(thread_key, id);

CREATE TABLE IF NOT EXISTS design_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft', -- draft | approved | superseded
    design_spec TEXT NOT NULL DEFAULT '{}',
    feedback TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, version)
);
CREATE INDEX IF NOT EXISTS idx_design_versions_project ON design_versions(project_id, version);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    type TEXT NOT NULL DEFAULT 'html',
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated',
    path TEXT NOT NULL,
    source_design_version INTEGER NOT NULL DEFAULT 0,
    user_feedback TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, type, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, type, version);

-- Geocoding results for itinerary logistics (OpenStreetMap), so a place is looked up once.
CREATE TABLE IF NOT EXISTS geo_cache (
    query TEXT PRIMARY KEY,
    found INTEGER NOT NULL DEFAULT 0,
    name TEXT DEFAULT '',
    kind TEXT DEFAULT '',
    lat REAL,
    lon REAL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- House style the writer and critics follow, editable from Slack ("style rule: ...").
-- kind: rule (an instruction) | banned (a phrase the copy must not contain).
CREATE TABLE IF NOT EXISTS style_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'rule',
    active INTEGER NOT NULL DEFAULT 1,
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Who to ask: the hotel's contact per hotel_key, and the editor (hotel_key '').
CREATE TABLE IF NOT EXISTS contacts (
    hotel_key TEXT NOT NULL,
    role TEXT NOT NULL,   -- hotel | editor
    name TEXT NOT NULL,
    PRIMARY KEY (hotel_key, role)
);
"""

# House style seeded once (schema v7), from a client's review of the first drafts:
# readers should get recommendations, not citations.
DEFAULT_STYLE_RULES: tuple[tuple[str, str], ...] = (
    ("rule", "Never name a source in the copy (no publication, review site, blog, 'the hotel says' or "
             "'guests describe'): turn the claim into a direct recommendation. A guide listing or award "
             "that is itself the point, such as the Michelin Guide, may be named."),
    ("rule", "Every outside place the plan recommends gets one line on why it is worth the visit, from the "
             "evidence (a story, a masterpiece, a view, how it looks at night)."),
    ("rule", "Every restaurant names a dish from the evidence, or asks the hotel contact for one with an "
             "[ASK ...] note."),
    *(("banned", phrase) for phrase in (
        "one reviewer", "a reviewer", "reviewers say", "reviewers describe", "guests describe",
        "guests report", "guests say", "per the hotel", "the hotel says", "the hotel describes",
        "press describes", "according to", "Time Out",
    )),
)

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS demo_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    expires_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS companies (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    profile_status TEXT NOT NULL DEFAULT 'not_prepared',
    source_record_json TEXT,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS user_company_access (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_id, company_id)
);

CREATE TABLE IF NOT EXISTS company_profile_versions (
    profile_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    as_of_date TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    rules_version TEXT NOT NULL,
    status TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (company_id, input_hash, rules_version)
);

CREATE TABLE IF NOT EXISTS profile_views (
    profile_id TEXT NOT NULL REFERENCES company_profile_versions(profile_id)
        ON DELETE CASCADE,
    view_kind TEXT NOT NULL CHECK (
        view_kind IN ('MLView', 'InterpreterView', 'UIView')
    ),
    schema_version TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    view_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (profile_id, view_kind)
);

CREATE TABLE IF NOT EXISTS profile_observations (
    id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES company_profile_versions(profile_id)
        ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_state TEXT NOT NULL,
    value_json TEXT,
    evidence_id TEXT,
    UNIQUE (profile_id, field_name)
);

CREATE TABLE IF NOT EXISTS derived_metrics (
    id TEXT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES company_profile_versions(profile_id)
        ON DELETE CASCADE,
    name TEXT NOT NULL,
    value_json TEXT,
    coverage REAL,
    data_reliability TEXT NOT NULL,
    metric_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, id)
);

CREATE TABLE IF NOT EXISTS profile_evidence (
    id TEXT NOT NULL,
    profile_id TEXT NOT NULL REFERENCES company_profile_versions(profile_id)
        ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    value_json TEXT,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (profile_id, id)
);

CREATE TABLE IF NOT EXISTS news_sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    base_url TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    config_json TEXT,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS source_documents (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES news_sources(id),
    external_id TEXT,
    url TEXT,
    title TEXT NOT NULL,
    published_at TEXT,
    fetched_at TEXT,
    content_hash TEXT,
    content_text TEXT,
    extraction_status TEXT NOT NULL,
    metadata_json TEXT,
    synthetic INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (source_id, url, content_hash)
);

CREATE TABLE IF NOT EXISTS news_events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    event_type TEXT NOT NULL,
    content TEXT,
    published_at TEXT,
    occurred_at TEXT,
    effective_at TEXT,
    extraction_status TEXT NOT NULL,
    normalization_version TEXT NOT NULL,
    event_json TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 0 CHECK (synthetic IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id TEXT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    document_id TEXT NOT NULL REFERENCES source_documents(id) ON DELETE CASCADE,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    PRIMARY KEY (event_id, document_id)
);

CREATE TABLE IF NOT EXISTS event_facts (
    id TEXT NOT NULL,
    event_id TEXT NOT NULL REFERENCES news_events(id) ON DELETE CASCADE,
    fact_type TEXT NOT NULL,
    value_json TEXT NOT NULL,
    supporting_span TEXT,
    source_document_id TEXT REFERENCES source_documents(id),
    PRIMARY KEY (event_id, id)
);

CREATE TABLE IF NOT EXISTS bank_reports (
    id TEXT PRIMARY KEY,
    source_document_id TEXT NOT NULL UNIQUE
        REFERENCES source_documents(id) ON DELETE CASCADE,
    report_date TEXT,
    import_status TEXT NOT NULL,
    ocr_quality REAL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bank_report_pages (
    report_id TEXT NOT NULL REFERENCES bank_reports(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    image_ref TEXT,
    ocr_text TEXT,
    ocr_quality REAL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    PRIMARY KEY (report_id, page_number)
);

CREATE TABLE IF NOT EXISTS bank_report_fragments (
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL REFERENCES bank_reports(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    fragment_text TEXT NOT NULL,
    locator_json TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    FOREIGN KEY (report_id, page_number)
        REFERENCES bank_report_pages(report_id, page_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS news_snapshots (
    id TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (item_count >= 0),
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS news_snapshot_events (
    snapshot_id TEXT NOT NULL REFERENCES news_snapshots(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES news_events(id),
    PRIMARY KEY (snapshot_id, event_id)
);

CREATE TABLE IF NOT EXISTS overview_snapshots (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    profile_id TEXT REFERENCES company_profile_versions(profile_id),
    news_snapshot_id TEXT REFERENCES news_snapshots(id),
    analytic_focus TEXT NOT NULL CHECK (analytic_focus IN ('CFO', 'CEO')),
    context_id TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('ready', 'empty', 'not_prepared')),
    generated_at TEXT,
    coverage_note TEXT,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS event_candidates (
    overview_id TEXT NOT NULL REFERENCES overview_snapshots(id)
        ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES news_events(id),
    rank INTEGER NOT NULL CHECK (rank > 0),
    relevance TEXT NOT NULL CHECK (relevance IN ('grounded', 'conditional')),
    priority TEXT NOT NULL,
    candidate_json TEXT NOT NULL,
    insight_id TEXT REFERENCES insights(id),
    PRIMARY KEY (overview_id, event_id),
    UNIQUE (overview_id, rank)
);

CREATE TABLE IF NOT EXISTS insights (
    id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    event_id TEXT NOT NULL REFERENCES news_events(id),
    context_id TEXT NOT NULL,
    analytic_focus TEXT NOT NULL CHECK (analytic_focus IN ('CFO', 'CEO')),
    current_version INTEGER NOT NULL DEFAULT 0 CHECK (current_version >= 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (context_id, event_id)
);

CREATE TABLE IF NOT EXISTS insight_versions (
    insight_id TEXT NOT NULL REFERENCES insights(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version > 0),
    schema_version TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT,
    prompt_version TEXT,
    generated_at TEXT NOT NULL,
    PRIMARY KEY (insight_id, version)
);

CREATE TABLE IF NOT EXISTS claims (
    id TEXT NOT NULL,
    insight_id TEXT NOT NULL,
    insight_version INTEGER NOT NULL,
    field_path TEXT NOT NULL,
    claim_json TEXT NOT NULL,
    PRIMARY KEY (insight_id, insight_version, id),
    FOREIGN KEY (insight_id, insight_version)
        REFERENCES insight_versions(insight_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT NOT NULL,
    insight_id TEXT NOT NULL,
    insight_version INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (insight_id, insight_version, id),
    FOREIGN KEY (insight_id, insight_version)
        REFERENCES insight_versions(insight_id, version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    company_id TEXT NOT NULL REFERENCES companies(id),
    insight_id TEXT NOT NULL,
    insight_version INTEGER NOT NULL,
    analytic_focus TEXT NOT NULL CHECK (analytic_focus IN ('CFO', 'CEO')),
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (insight_id, insight_version)
        REFERENCES insight_versions(insight_id, version)
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id)
        ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    evidence_json TEXT,
    unknowns_json TEXT,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    company_id TEXT NOT NULL REFERENCES companies(id),
    insight_id TEXT NOT NULL,
    insight_version INTEGER NOT NULL,
    rating TEXT NOT NULL,
    reason TEXT,
    comment TEXT,
    state TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, insight_id, insight_version),
    FOREIGN KEY (insight_id, insight_version)
        REFERENCES insight_versions(insight_id, version)
);

CREATE TABLE IF NOT EXISTS feedback_history (
    id TEXT PRIMARY KEY,
    feedback_id TEXT NOT NULL REFERENCES feedback(id) ON DELETE CASCADE,
    rating TEXT NOT NULL,
    reason TEXT,
    comment TEXT,
    state TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    company_id TEXT REFERENCES companies(id),
    event_id TEXT REFERENCES news_events(id),
    insight_id TEXT REFERENCES insights(id),
    context_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    status TEXT NOT NULL,
    plan_json TEXT,
    versions_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS agent_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK (sequence_number >= 0),
    step_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    input_summary_json TEXT,
    output_summary_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (run_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id TEXT PRIMARY KEY,
    step_id TEXT NOT NULL REFERENCES agent_steps(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result_summary_json TEXT,
    elapsed_ms INTEGER,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS budget_reservations (
    id TEXT PRIMARY KEY,
    budget_date TEXT NOT NULL,
    user_id TEXT REFERENCES users(id),
    context_id TEXT,
    operation TEXT NOT NULL,
    max_cost_kopecks INTEGER NOT NULL CHECK (max_cost_kopecks >= 0),
    actual_cost_kopecks INTEGER CHECK (
        actual_cost_kopecks IS NULL OR actual_cost_kopecks >= 0
    ),
    status TEXT NOT NULL CHECK (
        status IN ('reserved', 'committed', 'released')
    ),
    reserved_at TEXT NOT NULL,
    finalized_at TEXT
);

CREATE TABLE IF NOT EXISTS usage_ledger (
    id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE
        REFERENCES budget_reservations(id),
    budget_date TEXT NOT NULL,
    user_id TEXT REFERENCES users(id),
    context_id TEXT,
    operation TEXT NOT NULL,
    model_id TEXT,
    tokens_in INTEGER CHECK (tokens_in IS NULL OR tokens_in >= 0),
    tokens_out INTEGER CHECK (tokens_out IS NULL OR tokens_out >= 0),
    cost_kopecks INTEGER NOT NULL CHECK (cost_kopecks >= 0),
    elapsed_ms INTEGER CHECK (elapsed_ms IS NULL OR elapsed_ms >= 0),
    cache_hit INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    source_ref TEXT,
    manifest_hash TEXT,
    stats_json TEXT,
    error_summary TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS import_errors (
    id TEXT PRIMARY KEY,
    import_id TEXT NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
    row_ref TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT
        (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS cache_entries (
    cache_key TEXT PRIMARY KEY,
    company_id TEXT NOT NULL REFERENCES companies(id),
    context_id TEXT NOT NULL,
    value_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON demo_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_access_company
    ON user_company_access(company_id, user_id);
CREATE INDEX IF NOT EXISTS idx_profiles_company_date
    ON company_profile_versions(company_id, as_of_date DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source_date
    ON source_documents(source_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_published
    ON news_events(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type ON news_events(event_type);
CREATE INDEX IF NOT EXISTS idx_overviews_company_focus
    ON overview_snapshots(company_id, analytic_focus, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_candidates_event
    ON event_candidates(event_id);
CREATE INDEX IF NOT EXISTS idx_insights_company
    ON insights(company_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_state
    ON feedback(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_context
    ON agent_runs(context_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_budget_day_status
    ON budget_reservations(budget_date, status);
CREATE INDEX IF NOT EXISTS idx_usage_day
    ON usage_ledger(budget_date, created_at);
CREATE INDEX IF NOT EXISTS idx_import_errors_import
    ON import_errors(import_id);

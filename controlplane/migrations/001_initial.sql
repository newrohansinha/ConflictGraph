CREATE TABLE tests (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    suite TEXT NOT NULL,
    framework TEXT NOT NULL,
    test_file TEXT NOT NULL,
    test_class TEXT NOT NULL DEFAULT '',
    test_function TEXT NOT NULL DEFAULT '',
    parameters TEXT NOT NULL DEFAULT '',
    source_revision TEXT NOT NULL,
    duration_ema DOUBLE PRECISION NOT NULL DEFAULT 1,
    duration_median DOUBLE PRECISION NOT NULL DEFAULT 1,
    failure_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
    execution_count BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE runs (
    id UUID PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED')),
    scheduler_policy TEXT NOT NULL,
    worker_count INTEGER NOT NULL CHECK (worker_count > 0),
    seed BIGINT NOT NULL,
    source_revision TEXT NOT NULL,
    trace_mode TEXT NOT NULL,
    trace_quality DOUBLE PRECISION,
    model_version TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    error TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE executions (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_id TEXT NOT NULL REFERENCES tests(id),
    worker_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ NOT NULL,
    duration_seconds DOUBLE PRECISION NOT NULL CHECK (duration_seconds >= 0),
    exit_code INTEGER NOT NULL,
    stdout TEXT NOT NULL,
    stderr TEXT NOT NULL,
    failure_message TEXT NOT NULL,
    timed_out BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX executions_run_started_idx ON executions(run_id, started_at);
CREATE INDEX executions_test_started_idx ON executions(test_id, started_at DESC);

CREATE TABLE schedules (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL UNIQUE REFERENCES runs(id) ON DELETE CASCADE,
    policy TEXT NOT NULL,
    worker_count INTEGER NOT NULL,
    expected_makespan DOUBLE PRECISION NOT NULL,
    expected_risk DOUBLE PRECISION NOT NULL,
    latency_ms DOUBLE PRECISION NOT NULL,
    seed BIGINT NOT NULL,
    plan JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE predictions (
    id BIGSERIAL PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    test_a_id TEXT NOT NULL REFERENCES tests(id),
    test_b_id TEXT NOT NULL REFERENCES tests(id),
    probability DOUBLE PRECISION NOT NULL CHECK (probability BETWEEN 0 AND 1),
    cause TEXT NOT NULL,
    model_version TEXT NOT NULL,
    shared_resources TEXT[] NOT NULL DEFAULT '{}',
    explanation TEXT NOT NULL,
    predicted_at TIMESTAMPTZ NOT NULL,
    UNIQUE(run_id, test_a_id, test_b_id),
    CHECK(test_a_id < test_b_id)
);

CREATE INDEX predictions_run_probability_idx ON predictions(run_id, probability DESC);


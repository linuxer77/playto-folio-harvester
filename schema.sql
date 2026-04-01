CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_name VARCHAR(255) NOT NULL,
    job_title VARCHAR(255) NOT NULL,
    candidate_name VARCHAR(255) NOT NULL,
    portfolio_url VARCHAR(1024) NOT NULL,
    status VARCHAR(20) NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed', 'failed')),
    drive_link VARCHAR(1024),
    error_logs TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_created_at_desc ON jobs (created_at DESC);

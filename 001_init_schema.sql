-- Initialisation du schéma PostgreSQL
-- Exécuté automatiquement par docker-compose

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id TEXT PRIMARY KEY,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    total_reviews INTEGER DEFAULT 0,
    total_duplicates INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    error_message TEXT,
    duration_seconds FLOAT,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    company VARCHAR(255) NOT NULL,
    source VARCHAR(50) NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    rating INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    sentiment VARCHAR(20),
    verified BOOLEAN,
    checksum VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE,
    collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS run_metrics (
    metric_id SERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    scraper_name VARCHAR(50) NOT NULL,
    inserted_count INTEGER DEFAULT 0,
    duplicate_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    duration_seconds FLOAT,
    status VARCHAR(20),
    error_message TEXT,
    recorded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Index pour performance
CREATE INDEX idx_reviews_company ON reviews(company);
CREATE INDEX idx_reviews_source ON reviews(source);
CREATE INDEX idx_reviews_sentiment ON reviews(sentiment);
CREATE INDEX idx_reviews_created_at ON reviews(created_at DESC);
CREATE INDEX idx_reviews_checksum ON reviews(checksum);
CREATE INDEX idx_run_id ON reviews(run_id);
CREATE INDEX idx_pipeline_runs_status ON pipeline_runs(status);
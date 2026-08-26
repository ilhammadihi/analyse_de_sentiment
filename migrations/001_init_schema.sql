-- Schéma PostgreSQL — source de vérité UNIQUE.
-- Exécuté :
--   1. automatiquement par docker-compose (monté dans /docker-entrypoint-initdb.d)
--   2. par l'application via `python -m reviews init-db` (Database.apply_schema)
-- Idempotent : IF NOT EXISTS partout.

-- ---------------------------------------------------------------------------
-- Runs du pipeline
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id           TEXT PRIMARY KEY,
    started_at       TIMESTAMP WITH TIME ZONE NOT NULL,
    ended_at         TIMESTAMP WITH TIME ZONE,
    status           VARCHAR(20) NOT NULL DEFAULT 'running',
    total_reviews    INTEGER DEFAULT 0,
    total_duplicates INTEGER DEFAULT 0,
    total_errors     INTEGER DEFAULT 0,
    error_message    TEXT,
    duration_seconds FLOAT,
    metadata         JSONB DEFAULT '{}',
    created_at       TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Avis collectés
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    review_id    TEXT PRIMARY KEY,
    run_id       TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    company      VARCHAR(255) NOT NULL,
    source       VARCHAR(50) NOT NULL,
    title        TEXT,
    text         TEXT NOT NULL,
    rating       INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    sentiment    VARCHAR(20),
    verified     BOOLEAN,
    checksum     VARCHAR(64) UNIQUE,          -- déduplication de contenu côté BD
    created_at   TIMESTAMP WITH TIME ZONE,
    collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Métriques par source et par run
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS run_metrics (
    metric_id        SERIAL PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
    scraper_name     VARCHAR(50) NOT NULL,
    inserted_count   INTEGER DEFAULT 0,
    duplicate_count  INTEGER DEFAULT 0,
    error_count      INTEGER DEFAULT 0,
    duration_seconds FLOAT,
    status           VARCHAR(20),
    error_message    TEXT,
    recorded_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Alertes (alerting temps réel + déclencheurs des futurs agents IA)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id     SERIAL PRIMARY KEY,
    run_id       TEXT REFERENCES pipeline_runs(run_id) ON DELETE SET NULL,
    type         VARCHAR(50) NOT NULL,
    severity     VARCHAR(20) NOT NULL,
    title        TEXT NOT NULL,
    message      TEXT NOT NULL,
    company      VARCHAR(255),
    source       VARCHAR(50),
    notified     JSONB DEFAULT '[]',          -- canaux notifiés (log/email/webhook)
    resolved_at  TIMESTAMP WITH TIME ZONE,
    created_at   TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ---------------------------------------------------------------------------
-- Index
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_reviews_company     ON reviews(company);
CREATE INDEX IF NOT EXISTS idx_reviews_source      ON reviews(source);
CREATE INDEX IF NOT EXISTS idx_reviews_sentiment   ON reviews(sentiment);
CREATE INDEX IF NOT EXISTS idx_reviews_created_at  ON reviews(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_collected   ON reviews(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_run_id      ON reviews(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_status         ON pipeline_runs(status);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at   ON alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_severity     ON alerts(severity);

-- ---------------------------------------------------------------------------
-- Vue d'agrégats : tendance de sentiment par entreprise/source/jour.
-- Interrogée par l'API/dashboard (rapide, pas de scan des lignes brutes côté
-- application).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW sentiment_daily AS
SELECT
    date_trunc('day', COALESCE(created_at, collected_at))::date AS day,
    company,
    source,
    COUNT(*)                                                    AS total,
    COUNT(*) FILTER (WHERE sentiment = 'positive')             AS positive,
    COUNT(*) FILTER (WHERE sentiment = 'neutral')              AS neutral,
    COUNT(*) FILTER (WHERE sentiment = 'negative')             AS negative,
    AVG(rating)                                                AS avg_rating
FROM reviews
GROUP BY 1, 2, 3;

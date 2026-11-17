-- Idempotent schema. Executed at API startup and by Postgres init.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS user_subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint TEXT UNIQUE NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    location GEOMETRY(Point, 4326) NOT NULL,
    min_severity_dbz INT DEFAULT 40,
    quiet_hours_start TIME NULL,
    quiet_hours_end TIME NULL,
    timezone TEXT NOT NULL DEFAULT 'Europe/Rome',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_location ON user_subscriptions USING GIST (location);

CREATE TABLE IF NOT EXISTS forecast_polygons (
    id SERIAL PRIMARY KEY,
    run_timestamp TIMESTAMPTZ NOT NULL,
    lead_time_minutes INT NOT NULL,
    severity_level VARCHAR(16) NOT NULL,
    max_dbz FLOAT NOT NULL,
    estimated_rain_mmh FLOAT NOT NULL,
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_geom ON forecast_polygons USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_forecast_run ON forecast_polygons (run_timestamp);

CREATE TABLE IF NOT EXISTS alert_history (
    id SERIAL PRIMARY KEY,
    subscription_id UUID REFERENCES user_subscriptions(id) ON DELETE CASCADE,
    severity_level VARCHAR(16) NOT NULL,
    source VARCHAR(8) NOT NULL DEFAULT 'RADAR',
    ref TEXT NULL,
    sent_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alert_dedup ON alert_history (subscription_id, sent_at);

-- Institutional CAP alerts (Protezione Civile / MeteoAlarm)
CREATE TABLE IF NOT EXISTS cap_alerts (
    identifier TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    severity VARCHAR(16) NOT NULL,
    headline TEXT,
    description TEXT,
    area_desc TEXT,
    onset TIMESTAMPTZ,
    expires TIMESTAMPTZ,
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_cap_geom ON cap_alerts USING GIST (geom);

-- Worker run log (health / observability)
CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_timestamp TIMESTAMPTZ PRIMARY KEY,
    source VARCHAR(16) NOT NULL,
    frames INT NOT NULL,
    polygons INT NOT NULL,
    notifications INT NOT NULL DEFAULT 0,
    duration_s FLOAT NOT NULL,
    finished_at TIMESTAMPTZ DEFAULT NOW()
);

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

-- v2: selectable event types
ALTER TABLE user_subscriptions ADD COLUMN IF NOT EXISTS alert_types TEXT[] NOT NULL
    DEFAULT '{HEAVY_RAIN,CLOUDBURST,HAIL,CAP_THUNDERSTORM,CAP_RAIN,CAP_WIND}';
ALTER TABLE forecast_polygons ADD COLUMN IF NOT EXISTS event_type VARCHAR(20) NOT NULL DEFAULT 'HEAVY_RAIN';
CREATE INDEX IF NOT EXISTS idx_forecast_run_event ON forecast_polygons (run_timestamp, event_type, lead_time_minutes);
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS event_type VARCHAR(20) NULL;
ALTER TABLE cap_alerts ADD COLUMN IF NOT EXISTS event_type VARCHAR(20) NULL;

-- v3: lightning (EUMETSAT MTG LI flashes)
CREATE TABLE IF NOT EXISTS lightning_flashes (
    id BIGSERIAL PRIMARY KEY,
    flash_time TIMESTAMPTZ NOT NULL,
    radiance FLOAT NULL,
    geom GEOMETRY(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lightning_geom ON lightning_flashes USING GIST (geom);
CREATE INDEX IF NOT EXISTS idx_lightning_time ON lightning_flashes (flash_time);
ALTER TABLE lightning_flashes ENABLE ROW LEVEL SECURITY;

-- v4: point forecasts at subscriber locations + verification
CREATE TABLE IF NOT EXISTS point_forecasts (
    run_timestamp TIMESTAMPTZ NOT NULL,
    subscription_id UUID NOT NULL REFERENCES user_subscriptions(id) ON DELETE CASCADE,
    event_type VARCHAR(20) NOT NULL,
    lead_time_minutes INT NOT NULL,
    prob REAL NOT NULL,
    dbz REAL NULL,
    PRIMARY KEY (run_timestamp, subscription_id, event_type, lead_time_minutes)
);
CREATE TABLE IF NOT EXISTS point_obs (
    run_timestamp TIMESTAMPTZ NOT NULL,
    subscription_id UUID NOT NULL REFERENCES user_subscriptions(id) ON DELETE CASCADE,
    dbz REAL NULL,          -- max observed reflectivity within ~3 km (NULL = outside coverage)
    snow BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (run_timestamp, subscription_id)
);
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS eta TIMESTAMPTZ NULL;
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS prob REAL NULL;
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS verified BOOLEAN NULL;
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ NULL;
ALTER TABLE alert_history ADD COLUMN IF NOT EXISTS observed_dbz REAL NULL;
ALTER TABLE point_forecasts ENABLE ROW LEVEL SECURITY;
ALTER TABLE forecast_polygons ADD COLUMN IF NOT EXISTS prob REAL NULL;
ALTER TABLE point_obs ENABLE ROW LEVEL SECURITY;

-- Supabase exposes the public schema through its Data API: RLS with no policies blocks anon/authenticated
-- roles entirely (the backend connects as table owner and is unaffected). Harmless on plain Postgres.
ALTER TABLE user_subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE forecast_polygons ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE cap_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE pipeline_runs ENABLE ROW LEVEL SECURITY;

-- v5: Europe / world coverage (regional nowcast domains, multi-provider CAP alerts)
ALTER TABLE forecast_polygons ADD COLUMN IF NOT EXISTS domain VARCHAR(24) NOT NULL DEFAULT 'IT';
CREATE INDEX IF NOT EXISTS idx_forecast_domain_run ON forecast_polygons (domain, run_timestamp);
ALTER TABLE pipeline_runs ADD COLUMN IF NOT EXISTS domain VARCHAR(24) NOT NULL DEFAULT 'IT';
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pipeline_runs_pkey'
             AND conrelid = 'pipeline_runs'::regclass AND array_length(conkey, 1) = 1) THEN
    ALTER TABLE pipeline_runs DROP CONSTRAINT pipeline_runs_pkey;
    ALTER TABLE pipeline_runs ADD CONSTRAINT pipeline_runs_pkey PRIMARY KEY (run_timestamp, domain);
  END IF;
END $$;
ALTER TABLE cap_alerts ADD COLUMN IF NOT EXISTS provider VARCHAR(32) NULL;
ALTER TABLE cap_alerts ADD COLUMN IF NOT EXISTS country VARCHAR(4) NULL;
-- warning areas referenced by geocode (MeteoAlarm EMMA_ID, NWS zones): loaded once, cached
CREATE TABLE IF NOT EXISTS cap_areas (
    code TEXT PRIMARY KEY,
    country VARCHAR(4) NULL,
    name TEXT NULL,
    geom GEOMETRY(MultiPolygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cap_areas_geom ON cap_areas USING GIST (geom);
ALTER TABLE cap_areas ENABLE ROW LEVEL SECURITY;
-- where the app is being looked at (no identity: rounded cell only) -> on-demand regional nowcasts
CREATE TABLE IF NOT EXISTS coverage_demand (
    cell VARCHAR(16) PRIMARY KEY,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE coverage_demand ENABLE ROW LEVEL SECURITY;

-- v6: continuous verification / calibration of push thresholds (forecast vs observation, whole grid)
CREATE TABLE IF NOT EXISTS calib_stats (
    day DATE NOT NULL,
    source VARCHAR(8) NOT NULL,
    event_type VARCHAR(20) NOT NULL,
    lead_time_minutes INT NOT NULL,
    bin INT NOT NULL,
    n BIGINT NOT NULL DEFAULT 0,
    hits BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, source, event_type, lead_time_minutes, bin)
);
ALTER TABLE calib_stats ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS alert_thresholds (
    source VARCHAR(8) NOT NULL,
    event_type VARCHAR(20) NOT NULL,
    lead_time_minutes INT NOT NULL,
    p REAL NULL,
    precision REAL NULL,
    n BIGINT NOT NULL,
    status VARCHAR(20) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source, event_type, lead_time_minutes)
);
ALTER TABLE alert_thresholds ENABLE ROW LEVEL SECURITY;

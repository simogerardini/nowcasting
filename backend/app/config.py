from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://noctalix:password@db:5432/nowcast"
    redis_url: str = "redis://redis:6379/0"

    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_claim_email: str = "mailto:admin@noctalix.com"

    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket_name: str = "noctalix-nowcast-assets"
    r2_public_url: str = "https://assets-nowcast.noctalix.com"

    # Fallback when R2 is not configured: assets written here and served by the API at /assets
    local_assets_dir: str = "/data/assets"
    radar_cache_dir: str = "/data/radar"

    cors_origins: str = "https://nowcast.noctalix.com"
    frontend_url: str = "https://nowcast.noctalix.com"

    # Pipeline
    radar_product: str = "VMI"
    dpc_api_base: str = "https://radar-api.protezionecivile.it"
    rainviewer_api: str = "https://api.rainviewer.com/public/weather-maps.json"
    cap_feed_url: str = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"
    n_input_frames: int = 6
    n_leadtimes: int = 12
    timestep_min: int = 5
    min_dbz: float = 15.0
    zr_a: float = 200.0
    zr_b: float = 1.6
    thresholds_dbz: str = "35,45,55"
    min_polygon_pixels: int = 4
    forecast_retention_hours: int = 2
    dedup_minutes: int = 45
    poll_seconds: int = 60
    cap_poll_seconds: int = 600
    push_concurrency: int = 32

    @property
    def thresholds(self) -> list[int]:
        return [int(x) for x in self.thresholds_dbz.split(",") if x.strip()]

    @property
    def r2_enabled(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key_id and self.r2_secret_access_key)

    @property
    def assets_base_url(self) -> str:
        return self.r2_public_url if self.r2_enabled else "/assets"


@lru_cache
def get_settings() -> Settings:
    return Settings()

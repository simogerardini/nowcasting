from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://noctalix:password@db:5432/nowcast"
    redis_url: str = ""          # empty = in-process store (single container, e.g. HF Spaces)
    db_pool_size: int = 3
    db_max_overflow: int = 2

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
    dpc_origin: str = "https://radar.protezionecivile.it"  # API requires an origin (docs, since 12/01/2026)
    use_poh: bool = True  # DPC Probability Of Hail product for hail alerts
    # EUMETSAT Data Store (MTG Lightning Imager); empty = lightning disabled
    eumetsat_consumer_key: str = ""
    eumetsat_consumer_secret: str = ""
    lightning_poll_seconds: int = 90
    lightning_radius_km: float = 15.0
    lightning_window_min: int = 20
    lightning_min_flashes: int = 2
    rainviewer_api: str = "https://api.rainviewer.com/public/weather-maps.json"
    cap_feed_url: str = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-italy"
    # Europe / world coverage: regional nowcast domains around subscribers and active viewers
    regional_enabled: bool = True
    opera_bucket_url: str = "https://object-store.os-api.cci1.ecmwf.int/openradar-24h-backup"
    opera_api: str = ("https://api.meteogate.eu/eu-eumetnet-weather-radar/collections/observations/"
                      "locations/0-20010-0-OPERA")
    max_regional_domains: int = 6
    domain_half_km: float = 200.0      # regional domain = square of 2 x this around a 1-degree cell
    demand_ttl_min: int = 45           # viewer demand keeps a region alive this long
    europe_map: bool = True            # publish the OPERA Europe observed composite for the map
    meteoalarm_api: str = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{slug}"
    nws_enabled: bool = True
    glm_enabled: bool = True
    n_input_frames: int = 6
    radar_downsample: int = 2      # block-average factor (1 = native ~1 km); 2 keeps small VPSs fast
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

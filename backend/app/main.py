import logging
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis.asyncio as aioredis
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .database import get_session, init_db
from .models import LocationIn, ResubscribeIn, SettingsIn, SubscribeIn, SubscriptionOut, UnsubscribeIn
from .vapid import ensure_vapid_keys

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    ensure_vapid_keys(settings)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    yield
    await app.state.redis.aclose()


app = FastAPI(title="Noctalix Nowcast API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86400,
)

if not settings.r2_enabled:
    Path(settings.local_assets_dir).mkdir(parents=True, exist_ok=True)
    app.mount("/assets", StaticFiles(directory=settings.local_assets_dir), name="assets")


def _validate_tz(tz: str | None) -> None:
    if tz is None:
        return
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, f"invalid timezone {tz!r}")


SUB_COLUMNS = """id, ST_Y(location) AS lat, ST_X(location) AS lon, min_severity_dbz,
                 quiet_hours_start, quiet_hours_end, timezone"""


@app.get("/api/v1/health")
async def health(session: AsyncSession = Depends(get_session)):
    out: dict = {"status": "ok"}
    try:
        await session.execute(text("SELECT 1"))
        out["database"] = "ok"
        row = (await session.execute(text(
            "SELECT run_timestamp, source, frames, polygons, notifications, duration_s, finished_at "
            "FROM pipeline_runs ORDER BY run_timestamp DESC LIMIT 1"))).mappings().first()
        out["last_run"] = dict(row) if row else None
        out["subscriptions"] = (await session.execute(text("SELECT count(*) FROM user_subscriptions"))).scalar()
    except Exception as exc:  # noqa: BLE001
        out["database"] = f"error: {exc.__class__.__name__}"
        out["status"] = "degraded"
    try:
        await app.state.redis.ping()
        out["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        out["redis"] = f"error: {exc.__class__.__name__}"
        out["status"] = "degraded"
    out["assets_url"] = settings.assets_base_url
    return out


@app.get("/api/v1/vapid-public-key")
async def vapid_public_key():
    return {"publicKey": settings.vapid_public_key}


@app.get("/api/v1/config")
async def public_config():
    return {"publicKey": settings.vapid_public_key, "assetsUrl": settings.assets_base_url}


@app.post("/api/v1/subscribe", response_model=SubscriptionOut)
async def subscribe(body: SubscribeIn, session: AsyncSession = Depends(get_session)):
    _validate_tz(body.timezone)
    row = (await session.execute(text(f"""
        INSERT INTO user_subscriptions (endpoint, p256dh, auth, location, min_severity_dbz,
                                        quiet_hours_start, quiet_hours_end, timezone, updated_at)
        VALUES (:endpoint, :p256dh, :auth, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
                COALESCE(:min_dbz, 40), :qs, :qe, COALESCE(:tz, 'Europe/Rome'), NOW())
        ON CONFLICT (endpoint) DO UPDATE SET
            p256dh = EXCLUDED.p256dh,
            auth = EXCLUDED.auth,
            location = EXCLUDED.location,
            min_severity_dbz = COALESCE(:min_dbz, user_subscriptions.min_severity_dbz),
            quiet_hours_start = EXCLUDED.quiet_hours_start,
            quiet_hours_end = EXCLUDED.quiet_hours_end,
            timezone = COALESCE(:tz, user_subscriptions.timezone),
            updated_at = NOW()
        RETURNING {SUB_COLUMNS}"""), {
        "endpoint": body.subscription.endpoint, "p256dh": body.subscription.keys.p256dh,
        "auth": body.subscription.keys.auth, "lat": body.lat, "lon": body.lon,
        "min_dbz": body.min_severity_dbz, "qs": body.quiet_hours_start, "qe": body.quiet_hours_end,
        "tz": body.timezone,
    })).mappings().one()
    await session.commit()
    return SubscriptionOut(**row)


@app.patch("/api/v1/location", response_model=SubscriptionOut)
async def update_location(body: LocationIn, session: AsyncSession = Depends(get_session)):
    row = (await session.execute(text(f"""
        UPDATE user_subscriptions SET location = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), updated_at = NOW()
        WHERE endpoint = :endpoint RETURNING {SUB_COLUMNS}"""),
        {"endpoint": body.endpoint, "lat": body.lat, "lon": body.lon})).mappings().first()
    if not row:
        raise HTTPException(404, "subscription not found")
    await session.commit()
    return SubscriptionOut(**row)


@app.patch("/api/v1/settings", response_model=SubscriptionOut)
async def update_settings(body: SettingsIn, session: AsyncSession = Depends(get_session)):
    _validate_tz(body.timezone)
    row = (await session.execute(text(f"""
        UPDATE user_subscriptions SET
            min_severity_dbz = COALESCE(:min_dbz, min_severity_dbz),
            quiet_hours_start = CASE WHEN :clear THEN NULL ELSE COALESCE(:qs, quiet_hours_start) END,
            quiet_hours_end   = CASE WHEN :clear THEN NULL ELSE COALESCE(:qe, quiet_hours_end) END,
            timezone = COALESCE(:tz, timezone),
            updated_at = NOW()
        WHERE endpoint = :endpoint RETURNING {SUB_COLUMNS}"""), {
        "endpoint": body.endpoint, "min_dbz": body.min_severity_dbz, "qs": body.quiet_hours_start,
        "qe": body.quiet_hours_end, "tz": body.timezone, "clear": body.clear_quiet_hours,
    })).mappings().first()
    if not row:
        raise HTTPException(404, "subscription not found")
    await session.commit()
    return SubscriptionOut(**row)


@app.patch("/api/v1/resubscribe", response_model=SubscriptionOut)
async def resubscribe(body: ResubscribeIn, session: AsyncSession = Depends(get_session)):
    """Called by the service worker on `pushsubscriptionchange`: rotate endpoint/keys in place."""
    row = (await session.execute(text(f"""
        UPDATE user_subscriptions SET endpoint = :new, p256dh = :p, auth = :a, updated_at = NOW()
        WHERE endpoint = :old RETURNING {SUB_COLUMNS}"""), {
        "new": body.subscription.endpoint, "p": body.subscription.keys.p256dh,
        "a": body.subscription.keys.auth, "old": body.old_endpoint,
    })).mappings().first()
    if not row:
        raise HTTPException(404, "subscription not found")
    await session.commit()
    return SubscriptionOut(**row)


@app.delete("/api/v1/unsubscribe", status_code=204)
async def unsubscribe(body: UnsubscribeIn, session: AsyncSession = Depends(get_session)):
    await session.execute(text("DELETE FROM user_subscriptions WHERE endpoint = :e"), {"e": body.endpoint})
    await session.commit()
    return Response(status_code=204)


@app.get("/api/v1/alerts/point")
async def alerts_at_point(lat: float, lon: float, session: AsyncSession = Depends(get_session)):
    """Forecast impacts + active CAP alerts at a point (used by the PWA for the focus view)."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise HTTPException(422, "invalid coordinates")
    params = {"lat": lat, "lon": lon}
    radar = (await session.execute(text("""
        SELECT f.lead_time_minutes, f.severity_level, f.max_dbz, f.estimated_rain_mmh, f.run_timestamp
        FROM forecast_polygons f
        WHERE f.run_timestamp = (SELECT max(run_timestamp) FROM forecast_polygons)
          AND ST_Intersects(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), f.geom)
        ORDER BY f.lead_time_minutes ASC, f.max_dbz DESC"""), params)).mappings().all()
    cap = (await session.execute(text("""
        SELECT identifier, event, severity, headline, area_desc, onset, expires FROM cap_alerts
        WHERE (expires IS NULL OR expires > NOW())
          AND ST_Intersects(ST_SetSRID(ST_MakePoint(:lon, :lat), 4326), geom)"""), params)).mappings().all()
    return {"radar": [dict(r) for r in radar], "cap": [dict(c) for c in cap]}

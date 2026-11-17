"""One full cycle: ingest -> nowcast -> vectorize -> PostGIS + R2 -> notify."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal

from . import cap, notifier
from .ingest_dpc import fetch_latest_frames
from .nowcast_engine import run_nowcast
from .storage import Storage
from . import points
from .probability import event_probabilities
from .vectorizer import publish, vectorize_probabilistic

log = logging.getLogger("pipeline")
settings = get_settings()

INSERT_POLY = text("""
INSERT INTO forecast_polygons (run_timestamp, lead_time_minutes, severity_level, event_type, max_dbz,
                               estimated_rain_mmh, prob, geom)
VALUES (:run, :lead, :sev, :ev, :max_dbz, :rain, :prob, ST_Multi(ST_GeomFromText(:wkt, 4326)))
""")


async def run_cycle(redis, storage: Storage) -> datetime | None:
    t0 = time.perf_counter()
    frames = await asyncio.to_thread(fetch_latest_frames)
    run_time = frames[-1].time
    if await redis.get("nowcast:last_run") == run_time.isoformat():
        log.info("Run %s already processed", run_time)
        return run_time
    result = await asyncio.to_thread(run_nowcast, frames)
    probs = await asyncio.to_thread(event_probabilities, result)
    polys = await asyncio.to_thread(vectorize_probabilistic, result, probs)

    async with SessionLocal() as session:
        await session.execute(text("DELETE FROM forecast_polygons WHERE run_timestamp = :r"), {"r": run_time})
        if polys:
            await session.execute(INSERT_POLY, [{
                "run": run_time, "lead": p.lead_time_minutes, "sev": p.severity_level, "ev": p.event_type, "prob": p.prob,
                "max_dbz": p.max_dbz, "rain": p.estimated_rain_mmh, "wkt": p.geom.wkt,
            } for p in polys])
        await session.execute(text("DELETE FROM forecast_polygons WHERE run_timestamp < :c"),
                              {"c": run_time - timedelta(hours=settings.forecast_retention_hours)})
        await session.commit()
        cap_geo = await cap.cap_geojson(session)

    await asyncio.to_thread(publish, result, polys, storage, cap_geo)

    async with SessionLocal() as session:
        await points.store_point_data(session, result, probs)
        await points.verify_alerts(session, run_time)
        sent = await notifier.notify_radar(session, redis, run_time)
        duration = time.perf_counter() - t0
        await session.execute(text("""
            INSERT INTO pipeline_runs (run_timestamp, source, frames, polygons, notifications, duration_s)
            VALUES (:r, :s, :f, :p, :n, :d)
            ON CONFLICT (run_timestamp) DO UPDATE SET polygons = EXCLUDED.polygons,
                notifications = EXCLUDED.notifications, duration_s = EXCLUDED.duration_s, finished_at = NOW()"""),
            {"r": run_time, "s": result.source, "f": len(frames), "p": len(polys), "n": sent, "d": duration})
        await session.execute(text("DELETE FROM pipeline_runs WHERE run_timestamp < NOW() - INTERVAL '7 days'"))
        await session.commit()

    await redis.set("nowcast:last_run", run_time.isoformat(), ex=86400)
    log.info("Cycle %s done in %.1fs: %d polygons, %d notifications", run_time, duration, len(polys), sent)
    return run_time


async def run_cap_cycle(redis) -> None:
    alerts = await asyncio.to_thread(cap.fetch_cap_alerts)
    async with SessionLocal() as session:
        await cap.store_cap_alerts(session, alerts)
        await notifier.notify_cap(session, redis)

"""Nowcast cycles.

* Italy ("IT" domain): DPC mosaic (RainViewer fallback) -> nowcast -> probabilities -> PostGIS + map
  assets (latest.json) -> point forecasts / verification -> push.
* Regional domains (Europe / world): the same chain on a ~400 km square around every place where the
  app is used outside the DPC coverage (subscribers + recent viewers), OPERA in Europe, RainViewer
  elsewhere; map assets in regions.json (+ the OPERA Europe observed composite).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal

from . import cap, notifier, points
from .ingest_dpc import ITALY_BBOX, fetch_latest_frames
from .nowcast_engine import run_nowcast
from .probability import event_probabilities
from .storage import CACHE_IMMUTABLE, Storage
from .vectorizer import MercatorWarper, png_bytes, publish, vectorize_probabilistic

log = logging.getLogger("pipeline")
settings = get_settings()

INSERT_POLY = text("""
INSERT INTO forecast_polygons (run_timestamp, lead_time_minutes, severity_level, event_type, max_dbz,
                               estimated_rain_mmh, prob, domain, geom)
VALUES (:run, :lead, :sev, :ev, :max_dbz, :rain, :prob, :domain, ST_Multi(ST_GeomFromText(:wkt, 4326)))
""")

# Italy coverage from the last IT run: subscribers/viewers outside it are served by regional domains
IT_STATE: dict = {"covered_ids": set(), "coverage": None, "ready": False}
REGIONS: dict = {"domains": {}, "europe": None}   # published regional map state (regions.json)


async def _store_polygons(session, domain: str, run_time: datetime, polys) -> None:
    await session.execute(text("DELETE FROM forecast_polygons WHERE run_timestamp = :r AND domain = :d"),
                          {"r": run_time, "d": domain})
    if polys:
        await session.execute(INSERT_POLY, [{
            "run": run_time, "lead": p.lead_time_minutes, "sev": p.severity_level, "ev": p.event_type,
            "prob": p.prob, "max_dbz": p.max_dbz, "rain": p.estimated_rain_mmh, "wkt": p.geom.wkt, "domain": domain,
        } for p in polys])
    await session.execute(text("DELETE FROM forecast_polygons WHERE run_timestamp < :c"),
                          {"c": run_time - timedelta(hours=settings.forecast_retention_hours)})
    await session.commit()


async def _finish(session, redis, domain: str, run_time: datetime, result, probs, polys, n_frames: int,
                  t0: float, sub_ids=None) -> tuple[int, list]:
    stored = await points.store_point_data(session, result, probs, sub_ids=sub_ids)
    await points.verify_alerts(session, run_time)
    sent = await notifier.notify_radar(session, redis, run_time, sub_ids=stored)
    duration = time.perf_counter() - t0
    await session.execute(text("""
        INSERT INTO pipeline_runs (run_timestamp, domain, source, frames, polygons, notifications, duration_s)
        VALUES (:r, :dom, :s, :f, :p, :n, :d)
        ON CONFLICT (run_timestamp, domain) DO UPDATE SET polygons = EXCLUDED.polygons,
            notifications = EXCLUDED.notifications, duration_s = EXCLUDED.duration_s, finished_at = NOW()"""),
        {"r": run_time, "dom": domain, "s": result.source, "f": n_frames, "p": len(polys), "n": sent, "d": duration})
    await session.execute(text("DELETE FROM pipeline_runs WHERE run_timestamp < NOW() - INTERVAL '7 days'"))
    await session.commit()
    return sent, stored


# ------------------------------------------------------------------ Italy (DPC)
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
        await _store_polygons(session, "IT", run_time, polys)
        cap_geo = await cap.cap_geojson(session)

    await asyncio.to_thread(publish, result, polys, storage, cap_geo)

    async with SessionLocal() as session:
        sent, stored = await _finish(session, redis, "IT", run_time, result, probs, polys, len(frames), t0)
    IT_STATE.update(covered_ids=set(stored), ready=True,
                    coverage=(~np.isnan(result.observed[-1].dbz), result.transform, result.crs))
    await redis.set("nowcast:last_run", run_time.isoformat(), ex=86400)
    log.info("Cycle IT %s done in %.1fs: %d polygons, %d notifications", run_time,
             time.perf_counter() - t0, len(polys), sent)
    return run_time


def in_it_coverage(lon: float, lat: float) -> bool:
    cov = IT_STATE.get("coverage")
    if cov is None:
        b = ITALY_BBOX
        return b[0] <= lon <= b[2] and b[1] <= lat <= b[3]
    mask, tr, crs = cov
    from pyproj import Transformer
    x, y = Transformer.from_crs("EPSG:4326", crs, always_xy=True).transform(lon, lat)
    col, row = ~tr * (x, y)
    r, c = int(row), int(col)
    return 0 <= r < mask.shape[0] and 0 <= c < mask.shape[1] and bool(mask[r, c])


# ------------------------------------------------------------------ regional domains
async def wanted_domains() -> list:
    """Regional domains to compute: 1-degree cells holding subscribers outside the Italy coverage
    (weight 10) or recent viewers (weight 1); at most MAX_REGIONAL_DOMAINS, busiest first."""
    from .ingest_regional import cell_of, make_domain

    async with SessionLocal() as s:
        subs = (await s.execute(text(
            "SELECT id, ST_X(location) AS lon, ST_Y(location) AS lat FROM user_subscriptions"))).mappings().all()
        demand = (await s.execute(text(
            "SELECT lon, lat FROM coverage_demand WHERE last_seen > NOW() - make_interval(mins => CAST(:m AS INT))"),
            {"m": settings.demand_ttl_min})).mappings().all()
        await s.execute(text("DELETE FROM coverage_demand WHERE last_seen < NOW() - INTERVAL '1 day'"))
        await s.commit()
    cells: dict = {}
    for r in subs:
        if r["id"] in IT_STATE["covered_ids"] or in_it_coverage(r["lon"], r["lat"]):
            continue
        d = cells.setdefault(cell_of(r["lon"], r["lat"]), make_domain(*cell_of(r["lon"], r["lat"])))
        d.sub_ids.append(r["id"])
    for r in demand:
        if in_it_coverage(r["lon"], r["lat"]):
            continue
        d = cells.setdefault(cell_of(r["lon"], r["lat"]), make_domain(*cell_of(r["lon"], r["lat"])))
        d.viewers += 1
    ranked = sorted(cells.values(), key=lambda d: -(10 * len(d.sub_ids) + d.viewers))
    return ranked[: settings.max_regional_domains]


def _publish_domain(result, d, storage: Storage) -> dict:
    """Forecast frames of a regional domain (observed frames: Europe / RainViewer map layers)."""
    run = result.run_time.strftime("%Y%m%d%H%M")
    warper = MercatorWarper(result.forecast_dbz.shape[1:], result.transform, result.crs)
    frames = []
    for i, lead in enumerate(result.lead_minutes):
        url = storage.put(f"frames/{run}/{d.id}/fc_{lead:02d}.png", png_bytes(warper.warp(result.forecast_dbz[i])),
                          "image/png", CACHE_IMMUTABLE)
        frames.append({"time": (result.run_time + timedelta(minutes=lead)).isoformat(), "offset_min": lead,
                       "kind": "forecast", "url": url})
    return {"id": d.id, "source": result.source, "run_timestamp": result.run_time.isoformat(),
            "bbox": [round(v, 3) for v in d.bbox], "coordinates": warper.coordinates, "frames": frames}


def write_regions(storage: Storage) -> None:
    body = {"generated": datetime.now(timezone.utc).isoformat(),
            "domains": list(REGIONS["domains"].values()), "europe": REGIONS["europe"]}
    storage.put("regions.json", json.dumps(body, separators=(",", ":")).encode(), "application/json",
                "public, max-age=60")


async def run_domain(redis, storage: Storage, c, d, opera, latest: datetime | None) -> bool:
    """One regional cycle; False if nothing new."""
    from .ingest_regional import fetch_domain_frames

    key = f"nowcast:last_run:{d.id}"
    if latest is not None and await redis.get(key) == latest.isoformat():
        return False
    t0 = time.perf_counter()
    frames = await asyncio.to_thread(fetch_domain_frames, c, d, opera)
    run_time = frames[-1].time
    if await redis.get(key) == run_time.isoformat():
        return False
    result = await asyncio.to_thread(run_nowcast, frames)
    probs = await asyncio.to_thread(event_probabilities, result)
    polys = await asyncio.to_thread(vectorize_probabilistic, result, probs)
    async with SessionLocal() as session:
        await _store_polygons(session, d.id, run_time, polys)
    REGIONS["domains"][d.id] = await asyncio.to_thread(_publish_domain, result, d, storage)
    async with SessionLocal() as session:
        sent, _ = await _finish(session, redis, d.id, run_time, result, probs, polys, len(frames), t0,
                                sub_ids=d.sub_ids)
    await redis.set(key, run_time.isoformat(), ex=86400)
    log.info("Cycle %s (%s) %s done in %.1fs: %d polygons, %d notifications", d.id, result.source, run_time,
             time.perf_counter() - t0, len(polys), sent)
    return True


_EU_MASK: dict = {}


def _dpc_mask_on(f) -> np.ndarray | None:
    """Pixels of an OPERA overview frame covered by the DPC mosaic (hidden there: Italy layer on top)."""
    cov = IT_STATE.get("coverage")
    if cov is None:
        return None
    mask, tr, crs = cov
    key = (f.dbz.shape, tuple(f.transform)[:6], mask.shape, tuple(tr)[:6])
    if key not in _EU_MASK:
        from pyproj import Transformer
        h, w = f.dbz.shape
        cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
        xs, ys = f.transform * (cols, rows)
        X, Y = Transformer.from_crs(f.crs, crs, always_xy=True).transform(xs, ys)
        c, r = ~tr * (np.asarray(X), np.asarray(Y))
        r, c = np.floor(r).astype(int), np.floor(c).astype(int)
        ok = (r >= 0) & (r < mask.shape[0]) & (c >= 0) & (c < mask.shape[1])
        out = np.zeros((h, w), dtype=bool)
        out[ok] = mask[r[ok], c[ok]]
        _EU_MASK.clear()
        _EU_MASK[key] = out
    return _EU_MASK[key]


def publish_europe(storage: Storage, opera) -> bool:
    """OPERA Europe observed composite (4 km overview) for the map: last 6 frames, 5 min apart."""
    from .ingest_regional import europe_overview

    times = opera.last_times(settings.n_input_frames)
    have = {f["time"] for f in (REGIONS["europe"] or {}).get("frames", [])}
    if REGIONS["europe"] and times[-1].isoformat() in have:
        return False
    frames, coords = [], None
    old = {f["time"]: f for f in (REGIONS["europe"] or {}).get("frames", [])}
    for t in times:
        if t.isoformat() in old:
            frames.append(old[t.isoformat()])
            continue
        f = europe_overview(opera.path(t), t)
        warper = MercatorWarper(f.dbz.shape, f.transform, f.crs)
        coords = warper.coordinates
        dbz = np.where(np.isfinite(f.dbz) & (f.dbz >= settings.min_dbz), f.dbz, -np.inf)
        it = _dpc_mask_on(f)
        if it is not None:
            dbz[it] = -np.inf
        url = storage.put(f"frames/{t:%Y%m%d%H%M}/europe.png", png_bytes(warper.warp(dbz)), "image/png",
                          CACHE_IMMUTABLE)
        frames.append({"time": t.isoformat(), "url": url})
    REGIONS["europe"] = {"source": "OPERA", "coordinates": coords or REGIONS["europe"]["coordinates"],
                         "frames": frames}
    opera.prune(keep=settings.n_input_frames + 4)
    return True


async def run_regional(redis, storage: Storage) -> int:
    """Europe map layer + every wanted regional domain. Returns the number of domains computed."""
    from .ingest_regional import OperaClient, http_client

    if not settings.regional_enabled or not IT_STATE["ready"]:
        return 0
    domains = await wanted_domains()
    n = 0
    with http_client() as c:
        opera = OperaClient(c)
        changed = False
        if settings.europe_map:
            try:
                changed = await asyncio.to_thread(publish_europe, storage, opera)
            except Exception as exc:  # noqa: BLE001
                log.warning("Europe map layer failed: %s", exc)
        opera_latest = rv_latest = None
        try:
            opera_latest = await asyncio.to_thread(opera.latest_time)
        except Exception as exc:  # noqa: BLE001
            log.info("OPERA probe failed: %s", exc)
        try:
            m = (await asyncio.to_thread(c.get, settings.rainviewer_api)).json()
            rv_latest = datetime.fromtimestamp(int(m["radar"]["past"][-1]["time"]), tz=timezone.utc)
        except Exception as exc:  # noqa: BLE001
            log.info("RainViewer probe failed: %s", exc)
        for d in domains:
            prev = REGIONS["domains"].get(d.id)
            d.source = prev["source"] if prev and prev["source"] in ("OPERA", "RAINVIEWER") else None
            latest = opera_latest if d.source == "OPERA" else rv_latest if d.source == "RAINVIEWER" else None
            try:
                if await run_domain(redis, storage, c, d, opera, latest):
                    n += 1
                    changed = True
            except Exception as exc:  # noqa: BLE001
                log.warning("Regional domain %s failed: %s", d.id, exc)
    wanted = {d.id for d in domains}
    for k in [k for k in REGIONS["domains"] if k not in wanted]:
        del REGIONS["domains"][k]
        changed = True
    # domains whose newest forecast is older than the retention are dropped from the map
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.forecast_retention_hours)
    for k in [k for k, v in REGIONS["domains"].items() if datetime.fromisoformat(v["run_timestamp"]) < cutoff]:
        del REGIONS["domains"][k]
        changed = True
    if changed:
        await asyncio.to_thread(write_regions, storage)
    from . import lightning
    lightning.EXTRA_BBOXES[:] = [d.bbox for d in domains]
    return n


async def run_cap_cycle(redis) -> None:
    async with SessionLocal() as session:
        await cap.ensure_areas(session)
        points_ = await cap.demand_points(session)
    alerts = await asyncio.to_thread(cap.fetch_cap_alerts, points_)
    async with SessionLocal() as session:
        await cap.resolve_nws_zones(session, alerts)
        await cap.store_cap_alerts(session, alerts)
        await notifier.notify_cap(session, redis)

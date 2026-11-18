"""Worker entrypoint: polls radar releases every POLL_SECONDS and runs a cycle on each new scan.

Run:  python -m worker.scheduler           (daemon)
      python -m worker.scheduler --once    (single cycle, e.g. for cron / debugging)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.database import init_db
from app.kv import get_kv
from app.vapid import ensure_vapid_keys

from .ingest_dpc import latest_available_time
from .pipeline import run_cap_cycle, run_cycle, run_regional
from .storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")
settings = get_settings()

LOCK_KEY = "nowcast:lock"
CPU_LOCK = asyncio.Lock()          # radar cycles (Italy + regional) never overlap
STATE = {"it_waiting": False}
STATUS_FILE = Path(settings.radar_cache_dir).parent / "worker_status.json"


def write_status(**kw) -> None:
    try:
        data = json.loads(STATUS_FILE.read_text()) if STATUS_FILE.exists() else {}
        data.update(kw)
        STATUS_FILE.write_text(json.dumps(data, default=str))
    except OSError:
        pass


async def radar_tick(redis, storage: Storage, force: bool = False) -> None:
    latest = await asyncio.to_thread(latest_available_time)
    last = await redis.get("nowcast:last_run")
    if not force and latest is not None and last == latest.isoformat():
        return
    # One radar cycle at a time (small VPS). The Italy cycle has priority: it WAITS for a running regional
    # cycle (instead of skipping, which starved it when both ticks fired together) and regional ticks
    # skip while it is waiting.
    STATE["it_waiting"] = True
    try:
        await asyncio.wait_for(CPU_LOCK.acquire(), timeout=280)
    except asyncio.TimeoutError:
        log.warning("Radar lock busy for 280 s - skip")
        return
    finally:
        STATE["it_waiting"] = False
    write_status(last_attempt=datetime.now(timezone.utc).isoformat())
    try:
        run = await asyncio.wait_for(run_cycle(redis, storage), timeout=600)
        from . import ingest_dpc
        write_status(last_ok=datetime.now(timezone.utc).isoformat(), last_run=run, last_error=None,
                     dpc_error=ingest_dpc.LAST_DPC_ERROR)
    except Exception as exc:  # noqa: BLE001
        log.exception("Radar cycle failed")
        write_status(last_error=f"{type(exc).__name__}: {exc}"[:500],
                     last_error_at=datetime.now(timezone.utc).isoformat())
    finally:
        CPU_LOCK.release()


async def lightning_tick(redis, storage: Storage) -> None:
    from app.database import SessionLocal

    from . import lightning, notifier
    try:
        flashes = await asyncio.to_thread(lightning.fetch_new_flashes) if lightning.enabled() else []
        try:
            flashes += await asyncio.to_thread(lightning.fetch_glm_flashes)
        except Exception as exc:  # noqa: BLE001
            log.warning("GLM lightning failed: %s", exc)
        async with SessionLocal() as session:
            await lightning.store_flashes(session, flashes)
            geo = await lightning.lightning_geojson(session)
            await asyncio.to_thread(storage.put, "lightning.geojson", json.dumps(geo, separators=(",", ":")).encode(),
                                    "application/geo+json", "public, max-age=60", True)
            sent = await notifier.notify_lightning(session, redis)
        lightning.save_status(last_ok=datetime.now(timezone.utc).isoformat(), new_flashes=len(flashes),
                              products_total=lightning.STATS["products"],
                              flashes_full_disc_total=lightning.STATS["flashes_full_disc"],
                              last_product=lightning.STATS["last_product"],
                              flashes_60min=len(geo["features"]), notifications=sent, last_error=None,
                              glm_files=lightning.STATS.get("glm_files", 0),
                              mtg_enabled=lightning.enabled(), regions=len(lightning.EXTRA_BBOXES),
                              download_errors=lightning.STATS.get("last_download_errors"))
    except Exception as exc:  # noqa: BLE001
        log.exception("Lightning cycle failed")
        lightning.save_status(last_error=f"{type(exc).__name__}: {exc}"[:400],
                              last_error_at=datetime.now(timezone.utc).isoformat())


async def regional_tick(redis, storage: Storage) -> None:
    """Europe map layer + regional nowcasts (Europe: OPERA, world: RainViewer). Shares the radar lock:
    never runs concurrently with the Italy cycle (small VPS)."""
    if STATE["it_waiting"] or CPU_LOCK.locked():
        return
    await CPU_LOCK.acquire()
    try:
        from . import ingest_regional, pipeline
        n = await asyncio.wait_for(run_regional(redis, storage), timeout=600)
        write_status(regions=sorted(pipeline.REGIONS["domains"]), regions_computed=n,
                     europe_frames=len((pipeline.REGIONS["europe"] or {}).get("frames", [])),
                     opera_error=ingest_regional.LAST_OPERA_ERROR, regional_error=None)
    except Exception as exc:  # noqa: BLE001
        log.exception("Regional cycle failed")
        write_status(regional_error=f"{type(exc).__name__}: {exc}"[:400])
    finally:
        CPU_LOCK.release()


async def cap_tick(redis) -> None:
    try:
        await run_cap_cycle(redis)
    except Exception:  # noqa: BLE001
        log.exception("CAP cycle failed")


async def main(once: bool) -> None:
    await init_db()
    ensure_vapid_keys(settings)
    redis = get_kv()
    storage = Storage()
    if once:
        await cap_tick(redis)
        await radar_tick(redis, storage, force=True)
        return
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(radar_tick, "interval", seconds=settings.poll_seconds, args=[redis, storage],
                  max_instances=1, coalesce=True)
    sched.add_job(cap_tick, "interval", seconds=settings.cap_poll_seconds, args=[redis],
                  max_instances=1, coalesce=True)
    from . import lightning
    sched.add_job(lightning_tick, "interval", seconds=settings.lightning_poll_seconds,
                  args=[redis, storage], max_instances=1, coalesce=True)
    if lightning.enabled():
        log.info("Lightning (EUMETSAT MTG LI) enabled")
    else:
        lightning.save_status(enabled=False, note="set EUMETSAT_CONSUMER_KEY / EUMETSAT_CONSUMER_SECRET")
    if settings.regional_enabled:
        sched.add_job(regional_tick, "interval", seconds=settings.poll_seconds, args=[redis, storage],
                      max_instances=1, coalesce=True)
    sched.start()
    await cap_tick(redis)
    await radar_tick(redis, storage)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info("Worker started (poll %ss)", settings.poll_seconds)
    await stop.wait()
    sched.shutdown(wait=False)
    await redis.aclose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    asyncio.run(main(ap.parse_args().once))

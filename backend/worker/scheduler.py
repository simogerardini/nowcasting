"""Worker entrypoint: polls radar releases every POLL_SECONDS and runs a cycle on each new scan.

Run:  python -m worker.scheduler           (daemon)
      python -m worker.scheduler --once    (single cycle, e.g. for cron / debugging)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.database import init_db
from app.kv import get_kv
from app.vapid import ensure_vapid_keys

from .ingest_dpc import latest_available_time
from .pipeline import run_cap_cycle, run_cycle
from .storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("scheduler")
settings = get_settings()

LOCK_KEY = "nowcast:lock"


async def radar_tick(redis, storage: Storage, force: bool = False) -> None:
    latest = await asyncio.to_thread(latest_available_time)
    last = await redis.get("nowcast:last_run")
    if not force and latest is not None and last == latest.isoformat():
        return
    # Distributed lock: never overlap cycles (TTL covers a hung run)
    if not await redis.set(LOCK_KEY, "1", ex=240, nx=True):
        log.info("Previous cycle still running - skip")
        return
    try:
        await run_cycle(redis, storage)
    except Exception:  # noqa: BLE001
        log.exception("Radar cycle failed")
    finally:
        await redis.delete(LOCK_KEY)


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

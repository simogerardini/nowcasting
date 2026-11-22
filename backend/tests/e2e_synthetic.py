"""End-to-end check against a live PostGIS/Redis (docker compose run --rm worker python -m tests.e2e_synthetic).

Injects synthetic radar frames (storm cell moving toward a test subscriber), runs a full cycle with
a stubbed push sender, and verifies polygons, alert_history, dedup and 410 cleanup.
"""
import asyncio
import uuid

from sqlalchemy import text

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.kv import get_kv
from tests.test_pipeline import synthetic_frames
from worker import notifier, pipeline, probability

# tests must not depend on the calibration produced by the latest backtest
probability.CALIBRATION_FILE = probability.CALIBRATION_FILE.with_name("__none__.json")
probability.load_calibration.cache_clear()
from worker.storage import Storage

settings = get_settings()


async def main():
    await init_db()
    r = get_kv()
    await r.delete("nowcast:last_run")
    frames = synthetic_frames(shape=(300, 400))
    pipeline.fetch_latest_frames = lambda: frames  # inject
    ep_ok = f"https://push.example/{uuid.uuid4()}"
    ep_gone = f"https://push.example/{uuid.uuid4()}"
    async with SessionLocal() as s:
        for ep in (ep_ok, ep_gone):  # storm centre ~ (10.2E, 44.5N) after 30 min
            await s.execute(text("""INSERT INTO user_subscriptions (endpoint,p256dh,auth,location,alert_types)
                VALUES (:e,'pkey-xxxxxxxxxxxx','auth-xxxx',ST_SetSRID(ST_MakePoint(10.25,44.5),4326),
                        '{HEAVY_RAIN,CLOUDBURST,HAIL,RAIN_START}')"""), {"e": ep})
        await s.commit()
    sent_to = []
    notifier._send_sync = lambda n: (sent_to.append((n.endpoint, n.payload["title"])), 410 if n.endpoint == ep_gone else 201)[1]

    run = await pipeline.run_cycle(r, Storage())
    async with SessionLocal() as s:
        npoly = (await s.execute(text("SELECT count(*) FROM forecast_polygons WHERE run_timestamp=:r"), {"r": run})).scalar()
        hist = (await s.execute(text("""SELECT severity_level FROM alert_history ah JOIN user_subscriptions u
                 ON u.id=ah.subscription_id WHERE u.endpoint=:e"""), {"e": ep_ok})).scalars().all()
        gone = (await s.execute(text("SELECT count(*) FROM user_subscriptions WHERE endpoint=:e"), {"e": ep_gone})).scalar()
    print("polygons:", npoly, "| sent:", sent_to, "| history:", hist, "| gone-left:", gone)
    assert npoly > 0 and hist and gone == 0

    # second run, same severity -> dedup
    sent_to.clear()
    await r.delete("nowcast:last_run")
    async with SessionLocal() as s:
        n2 = await notifier.notify_radar(s, r, run)
    print("dedup second pass sent:", n2)
    assert n2 == 0
    # --- onset alerts: forecast (dry now, rain coming) and observed (dry previous run, raining now)
    from datetime import timedelta
    await r.flushdb()
    t1 = frames[-1].time + timedelta(minutes=10)
    run1 = synthetic_frames(shape=(300, 400), x0=85, t0=t1 - timedelta(minutes=25))
    run2 = synthetic_frames(shape=(300, 400), x0=100, t0=t1 + timedelta(minutes=10) - timedelta(minutes=25))
    async with SessionLocal() as s:
        await s.execute(text("""INSERT INTO user_subscriptions (endpoint,p256dh,auth,location,alert_types)
            VALUES ('https://push.example/onset-fc','pkey-xxxxxxxxxxxx','auth-xxxx',
                    ST_SetSRID(ST_MakePoint(10.43,44.5),4326),'{RAIN_START}'),
                   ('https://push.example/onset-obs','pkey-xxxxxxxxxxxx','auth-xxxx',
                    ST_SetSRID(ST_MakePoint(10.47,44.5),4326),'{RAIN_START}')"""))
        await s.commit()
    sent_to.clear()
    pipeline.fetch_latest_frames = lambda: run1
    await pipeline.run_cycle(r, Storage())
    fc_titles = [t for e, t in sent_to if e.endswith("onset-fc")]
    sent_to.clear()
    pipeline.fetch_latest_frames = lambda: run2
    await r.delete("nowcast:last_run")
    await pipeline.run_cycle(r, Storage())
    print("onset forecast (run1):", fc_titles, "| run2:", [t for e, t in sent_to])
    async with SessionLocal() as s:
        per_user = dict((await s.execute(text("""SELECT u.endpoint, count(*) FROM alert_history ah
            JOIN user_subscriptions u ON u.id = ah.subscription_id
            WHERE u.endpoint LIKE 'https://push.example/onset-%' GROUP BY 1"""))).all())
        ver = (await s.execute(text("SELECT count(*) FROM alert_history WHERE eta IS NOT NULL AND prob IS NOT NULL"))).scalar()
    print("rain-onset alerts per user over both runs:", per_user, "| alerts with ETA+prob:", ver)
    # each user warned exactly once (forecast or observed onset), never twice
    assert per_user.get("https://push.example/onset-fc") == 1 and per_user.get("https://push.example/onset-obs") == 1

    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM user_subscriptions WHERE endpoint LIKE 'https://push.example/%'"))
        await s.execute(text("DELETE FROM forecast_polygons"))
        await s.execute(text("DELETE FROM pipeline_runs"))
        await s.commit()
    await r.flushdb()
    print("E2E OK")


if __name__ == "__main__":
    asyncio.run(main())

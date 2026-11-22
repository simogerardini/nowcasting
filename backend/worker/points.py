"""Point forecasts at subscriber locations (what alerts are based on) and point observations
(what alerts and probabilities are verified against)."""
from __future__ import annotations

import logging

import numpy as np
from pyproj import Transformer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("points")


def _pixel(result, lon: float, lat: float) -> tuple[int, int] | None:
    tr = Transformer.from_crs("EPSG:4326", result.crs, always_xy=True)
    x, y = tr.transform(lon, lat)
    col, row = ~result.transform * (x, y)
    r, c = int(row), int(col)
    h, w = result.forecast_dbz.shape[1:]
    return (r, c) if 0 <= r < h and 0 <= c < w else None


def _win(a: np.ndarray, r: int, c: int, k: int = 1) -> np.ndarray:
    return a[max(r - k, 0):r + k + 1, max(c - k, 0):c + k + 1]


async def store_point_data(session: AsyncSession, result, probs: dict[str, np.ndarray],
                           sub_ids: list | None = None) -> list:
    """Point forecasts + observation for the subscribers inside this run's radar coverage (optionally
    restricted to `sub_ids`). Returns the ids stored (= the subscribers this domain is responsible for)."""
    q = "SELECT id, ST_X(location) AS lon, ST_Y(location) AS lat FROM user_subscriptions"
    params: dict = {}
    if sub_ids is not None:
        if not sub_ids:
            return []
        q += " WHERE id = ANY(:ids)"
        params["ids"] = list(sub_ids)
    subs = (await session.execute(text(q), params)).mappings().all()
    run = result.run_time
    fc_rows, obs_rows, stored = [], [], []
    obs_snow = result.observed[-1].snow if result.observed else None
    for s in subs:
        px = _pixel(result, s["lon"], s["lat"])
        if px is None:
            continue
        r, c = px
        ow = _win(np.nan_to_num(result.observed_dbz, nan=np.nan, neginf=-99.0), r, c)
        if not np.isfinite(ow).any():
            continue  # outside radar coverage: another domain (or none) serves this subscriber
        stored.append(s["id"])
        obs_dbz = float(np.nanmax(ow))
        snow = bool(obs_snow is not None and float(_win(obs_snow, r, c).max()) > 0.5)
        obs_rows.append({"run": run, "sid": s["id"], "dbz": obs_dbz, "snow": snow})
        for ev, p in probs.items():
            for i, lead in enumerate(result.lead_minutes):
                pv = float(p[i, r, c])
                if pv < 0.02:
                    continue
                dw = _win(result.forecast_dbz[i], r, c)
                dv = dw[np.isfinite(dw)]
                fc_rows.append({"run": run, "sid": s["id"], "ev": ev, "lead": lead, "p": round(pv, 4),
                                "dbz": float(np.median(dv)) if dv.size else None})
    if obs_rows:
        await session.execute(text("""
            INSERT INTO point_obs (run_timestamp, subscription_id, dbz, snow) VALUES (:run, :sid, :dbz, :snow)
            ON CONFLICT (run_timestamp, subscription_id) DO UPDATE SET dbz = EXCLUDED.dbz, snow = EXCLUDED.snow"""),
            obs_rows)
    if fc_rows:
        await session.execute(text("""
            INSERT INTO point_forecasts (run_timestamp, subscription_id, event_type, lead_time_minutes, prob, dbz)
            VALUES (:run, :sid, :ev, :lead, :p, :dbz)
            ON CONFLICT (run_timestamp, subscription_id, event_type, lead_time_minutes)
            DO UPDATE SET prob = EXCLUDED.prob, dbz = EXCLUDED.dbz"""), fc_rows)
    await session.execute(text("DELETE FROM point_forecasts WHERE run_timestamp < CAST(:run AS TIMESTAMPTZ) - INTERVAL '30 days'"),
                          {"run": run})
    await session.execute(text("DELETE FROM point_obs WHERE run_timestamp < CAST(:run AS TIMESTAMPTZ) - INTERVAL '30 days'"), {"run": run})
    await session.commit()
    return stored


VERIFY_SQL = text("""
UPDATE alert_history ah SET
    observed_dbz = v.mx,
    verified = COALESCE(v.mx, -99) >= (CASE ah.event_type
                    WHEN 'HAIL' THEN 55 WHEN 'CLOUDBURST' THEN 45 WHEN 'HEAVY_RAIN' THEN 35 ELSE 15 END),
    verified_at = NOW()
FROM (
    SELECT a.id, max(o.dbz) AS mx, count(o.*) AS nobs
    FROM alert_history a
    LEFT JOIN point_obs o ON o.subscription_id = a.subscription_id
         AND o.run_timestamp BETWEEN a.eta - INTERVAL '10 minutes' AND a.eta + INTERVAL '15 minutes'
    WHERE a.source = 'RADAR' AND a.verified IS NULL AND a.eta IS NOT NULL
      AND a.eta + INTERVAL '15 minutes' <= :latest
    GROUP BY a.id
) v
WHERE ah.id = v.id AND v.nobs > 0
""")


async def verify_alerts(session: AsyncSession, latest_obs_time) -> None:
    await session.execute(VERIFY_SQL, {"latest": latest_obs_time})
    await session.commit()


STATS_SQL = text("""
SELECT event_type, count(*) AS alerts,
       count(*) FILTER (WHERE verified) AS hits,
       count(*) FILTER (WHERE verified = false) AS false_alarms,
       count(*) FILTER (WHERE verified IS NULL) AS pending
FROM alert_history WHERE source = 'RADAR' AND eta IS NOT NULL  -- alerts issued by the calibrated model
  AND sent_at > NOW() - make_interval(days => CAST(:days AS INT))
GROUP BY event_type ORDER BY event_type
""")

RELIABILITY_SQL = text("""
SELECT pf.event_type, width_bucket(pf.prob, 0, 1.0001, 10) AS bin, count(*) AS n,
       avg(pf.prob) AS mean_p,
       avg(CASE WHEN o.dbz >= (CASE pf.event_type WHEN 'HAIL' THEN 55 WHEN 'CLOUDBURST' THEN 45
                                 WHEN 'HEAVY_RAIN' THEN 35 ELSE 15 END) THEN 1.0 ELSE 0.0 END) AS observed
FROM point_forecasts pf
JOIN point_obs o ON o.subscription_id = pf.subscription_id
 AND o.run_timestamp = pf.run_timestamp + make_interval(mins => pf.lead_time_minutes)
WHERE pf.run_timestamp > NOW() - make_interval(days => CAST(:days AS INT))
GROUP BY 1, 2 ORDER BY 1, 2
""")


async def verification_stats(session: AsyncSession, days: int = 30) -> dict:
    alerts = [dict(r) for r in (await session.execute(STATS_SQL, {"days": days})).mappings().all()]
    for a in alerts:
        done = a["hits"] + a["false_alarms"]
        a["precision"] = round(a["hits"] / done, 3) if done else None
    rel = [{k: (round(float(v), 4) if isinstance(v, float) else v) for k, v in dict(r).items()}
           for r in (await session.execute(RELIABILITY_SQL, {"days": days})).mappings().all()]
    thr = [dict(r) for r in (await session.execute(text(
        "SELECT source, event_type, lead_time_minutes, p, precision, n, status, updated_at FROM alert_thresholds "
        "ORDER BY source, event_type, lead_time_minutes"))).mappings().all()]
    return {"days": days, "alerts": alerts, "point_reliability": rel, "push_thresholds": thr}

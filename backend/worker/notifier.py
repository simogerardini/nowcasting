"""Spatial matching (PostGIS) + Web Push delivery (VAPID) with dedup, quiet hours and endpoint cleanup."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
from pywebpush import WebPushException, webpush
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events import CAP_EVENTS, LIGHTNING_EVENTS, RADAR_EVENTS, SEVERITY_RANK, sql_rank

from .nowcast_engine import dbz_to_rain

log = logging.getLogger("notifier")
settings = get_settings()

CAP_SEVERITY_LEVEL = {"Minor": "MODERATE", "Moderate": "MODERATE", "Severe": "SEVERE", "Extreme": "EXTREME"}
RANK = sql_rank("f.severity_level")

def _thresholds_sql() -> str:
    """VALUES table of per-event alert rules (calibrated probability threshold, max lead, severity)."""
    from .probability import alert_threshold
    rows = []
    for ev, spec in RADAR_EVENTS.items():
        rank = SEVERITY_RANK[spec["severity"]]
        rows.append(f"('{ev}', {alert_threshold(ev):.3f}, {int(spec['max_lead'])}, {rank}, '{spec['severity']}', "
                    f"{int(spec['dedup_min'])})")
    return "(VALUES " + ", ".join(rows) + ") AS t(event_type, p, max_lead, rnk, severity, dedup_min)"


def match_sql():
    """Point-based, probabilistic matching (replaces polygon intersection):
    - forecast alert: calibrated P(event at the user's pixel) >= event threshold at some lead <= max lead
    - rain/snow onset: forecast only if dry at the user now; observed onset if wet now and dry in the previous run
    - one notification per user per run (most severe, then most imminent, then most probable)
    - dedup: same event within its window, and never an equal/less severe radar alert within 45 min."""
    return text(f"""
WITH t AS (SELECT * FROM {_thresholds_sql()}),
cur AS (SELECT * FROM point_obs WHERE run_timestamp = :current_run AND subscription_id = ANY(:ids)),
prev AS (SELECT DISTINCT ON (subscription_id) * FROM point_obs
         WHERE run_timestamp < :current_run AND run_timestamp >= CAST(:current_run AS TIMESTAMPTZ) - INTERVAL '25 minutes'
           AND subscription_id = ANY(:ids)
         ORDER BY subscription_id, run_timestamp DESC),
cand AS (
    SELECT pf.subscription_id, pf.event_type, pf.lead_time_minutes, pf.prob, pf.dbz
    FROM point_forecasts pf JOIN t ON t.event_type = pf.event_type
    WHERE pf.run_timestamp = :current_run AND pf.subscription_id = ANY(:ids)
      AND pf.lead_time_minutes BETWEEN 5 AND t.max_lead AND pf.prob >= t.p
    UNION ALL
    SELECT c.subscription_id, CASE WHEN c.snow THEN 'SNOW' ELSE 'RAIN_START' END, 0, 1.0, c.dbz
    FROM cur c JOIN prev p ON p.subscription_id = c.subscription_id
    WHERE c.dbz >= 15 AND COALESCE(p.dbz, -99) < 15
)
SELECT DISTINCT ON (u.id)
    u.id AS user_id, u.endpoint, u.p256dh, u.auth, u.quiet_hours_start, u.quiet_hours_end, u.timezone,
    cand.lead_time_minutes, cand.event_type, cand.prob, cand.dbz AS point_dbz, t.severity AS severity_level,
    (SELECT count(*) FROM lightning_flashes l
      WHERE l.flash_time > NOW() - INTERVAL '15 minutes'
        AND ST_DWithin(l.geom, u.location, 0.4)
        AND ST_DWithin(l.geom::geography, u.location::geography, 30000)) AS flashes_near,
    -- official warning at the user's point for the same hazard: quoted as corroboration only
    (SELECT c.severity || '|' || COALESCE(c.event_type, '') || '|' || COALESCE(c.provider, 'MeteoAlarm')
       FROM cap_alerts c
      WHERE ST_Intersects(u.location, c.geom) AND (c.expires IS NULL OR c.expires > NOW())
        AND (c.onset IS NULL OR c.onset <= NOW() + INTERVAL '1 hour')
        AND c.severity IN ('Moderate', 'Severe', 'Extreme')
        AND c.event_type = ANY(CASE cand.event_type
              WHEN 'HAIL' THEN ARRAY['CAP_THUNDERSTORM']
              WHEN 'CLOUDBURST' THEN ARRAY['CAP_THUNDERSTORM', 'CAP_RAIN']
              WHEN 'HEAVY_RAIN' THEN ARRAY['CAP_RAIN', 'CAP_THUNDERSTORM']
              WHEN 'SNOW' THEN ARRAY['CAP_SNOW_ICE']
              ELSE ARRAY[]::text[] END)
      ORDER BY (CASE c.severity WHEN 'Extreme' THEN 3 WHEN 'Severe' THEN 2 ELSE 1 END) DESC
      LIMIT 1) AS cap_context
FROM cand
JOIN user_subscriptions u ON u.id = cand.subscription_id
JOIN t ON t.event_type = cand.event_type
LEFT JOIN cur ON cur.subscription_id = u.id
WHERE cand.event_type = ANY(u.alert_types)
  AND (cand.event_type NOT IN ('RAIN_START', 'SNOW') OR cand.lead_time_minutes = 0 OR COALESCE(cur.dbz, -99) < 15)
  AND NOT EXISTS (
      SELECT 1 FROM alert_history ah
      WHERE ah.subscription_id = u.id AND ah.source = 'RADAR'
        AND (
          (ah.event_type = cand.event_type AND ah.sent_at > NOW() - make_interval(mins => t.dedup_min))
          OR (ah.sent_at > NOW() - make_interval(mins => CAST(:dedup AS INT))
              AND {sql_rank("ah.severity_level")} >= t.rnk)
        )
  )
ORDER BY u.id, t.rnk DESC, cand.lead_time_minutes ASC, cand.prob DESC
""")


CAP_RANK = "(CASE c.severity WHEN 'Extreme' THEN 3 WHEN 'Severe' THEN 2 ELSE 1 END)"
# one warning per user and hazard (national services often issue several overlapping warnings), never
# again the same warning, and no repeat of a hazard at equal/lower level within 12 h
CAP_MATCH_SQL = text(f"""
SELECT DISTINCT ON (u.id, c.event_type)
       u.id AS user_id, u.endpoint, u.p256dh, u.auth,
       u.quiet_hours_start, u.quiet_hours_end, u.timezone,
       c.identifier, c.event, c.event_type, c.severity, c.headline, c.area_desc, c.onset, c.expires, c.provider,
       (SELECT a.name FROM cap_areas a WHERE ST_Intersects(a.geom, u.location)
         ORDER BY ST_Area(a.geom) LIMIT 1) AS user_area
FROM cap_alerts c
JOIN user_subscriptions u ON ST_Intersects(u.location, c.geom)
WHERE (c.expires IS NULL OR c.expires > NOW())
  AND c.severity IN ('Moderate', 'Severe', 'Extreme')
  AND c.event_type = ANY(u.alert_types)
  AND NOT EXISTS (SELECT 1 FROM alert_history ah
                  WHERE ah.subscription_id = u.id AND ah.source = 'CAP'
                    AND (ah.ref = c.identifier
                         OR (ah.event_type = c.event_type AND ah.sent_at > NOW() - INTERVAL '12 hours'
                             AND {sql_rank("ah.severity_level")} >= {CAP_RANK})))
ORDER BY u.id, c.event_type, {CAP_RANK} DESC, c.onset ASC NULLS LAST
""")


@dataclass
class Notification:
    user_id: object
    endpoint: str
    p256dh: str
    auth: str
    severity: str
    payload: dict
    source: str = "RADAR"
    ref: str | None = None
    event_type: str | None = None
    eta: datetime | None = None
    prob: float | None = None


# ------------------------------------------------------------------ rules
def in_quiet_hours(start: time | None, end: time | None, tz: str | None, now: datetime | None = None) -> bool:
    if not start or not end:
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(tz or "Europe/Rome"))
    t = now.time().replace(tzinfo=None)
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # window across midnight, e.g. 23:00-07:00


def should_send(severity: str, quiet: bool) -> bool:
    """Quiet hours silence INFO/MODERATE (rain onset, heavy rain, snow); SEVERE/EXTREME always go through."""
    return not (quiet and severity in ("INFO", "MODERATE"))


def build_radar_payload(row, run_time: datetime) -> dict:
    lead = int(row["lead_time_minutes"])
    ev = row["event_type"]
    spec = RADAR_EVENTS[ev]
    sev = row["severity_level"]
    data = {"url": f"{settings.frontend_url}/?focus=alert", "severity": sev, "event": ev, "lead": lead}
    if lead == 0:
        title = {"RAIN_START": "🌧️ Inizia a piovere", "SNOW": "❄️ Inizia a nevicare"}.get(ev, spec["title"])
        body = {"RAIN_START": "Il radar rileva pioggia sulla tua posizione adesso.",
                "SNOW": "Il radar rileva neve sulla tua posizione adesso."}.get(ev, "") + " Tocca per aprire il radar."
        return {"title": title, "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
                "tag": f"nowcast-{ev}", "renotify": False, "data": data}
    eta = (run_time + timedelta(minutes=lead)).astimezone(ZoneInfo(row["timezone"] or "Europe/Rome"))
    pct = int(round(100 * float(row["prob"]) / 5.0) * 5)
    # intensity: forecast at the user's pixel, never below the event threshold the alert is about
    dbz = max(float(row["point_dbz"] if row["point_dbz"] is not None else spec["dbz"]), spec["dbz"])
    rate = float(dbz_to_rain(np.array(dbz)))
    thr_rate = float(dbz_to_rain(np.array(spec["dbz"])))
    what = {
        "RAIN_START": "Inizio pioggia",
        "HEAVY_RAIN": f"Pioggia forte (≥ {thr_rate:.0f} mm/h, stimati ~{rate:.0f} mm/h)",
        "CLOUDBURST": f"Nubifragio / temporale forte (≥ {thr_rate:.0f} mm/h, stimati ~{rate:.0f} mm/h)",
        "HAIL": "Cella molto intensa: possibile grandine e raffiche",
        "SNOW": "Inizio nevicata",
    }[ev]
    body = f"{what} sulla tua posizione verso le {eta:%H:%M}. Probabilità {pct}%."
    if row.get("flashes_near") and ev in ("HEAVY_RAIN", "CLOUDBURST", "HAIL"):
        body += f" ⚡ Attività elettrica in zona ({row['flashes_near']} fulmini entro 30 km)."
    if row.get("cap_context"):
        cap_sev, cap_ev, provider = (row["cap_context"].split("|") + ["", ""])[:3]
        colour = {"Moderate": "gialla", "Severe": "arancione", "Extreme": "rossa"}.get(cap_sev, "gialla")
        what = CAP_EVENTS.get(cap_ev, {}).get("label", "Allerta meteo").removeprefix("Allerta ").strip()
        body += f" 📢 Coerente con l'allerta {colour} {what} ({provider})."
    body += " Tocca per aprire il radar."
    data["prob"] = round(float(row["prob"]), 3)
    return {
        "title": f"{spec['title']} tra ~{lead} min", "body": body, "icon": "/icons/storm-alert.png",
        "badge": "/icons/badge.png", "tag": f"nowcast-{ev}", "renotify": SEVERITY_RANK[sev] >= 2,
        "requireInteraction": sev == "EXTREME", "data": data,
    }


def build_cap_payload(row) -> dict:
    sev = CAP_SEVERITY_LEVEL.get(row["severity"], "MODERATE")
    label = CAP_EVENTS.get(row["event_type"] or "", {}).get("label", "Allerta meteo")
    what = label.removeprefix("Allerta ").strip()
    colour = {"MODERATE": "gialla", "SEVERE": "arancione", "EXTREME": "rossa"}[sev]
    icon = {"MODERATE": "🟡", "SEVERE": "🟠", "EXTREME": "🔴"}[sev]
    area = row.get("user_area") or row["area_desc"] or ""
    title = f"{icon} Allerta {colour} {what}" + (f" – {area}" if area else "")
    until = ""
    if row["expires"]:
        until = f"Valida fino al {row['expires'].astimezone(ZoneInfo(row['timezone'] or 'Europe/Rome')):%d/%m alle %H:%M} (ora locale). "
    body = f"{until}Fonte: {row.get('provider') or 'MeteoAlarm'}. Tocca per i dettagli."
    return {
        "title": title[:120], "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
        "tag": f"cap-{row['identifier'][-24:]}", "renotify": sev != "MODERATE",
        "data": {"url": f"{settings.frontend_url}/?focus=cap", "severity": sev, "event": row["event_type"]},
    }


# ------------------------------------------------------------------ delivery
def _send_sync(n: Notification) -> int:
    """Returns HTTP status (201/200 ok, 404/410 gone, other = transient)."""
    try:
        resp = webpush(
            subscription_info={"endpoint": n.endpoint, "keys": {"p256dh": n.p256dh, "auth": n.auth}},
            data=json.dumps(n.payload, ensure_ascii=False),
            vapid_private_key=settings.vapid_private_key,
            vapid_claims={"sub": settings.vapid_claim_email},
            ttl=1800 if n.source == "RADAR" else 21600,
            headers={"Urgency": "high" if n.severity != "MODERATE" else "normal"},
            timeout=10,
        )
        return resp.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 0
    except Exception as exc:  # noqa: BLE001
        log.warning("push error %s: %s", n.endpoint[:60], exc)
        return 0


async def deliver(session: AsyncSession, redis, batch: list[Notification]) -> int:
    sem = asyncio.Semaphore(settings.push_concurrency)
    sent, gone = 0, []

    async def one(n: Notification):
        nonlocal sent
        # Redis TTL guard against concurrent/duplicate runs
        key = f"alert:{n.source}:{n.user_id}:{n.ref or n.event_type or n.severity}"
        ttl = settings.dedup_minutes * 60 if n.source in ("RADAR", "LIGHT") else 7 * 86400
        if not await redis.set(key, "1", ex=ttl, nx=True):
            return
        async with sem:
            status = await asyncio.to_thread(_send_sync, n)
        if status in (200, 201, 202):
            sent += 1
            await session.execute(text(
                "INSERT INTO alert_history (subscription_id, severity_level, source, ref, event_type, eta, prob) "
                "VALUES (:u, :s, :src, :r, :e, :eta, :p)"),
                {"u": n.user_id, "s": n.severity, "src": n.source, "r": n.ref, "e": n.event_type,
                 "eta": n.eta, "p": n.prob})
        else:
            await redis.delete(key)
            if status in (404, 410):
                gone.append(n.endpoint)
            else:
                log.warning("push failed status=%s endpoint=%s", status, n.endpoint[:60])

    await asyncio.gather(*(one(n) for n in batch))
    if gone:
        await session.execute(text("DELETE FROM user_subscriptions WHERE endpoint = ANY(:e)"), {"e": gone})
        log.info("Removed %d expired push endpoints", len(gone))
    await session.commit()
    return sent


async def notify_radar(session: AsyncSession, redis, run_time: datetime, sub_ids: list | None = None) -> int:
    """Alerts for the subscribers of one domain run (sub_ids = those whose point data this run stored)."""
    if sub_ids is None:
        sub_ids = (await session.execute(text("SELECT subscription_id FROM point_obs WHERE run_timestamp = :r"),
                                         {"r": run_time})).scalars().all()
    if not sub_ids:
        return 0
    rows = (await session.execute(match_sql(), {"current_run": run_time, "ids": list(sub_ids),
                                                "dedup": settings.dedup_minutes})).mappings().all()
    batch = []
    for r in rows:
        quiet = in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])
        if not should_send(r["severity_level"], quiet):
            continue
        n = Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], r["severity_level"],
                         build_radar_payload(r, run_time), event_type=r["event_type"])
        n.eta = run_time + timedelta(minutes=int(r["lead_time_minutes"]))
        n.prob = float(r["prob"])
        batch.append(n)
    sent = await deliver(session, redis, batch) if batch else 0
    log.info("Radar alerts: %d matched, %d queued, %d sent", len(rows), len(batch), sent)
    return sent


async def notify_cap(session: AsyncSession, redis) -> int:
    """Standalone pushes for official warnings are OFF by default (CAP_PUSH=false): only events detected or
    forecast by our own radar model at the user's point are notified; warnings are quoted inside them."""
    if not settings.cap_push:
        return 0
    return await _notify_cap(session, redis)


async def _notify_cap(session: AsyncSession, redis) -> int:
    rows = (await session.execute(CAP_MATCH_SQL)).mappings().all()
    batch = []
    for r in rows:
        sev = CAP_SEVERITY_LEVEL.get(r["severity"], "MODERATE")
        if not should_send(sev, in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])):
            continue
        batch.append(Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], sev,
                                  build_cap_payload(r), source="CAP", ref=r["identifier"], event_type=r["event_type"]))
    sent = await deliver(session, redis, batch) if batch else 0
    log.info("CAP alerts: %d matched, %d sent", len(rows), sent)
    return sent


# ------------------------------------------------------------------ lightning (EUMETSAT MTG LI)
LIGHTNING_SQL = text("""
SELECT u.id AS user_id, u.endpoint, u.p256dh, u.auth, u.quiet_hours_start, u.quiet_hours_end, u.timezone,
       count(*) AS n,
       min(ST_Distance(u.location::geography, l.geom::geography)) AS dmin_m,
       avg(ST_Distance(u.location::geography, l.geom::geography))
         FILTER (WHERE l.flash_time > NOW() - make_interval(mins => CAST(:half AS INT))) AS d_recent,
       avg(ST_Distance(u.location::geography, l.geom::geography))
         FILTER (WHERE l.flash_time <= NOW() - make_interval(mins => CAST(:half AS INT))) AS d_older,
       max(l.flash_time) AS last_flash
FROM user_subscriptions u
JOIN lightning_flashes l
  ON ST_DWithin(l.geom, u.location, 0.5)
 AND ST_DWithin(l.geom::geography, u.location::geography, :radius_m)
WHERE 'LIGHTNING' = ANY(u.alert_types)
  AND l.flash_time > NOW() - make_interval(mins => CAST(:window AS INT))
  AND NOT EXISTS (SELECT 1 FROM alert_history ah
                  WHERE ah.subscription_id = u.id AND ah.event_type = 'LIGHTNING'
                    AND ah.sent_at > NOW() - make_interval(mins => CAST(:dedup AS INT)))
GROUP BY u.id
HAVING count(*) >= :min_n
""")


def build_lightning_payload(row) -> dict:
    km = row["dmin_m"] / 1000.0
    trend = ""
    if row["d_recent"] is not None and row["d_older"] is not None:
        if row["d_recent"] < row["d_older"] - 2000:
            trend = ", in avvicinamento"
        elif row["d_recent"] > row["d_older"] + 2000:
            trend = ", in allontanamento"
    spec = LIGHTNING_EVENTS["LIGHTNING"]
    return {
        "title": f"{spec['title']} (~{km:.0f} km)",
        "body": (f"{row['n']} fulmini negli ultimi {settings.lightning_window_min} min, il più vicino a "
                 f"{km:.0f} km{trend}. Evita luoghi aperti e ripari esposti."),
        "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png", "tag": "nowcast-LIGHTNING",
        "renotify": True, "data": {"url": f"{settings.frontend_url}/?focus=alert", "event": "LIGHTNING"},
    }


async def notify_lightning(session: AsyncSession, redis) -> int:
    rows = (await session.execute(LIGHTNING_SQL, {
        "radius_m": settings.lightning_radius_km * 1000, "window": settings.lightning_window_min,
        "half": settings.lightning_window_min // 2, "dedup": LIGHTNING_EVENTS["LIGHTNING"]["dedup_min"],
        "min_n": settings.lightning_min_flashes,
    })).mappings().all()
    batch = []
    for r in rows:
        sev = LIGHTNING_EVENTS["LIGHTNING"]["severity"]
        if not should_send(sev, in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])):
            continue
        batch.append(Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], sev,
                                  build_lightning_payload(r), source="LIGHT", event_type="LIGHTNING"))
    sent = await deliver(session, redis, batch) if batch else 0
    if rows:
        log.info("Lightning alerts: %d matched, %d sent", len(rows), sent)
    return sent

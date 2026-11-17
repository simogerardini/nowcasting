"""Spatial matching (PostGIS) + Web Push delivery (VAPID) with dedup, quiet hours and endpoint cleanup."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from pywebpush import WebPushException, webpush
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events import CAP_EVENTS, RADAR_EVENTS, SEVERITY_RANK, sql_rank

log = logging.getLogger("notifier")
settings = get_settings()

CAP_SEVERITY_LEVEL = {"Minor": "MODERATE", "Moderate": "MODERATE", "Severe": "SEVERE", "Extreme": "EXTREME"}
RANK = sql_rank("f.severity_level")

# Spec §5 query, generalised to user-selected event types:
# - one notification per user per run: most severe event first, then most imminent
# - dedup: same event within its window (RAIN_START / SNOW 120 min, others 45 min), and within 45 min
#   anything not more severe than what was already sent (only upgrades get through)
# - onset events (RAIN_START, SNOW): forecast only if dry now; or observed now after a dry previous run
MATCH_SQL = text(f"""
SELECT DISTINCT ON (u.id)
    u.id AS user_id, u.endpoint, u.p256dh, u.auth,
    u.quiet_hours_start, u.quiet_hours_end, u.timezone,
    f.lead_time_minutes, f.severity_level, f.event_type, f.estimated_rain_mmh, f.max_dbz
FROM user_subscriptions u
JOIN forecast_polygons f ON ST_Intersects(u.location, f.geom)
WHERE f.run_timestamp = :current_run
  AND f.event_type = ANY(u.alert_types)
  AND (
    -- forecast: rain/snow arriving at a place that is dry now (onset) or any intense event
    (f.lead_time_minutes > 0
     AND (f.event_type NOT IN ('RAIN_START', 'SNOW') OR NOT EXISTS (
            SELECT 1 FROM forecast_polygons o
            WHERE o.run_timestamp = :current_run AND o.lead_time_minutes = 0
              AND o.event_type = f.event_type AND ST_Intersects(u.location, o.geom))))
    -- observed onset: raining/snowing now, dry in the previous radar run (covers data latency)
    OR (f.lead_time_minutes = 0 AND f.event_type IN ('RAIN_START', 'SNOW') AND NOT EXISTS (
            SELECT 1 FROM forecast_polygons o
            WHERE o.run_timestamp = (SELECT max(run_timestamp) FROM forecast_polygons
                                     WHERE run_timestamp < :current_run)
              AND o.lead_time_minutes = 0
              AND o.event_type = f.event_type AND ST_Intersects(u.location, o.geom)))
  )
  AND NOT EXISTS (
      SELECT 1 FROM alert_history ah
      WHERE ah.subscription_id = u.id AND ah.source = 'RADAR'
        AND (
          -- same event recently (onset events: longer window)
          (ah.event_type = f.event_type
           AND ah.sent_at > NOW() - make_interval(mins => CASE WHEN f.event_type IN ('RAIN_START', 'SNOW')
                                                   THEN CAST(:dedup_onset AS INT) ELSE CAST(:dedup AS INT) END))
          -- or an equal/more severe alert recently: only upgrades get through
          OR (ah.sent_at > NOW() - make_interval(mins => CAST(:dedup AS INT))
              AND {sql_rank("ah.severity_level")} >= {RANK})
        )
  )
ORDER BY u.id, {RANK} DESC, f.lead_time_minutes ASC, f.max_dbz DESC
""")

CAP_MATCH_SQL = text("""
SELECT u.id AS user_id, u.endpoint, u.p256dh, u.auth,
       u.quiet_hours_start, u.quiet_hours_end, u.timezone,
       c.identifier, c.event, c.event_type, c.severity, c.headline, c.area_desc, c.onset, c.expires
FROM cap_alerts c
JOIN user_subscriptions u ON ST_Intersects(u.location, c.geom)
WHERE (c.expires IS NULL OR c.expires > NOW())
  AND c.severity IN ('Moderate', 'Severe', 'Extreme')
  AND c.event_type = ANY(u.alert_types)
  AND NOT EXISTS (SELECT 1 FROM alert_history ah
                  WHERE ah.subscription_id = u.id AND ah.source = 'CAP' AND ah.ref = c.identifier)
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
    eta = (run_time + timedelta(minutes=lead)).astimezone(ZoneInfo(row["timezone"] or "Europe/Rome"))
    rain = row["estimated_rain_mmh"]
    if lead == 0:
        title = {"RAIN_START": "🌧️ Inizia a piovere", "SNOW": "❄️ Inizia a nevicare"}.get(ev, spec["title"])
        body = {"RAIN_START": "Il radar rileva pioggia sulla tua posizione adesso.",
                "SNOW": "Il radar rileva neve sulla tua posizione adesso."}.get(ev, "") + " Tocca per aprire il radar."
        return {
            "title": title, "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
            "tag": f"nowcast-{ev}", "renotify": False,
            "data": {"url": f"{settings.frontend_url}/?focus=alert", "severity": sev, "event": ev, "lead": 0},
        }
    title = f"{spec['title']} tra ~{lead} min"
    body = {
        "RAIN_START": f"Inizio pioggia previsto sulla tua posizione verso le {eta:%H:%M}.",
        "HEAVY_RAIN": f"Pioggia forte (~{rain:.0f} mm/h) sulla tua posizione verso le {eta:%H:%M}.",
        "CLOUDBURST": f"Nubifragio / temporale forte (~{rain:.0f} mm/h) sulla tua posizione verso le {eta:%H:%M}.",
        "HAIL": f"Cella molto intensa ({row['max_dbz']:.0f} dBZ): possibile grandine e raffiche verso le {eta:%H:%M}.",
        "SNOW": f"Neve in arrivo sulla tua posizione verso le {eta:%H:%M}.",
    }[ev] + " Tocca per aprire il radar."
    return {
        "title": title, "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
        "tag": f"nowcast-{ev}", "renotify": SEVERITY_RANK[sev] >= 2,
        "requireInteraction": sev == "EXTREME",
        "data": {"url": f"{settings.frontend_url}/?focus=alert", "severity": sev, "event": ev, "lead": lead},
    }


def build_cap_payload(row) -> dict:
    sev = CAP_SEVERITY_LEVEL.get(row["severity"], "MODERATE")
    label = CAP_EVENTS.get(row["event_type"] or "", {}).get("label", "Allerta meteo")
    what = label.removeprefix("Allerta ").strip()
    colour = {"MODERATE": "gialla", "SEVERE": "arancione", "EXTREME": "rossa"}[sev]
    icon = {"MODERATE": "🟡", "SEVERE": "🟠", "EXTREME": "🔴"}[sev]
    area = row["area_desc"] or ""
    title = f"{icon} Allerta {colour} {what}" + (f" – {area}" if area else "")
    until = ""
    if row["expires"]:
        until = f"Valida fino al {row['expires'].astimezone(ZoneInfo(row['timezone'] or 'Europe/Rome')):%d/%m alle %H:%M}. "
    body = f"{until}Fonte: MeteoAlarm / Protezione Civile. Tocca per i dettagli."
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
        ttl = settings.dedup_minutes * 60 if n.source == "RADAR" else 7 * 86400
        if not await redis.set(key, "1", ex=ttl, nx=True):
            return
        async with sem:
            status = await asyncio.to_thread(_send_sync, n)
        if status in (200, 201, 202):
            sent += 1
            await session.execute(text(
                "INSERT INTO alert_history (subscription_id, severity_level, source, ref, event_type) "
                "VALUES (:u, :s, :src, :r, :e)"),
                {"u": n.user_id, "s": n.severity, "src": n.source, "r": n.ref, "e": n.event_type})
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


async def notify_radar(session: AsyncSession, redis, run_time: datetime) -> int:
    rows = (await session.execute(MATCH_SQL, {"current_run": run_time, "dedup": settings.dedup_minutes,
                                              "dedup_onset": RADAR_EVENTS["RAIN_START"]["dedup_min"]})).mappings().all()
    batch = []
    for r in rows:
        quiet = in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])
        if not should_send(r["severity_level"], quiet):
            continue
        batch.append(Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], r["severity_level"],
                                  build_radar_payload(r, run_time), event_type=r["event_type"]))
    sent = await deliver(session, redis, batch) if batch else 0
    log.info("Radar alerts: %d matched, %d queued, %d sent", len(rows), len(batch), sent)
    return sent


async def notify_cap(session: AsyncSession, redis) -> int:
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

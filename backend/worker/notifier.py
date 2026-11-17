"""Spatial matching (PostGIS) + Web Push delivery (VAPID) with dedup, quiet hours and endpoint cleanup."""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import redis.asyncio as aioredis
from pywebpush import WebPushException, webpush
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SEVERITY_RANK

log = logging.getLogger("notifier")
settings = get_settings()

SEVERITY_TITLE = {
    "MODERATE": "🌧️ Rovescio intenso",
    "SEVERE": "⚠️ Temporale forte",
    "EXTREME": "🚨 Grandine / downburst",
}
CAP_SEVERITY_LEVEL = {"Minor": "MODERATE", "Moderate": "MODERATE", "Severe": "SEVERE", "Extreme": "EXTREME"}

# Spec §5 query, extended with: severity tiebreak on equal lead time, upgrade-aware dedup
# (blocks same-or-lower severity within the dedup window, lets upgrades through).
MATCH_SQL = text("""
SELECT DISTINCT ON (u.id)
    u.id AS user_id, u.endpoint, u.p256dh, u.auth,
    u.quiet_hours_start, u.quiet_hours_end, u.timezone,
    f.lead_time_minutes, f.severity_level, f.estimated_rain_mmh, f.max_dbz
FROM user_subscriptions u
JOIN forecast_polygons f ON ST_Intersects(u.location, f.geom)
WHERE f.run_timestamp = :current_run
  AND f.max_dbz >= u.min_severity_dbz
  AND NOT EXISTS (
      SELECT 1 FROM alert_history ah
      WHERE ah.subscription_id = u.id
        AND ah.source = 'RADAR'
        AND ah.sent_at > NOW() - make_interval(mins => :dedup)
        AND (CASE ah.severity_level WHEN 'EXTREME' THEN 3 WHEN 'SEVERE' THEN 2 ELSE 1 END)
            >= (CASE f.severity_level WHEN 'EXTREME' THEN 3 WHEN 'SEVERE' THEN 2 ELSE 1 END)
  )
ORDER BY u.id, f.lead_time_minutes ASC,
         (CASE f.severity_level WHEN 'EXTREME' THEN 3 WHEN 'SEVERE' THEN 2 ELSE 1 END) DESC,
         f.max_dbz DESC
""")

CAP_MATCH_SQL = text("""
SELECT u.id AS user_id, u.endpoint, u.p256dh, u.auth,
       u.quiet_hours_start, u.quiet_hours_end, u.timezone,
       c.identifier, c.event, c.severity, c.headline, c.area_desc, c.onset, c.expires
FROM cap_alerts c
JOIN user_subscriptions u ON ST_Intersects(u.location, c.geom)
WHERE (c.expires IS NULL OR c.expires > NOW())
  AND c.severity IN ('Moderate', 'Severe', 'Extreme')
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
    """Quiet hours silence MODERATE only; SEVERE/EXTREME always go through."""
    return not (quiet and severity == "MODERATE")


def build_radar_payload(row, run_time: datetime) -> dict:
    lead = int(row["lead_time_minutes"])
    sev = row["severity_level"]
    eta = (run_time + timedelta(minutes=lead)).astimezone(ZoneInfo(row["timezone"] or "Europe/Rome"))
    rain = row["estimated_rain_mmh"]
    title = f"{SEVERITY_TITLE[sev]} in arrivo tra ~{lead} min"
    what = {"MODERATE": "Prevista pioggia forte", "SEVERE": "Previsto nubifragio",
            "EXTREME": "Possibile grandine e raffiche violente"}[sev]
    body = f"{what} ({rain:.0f} mm/h) sulla tua posizione alle {eta:%H:%M}. Tocca per aprire il radar live."
    return {
        "title": title, "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
        "tag": f"nowcast-{sev}", "renotify": SEVERITY_RANK[sev] >= 2,
        "requireInteraction": sev == "EXTREME",
        "data": {"url": f"{settings.frontend_url}/?focus=alert", "severity": sev, "lead": lead},
    }


def build_cap_payload(row) -> dict:
    sev = CAP_SEVERITY_LEVEL.get(row["severity"], "MODERATE")
    title = f"📢 Allerta {row['event']}"
    body = (row["headline"] or row["area_desc"] or "Allerta istituzionale attiva nella tua zona")[:240]
    return {
        "title": title, "body": body, "icon": "/icons/storm-alert.png", "badge": "/icons/badge.png",
        "tag": f"cap-{row['identifier'][-24:]}",
        "data": {"url": f"{settings.frontend_url}/?focus=cap", "severity": sev},
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


async def deliver(session: AsyncSession, redis: aioredis.Redis, batch: list[Notification]) -> int:
    sem = asyncio.Semaphore(settings.push_concurrency)
    sent, gone = 0, []

    async def one(n: Notification):
        nonlocal sent
        # Redis TTL guard against concurrent/duplicate runs
        key = f"alert:{n.source}:{n.user_id}:{n.ref or n.severity}"
        ttl = settings.dedup_minutes * 60 if n.source == "RADAR" else 7 * 86400
        if not await redis.set(key, "1", ex=ttl, nx=True):
            return
        async with sem:
            status = await asyncio.to_thread(_send_sync, n)
        if status in (200, 201, 202):
            sent += 1
            await session.execute(text(
                "INSERT INTO alert_history (subscription_id, severity_level, source, ref) VALUES (:u, :s, :src, :r)"),
                {"u": n.user_id, "s": n.severity, "src": n.source, "r": n.ref})
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


async def notify_radar(session: AsyncSession, redis: aioredis.Redis, run_time: datetime) -> int:
    rows = (await session.execute(MATCH_SQL, {"current_run": run_time, "dedup": settings.dedup_minutes})).mappings().all()
    batch = []
    for r in rows:
        quiet = in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])
        if not should_send(r["severity_level"], quiet):
            continue
        batch.append(Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], r["severity_level"],
                                  build_radar_payload(r, run_time)))
    sent = await deliver(session, redis, batch) if batch else 0
    log.info("Radar alerts: %d matched, %d queued, %d sent", len(rows), len(batch), sent)
    return sent


async def notify_cap(session: AsyncSession, redis: aioredis.Redis) -> int:
    rows = (await session.execute(CAP_MATCH_SQL)).mappings().all()
    batch = []
    for r in rows:
        sev = CAP_SEVERITY_LEVEL.get(r["severity"], "MODERATE")
        if not should_send(sev, in_quiet_hours(r["quiet_hours_start"], r["quiet_hours_end"], r["timezone"])):
            continue
        batch.append(Notification(r["user_id"], r["endpoint"], r["p256dh"], r["auth"], sev,
                                  build_cap_payload(r), source="CAP", ref=r["identifier"]))
    sent = await deliver(session, redis, batch) if batch else 0
    log.info("CAP alerts: %d matched, %d sent", len(rows), sent)
    return sent

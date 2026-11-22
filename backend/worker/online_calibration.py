"""Continuous verification and calibration of the alerting model against observations.

Every cycle, the probability fields issued 5, 10 and 15 minutes ago (the only leads that can trigger
a push) are compared with the radar observed now, on the whole grid (subsampled), with the same rule
used for pushes: only points where the event was NOT already occurring at issue time count (an alert
announces an onset), hit = event observed within ~3 km. Counts go to `calib_stats` (per day, radar
source, event, lead, probability bin).

Hourly, for every source / event / lead, the push threshold is re-fitted on the last 30 days: the
lowest probability >= ALERT_P_FLOOR whose alerts were confirmed at least TARGET_PRECISION of the time
(with at least MIN_ALERTS samples). If no threshold reaches the target, alerts for that lead are
disabled; without enough data, the conservative default applies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import numpy as np
from scipy import ndimage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import RADAR_EVENTS

log = logging.getLogger("calibration.online")

ALERT_LEADS = (5, 10, 15)       # push only for onsets within 15 min
ALERT_P_FLOOR = 0.7             # "highly probable"
TARGET_PRECISION = 0.9
MIN_ALERTS = 200                # samples above a threshold needed before trusting it
WINDOW_DAYS = 30
NBINS = 20
STRIDE = 3                      # grid subsampling (pixels) for the statistics
EVENTS = ("RAIN_START", "HEAVY_RAIN", "CLOUDBURST", "HAIL")

# domain -> {run_time: {"p": {ev: uint8 (L, h, w)}, "wet": {ev: bool (h, w)}, "shape": (H, W)}}
_HISTORY: dict[str, dict[datetime, dict]] = {}
_PENDING: dict[tuple, np.ndarray] = {}   # (day, source, event, lead) -> int64 (2, NBINS): n, hits
# fitted thresholds: (source_family, event, lead) -> {"p", "precision", "n", "status"}
THRESHOLDS: dict[tuple, dict] = {}


def family(source: str | None) -> str:
    """Statistics are kept per radar family (different resolution / quality)."""
    return "DPC" if (source or "").upper() == "DPC" else "OPERA" if (source or "").upper() == "OPERA" else "OTHER"


def _exceed(dbz: np.ndarray, thr: float) -> np.ndarray:
    f = np.where(np.isfinite(dbz), dbz, -99.0)
    return ndimage.maximum_filter(f, size=3, mode="nearest") >= thr   # within ~3 km (2 km pixels)


def update(domain: str, result, probs: dict[str, np.ndarray]) -> int:
    """Store this run's short-lead probabilities and verify the runs issued 5/10/15 min ago.
    Returns the number of verified (point, lead) samples."""
    if result.observed_dbz is None:
        return 0
    obs = result.observed_dbz
    shape = obs.shape
    covered = ~np.isnan(obs)
    hist = _HISTORY.setdefault(domain, {})
    now = result.run_time
    src = family(result.source)
    day = now.date()
    sub = (slice(None, None, STRIDE), slice(None, None, STRIDE))
    exceed_now = {ev: _exceed(obs, RADAR_EVENTS[ev]["dbz"]) for ev in EVENTS}
    samples = 0
    for issued, rec in list(hist.items()):
        lead = int(round((now - issued).total_seconds() / 60))
        if rec["shape"] != shape or lead not in ALERT_LEADS:
            continue
        li = ALERT_LEADS.index(lead)
        for ev in EVENTS:
            if ev not in rec["p"]:
                continue
            p = rec["p"][ev][li]
            valid = (covered[sub] & ~rec["wet"][ev])          # not already occurring at issue time
            if not valid.any():
                continue
            pv = p[valid].astype(np.int64)
            hit = exceed_now[ev][sub][valid]
            b = np.minimum(pv * NBINS // 255, NBINS - 1)   # uint8 0..255 -> 20 bins
            acc = _PENDING.setdefault((day, src, ev, lead), np.zeros((2, NBINS), dtype=np.int64))
            acc[0] += np.bincount(b, minlength=NBINS)
            acc[1] += np.bincount(b, weights=hit.astype(np.int64), minlength=NBINS).astype(np.int64)
            samples += int(valid.sum())
    # keep this run (leads 5/10/15 only, quantised, subsampled)
    idx = [result.lead_minutes.index(L) for L in ALERT_LEADS if L in result.lead_minutes]
    if len(idx) == len(ALERT_LEADS):
        hist[now] = {
            "shape": shape,
            "p": {ev: np.round(np.clip(probs[ev][idx], 0, 1) * 255).astype(np.uint8)[(slice(None),) + sub]
                  for ev in EVENTS if ev in probs},
            "wet": {ev: exceed_now[ev][sub] for ev in EVENTS},
        }
    for t in [t for t in hist if now - t > timedelta(minutes=20)]:
        del hist[t]
    return samples


async def flush(session: AsyncSession) -> None:
    if not _PENDING:
        return
    rows = []
    for (day, src, ev, lead), acc in _PENDING.items():
        for b in range(NBINS):
            if acc[0, b]:
                rows.append({"d": day, "s": src, "e": ev, "l": lead, "b": b, "n": int(acc[0, b]), "h": int(acc[1, b])})
    _PENDING.clear()
    if rows:
        await session.execute(text("""
            INSERT INTO calib_stats (day, source, event_type, lead_time_minutes, bin, n, hits)
            VALUES (:d, :s, :e, :l, :b, :n, :h)
            ON CONFLICT (day, source, event_type, lead_time_minutes, bin)
            DO UPDATE SET n = calib_stats.n + EXCLUDED.n, hits = calib_stats.hits + EXCLUDED.hits"""), rows)
        await session.execute(text("DELETE FROM calib_stats WHERE day < CURRENT_DATE - 120"))
        await session.commit()


def fit_threshold(n: np.ndarray, hits: np.ndarray, floor: float = ALERT_P_FLOOR,
                  target: float = TARGET_PRECISION, min_alerts: int = MIN_ALERTS) -> dict:
    """Lowest bin edge >= floor whose cumulative precision (alerts with p >= edge) reaches target."""
    n = np.asarray(n, dtype=np.int64)
    hits = np.asarray(hits, dtype=np.int64)
    cn = np.cumsum(n[::-1])[::-1]
    ch = np.cumsum(hits[::-1])[::-1]
    first = int(np.ceil(floor * NBINS - 1e-9))
    if cn[first] < min_alerts:
        return {"status": "insufficient_data", "n": int(cn[first])}
    for b in range(first, NBINS):
        if cn[b] < min_alerts:
            break
        prec = ch[b] / cn[b]
        if prec >= target:
            return {"status": "ok", "p": round(b / NBINS, 3), "precision": round(float(prec), 3), "n": int(cn[b])}
    return {"status": "disabled", "p": 1.01, "precision": round(float(ch[first] / cn[first]), 3), "n": int(cn[first])}


async def recalibrate(session: AsyncSession) -> dict:
    rows = (await session.execute(text("""
        SELECT source, event_type, lead_time_minutes, bin, sum(n) AS n, sum(hits) AS hits
        FROM calib_stats WHERE day >= CURRENT_DATE - CAST(:d AS INT)
        GROUP BY 1, 2, 3, 4"""), {"d": WINDOW_DAYS})).mappings().all()
    acc: dict[tuple, np.ndarray] = {}
    for r in rows:
        a = acc.setdefault((r["source"], r["event_type"], int(r["lead_time_minutes"])), np.zeros((2, NBINS), np.int64))
        a[0, int(r["bin"])] += int(r["n"])
        a[1, int(r["bin"])] += int(r["hits"])
    fitted = {k: fit_threshold(a[0], a[1]) for k, a in acc.items()}
    THRESHOLDS.clear()
    THRESHOLDS.update(fitted)
    await session.execute(text("DELETE FROM alert_thresholds"))
    if fitted:
        await session.execute(text("""
            INSERT INTO alert_thresholds (source, event_type, lead_time_minutes, p, precision, n, status)
            VALUES (:s, :e, :l, :p, :pr, :n, :st)"""),
            [{"s": k[0], "e": k[1], "l": k[2], "p": v.get("p"), "pr": v.get("precision"), "n": v["n"],
              "st": v["status"]} for k, v in fitted.items()])
    await session.commit()
    log.info("Online calibration: %s", {f"{k[0]}/{k[1]}/{k[2]}": v.get("p", v["status"]) for k, v in fitted.items()})
    return fitted


async def load(session: AsyncSession) -> None:
    rows = (await session.execute(text("SELECT * FROM alert_thresholds"))).mappings().all()
    THRESHOLDS.clear()
    for r in rows:
        THRESHOLDS[(r["source"], r["event_type"], int(r["lead_time_minutes"]))] = {
            "status": r["status"], "p": r["p"], "precision": r["precision"], "n": r["n"]}


def threshold(source: str | None, event: str, lead: int) -> float:
    """Push threshold for an event at a lead (SNOW uses RAIN_START: same echo, typed by the snow mask)."""
    from .probability import alert_threshold

    ev = "RAIN_START" if event == "SNOW" else event
    default = max(alert_threshold(event), ALERT_P_FLOOR)
    fit = THRESHOLDS.get((family(source), ev, lead))
    if fit and fit["status"] in ("ok", "disabled") and fit.get("p") is not None:
        return max(float(fit["p"]), ALERT_P_FLOOR)
    return default

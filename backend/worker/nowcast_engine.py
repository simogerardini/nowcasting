"""pySTEPS nowcasting: Lucas-Kanade optical flow + S-PROG, 12 x 5 min (+60 min)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from pysteps import extrapolation, motion, nowcasts

from app.config import get_settings

from .ingest_dpc import RadarFrame

log = logging.getLogger("nowcast")
settings = get_settings()

RAIN_THR_MMH = 0.1        # below this = no rain in dB space
DB_ZERO = -15.0           # zero value in dB(R) space


@dataclass
class NowcastResult:
    run_time: datetime                 # time of the last observed frame
    observed: list[RadarFrame]         # input frames (dBZ, filtered)
    forecast_dbz: np.ndarray           # (T, H, W) dBZ, NaN outside coverage
    lead_minutes: list[int]
    transform: object
    crs: object
    source: str
    elapsed_s: float
    snow_fc: np.ndarray | None = None  # (T, H, W) advected snow fraction 0..1 (None = unknown)
    observed_dbz: np.ndarray | None = None  # last observed frame, filtered (for "already raining" checks)


# ------------------------------------------------------------------ Z-R conversions (Marshall-Palmer)
def dbz_to_rain(dbz: np.ndarray, a: float | None = None, b: float | None = None) -> np.ndarray:
    a = a or settings.zr_a
    b = b or settings.zr_b
    z = np.power(10.0, dbz / 10.0)
    return np.power(z / a, 1.0 / b)


def rain_to_dbz(r: np.ndarray, a: float | None = None, b: float | None = None) -> np.ndarray:
    a = a or settings.zr_a
    b = b or settings.zr_b
    with np.errstate(divide="ignore"):
        return 10.0 * np.log10(a * np.power(r, b))


def preprocess(dbz: np.ndarray) -> np.ndarray:
    """Drop noise/drizzle < MIN_DBZ (set to 'no rain' but keep coverage)."""
    out = dbz.astype("float32", copy=True)
    valid = np.isfinite(out)
    out[valid & (out < settings.min_dbz)] = -np.inf  # observed, no significant echo
    return out


def _to_db_rain(dbz: np.ndarray) -> np.ndarray:
    r = np.where(np.isfinite(dbz), dbz_to_rain(np.where(np.isfinite(dbz), dbz, 0.0)), 0.0)
    r[~np.isfinite(dbz)] = 0.0
    with np.errstate(divide="ignore"):
        db = np.where(r >= RAIN_THR_MMH, 10.0 * np.log10(np.maximum(r, 1e-6)), DB_ZERO)
    return db.astype("float64")


def _from_db_rain(db: np.ndarray) -> np.ndarray:
    r = np.where(db > DB_ZERO + 1e-6, np.power(10.0, db / 10.0), 0.0)
    dbz = np.full(r.shape, -np.inf, dtype="float32")
    wet = r >= RAIN_THR_MMH
    dbz[wet] = rain_to_dbz(r[wet]).astype("float32")
    return dbz


def run_nowcast(frames: list[RadarFrame]) -> NowcastResult:
    if len(frames) < 3:
        raise ValueError("need at least 3 frames")
    shapes = {f.dbz.shape for f in frames}
    if len(shapes) != 1:
        raise ValueError(f"inconsistent radar grids: {shapes}")
    t0 = time.perf_counter()
    n = settings.n_leadtimes
    lead = [settings.timestep_min * (i + 1) for i in range(n)]
    # Input cadence may differ from the output step (DPC 5 min, RainViewer 10 min):
    # pySTEPS takes fractional timesteps in units of the input interval.
    diffs = [(b.time - a.time).total_seconds() / 60 for a, b in zip(frames, frames[1:])]
    dt_in = float(np.median(diffs)) if diffs else settings.timestep_min
    steps: int | list[float] = n if abs(dt_in - settings.timestep_min) < 0.5 else [m / dt_in for m in lead]

    filtered = [RadarFrame(f.time, preprocess(f.dbz), f.transform, f.crs, f.source, f.snow) for f in frames]
    coverage = ~np.isnan(frames[-1].dbz)  # radar footprint (-inf = observed dry)
    stack = np.stack([_to_db_rain(f.dbz) for f in filtered])  # (T,H,W) dB(R), no NaN

    wet_frac = float((stack[-1] > DB_ZERO).mean())
    snow_fc = None
    if wet_frac < 1e-5:
        log.info("No significant echoes - empty forecast")
        fc = np.full((n,) + stack.shape[1:], -np.inf, dtype="float32")
    else:
        velocity = motion.get_method("LK")(stack, fd_kwargs={"buffer_mask": 10}, verbose=False)
        ar_order = 2
        try:
            sprog = nowcasts.get_method("sprog")
            fc_db = sprog(
                stack[-(ar_order + 1):], velocity, steps,
                precip_thr=10.0 * np.log10(RAIN_THR_MMH),
                n_cascade_levels=6, ar_order=ar_order,
                probmatching_method="cdf", domain="spatial", measure_time=False,
            )
        except Exception as exc:  # noqa: BLE001 - degenerate fields: pure advection
            log.warning("S-PROG failed (%s); using semi-Lagrangian extrapolation", exc)
            fc_db = nowcasts.get_method("extrapolation")(stack[-1], velocity, steps)
        fc_db = np.nan_to_num(np.asarray(fc_db), nan=DB_ZERO)
        fc = np.stack([_from_db_rain(fc_db[i]) for i in range(n)])
        last_snow = frames[-1].snow
        if last_snow is not None and float(np.asarray(last_snow).max()) > 0:
            sl = extrapolation.get_method("semilagrangian")
            snow_fc = np.clip(np.nan_to_num(sl(np.asarray(last_snow, dtype="float64"), velocity, steps), nan=0.0), 0, 1)
            snow_fc = snow_fc.astype("float32")

    fc[:, ~coverage] = np.nan
    elapsed = time.perf_counter() - t0
    log.info("Nowcast done in %.2fs (grid %s, wet %.3f%%, input dt %.0f min)", elapsed, stack.shape[1:],
             wet_frac * 100, dt_in)
    return NowcastResult(
        run_time=frames[-1].time, observed=filtered, forecast_dbz=fc, lead_minutes=lead,
        transform=frames[-1].transform, crs=frames[-1].crs, source=frames[-1].source, elapsed_s=elapsed,
        snow_fc=snow_fc, observed_dbz=filtered[-1].dbz,
    )


def valid_time(run_time: datetime, lead_min: int) -> datetime:
    return run_time + timedelta(minutes=lead_min)

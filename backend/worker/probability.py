"""Probabilistic nowcast: local Lagrangian exceedance probabilities (Germann & Zawadzki 2004, pySTEPS),
calibrated against observations (reliability tables from backtests + online verification).

P(event at pixel, lead) = fraction of the extrapolated field above the event threshold within a
neighbourhood whose size grows linearly with lead time (slope, px per input step). Raw probabilities
are then mapped through a per-event / per-lead reliability table so that "70%" means it happens ~70%
of the time.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import numpy as np
from pysteps.nowcasts import lagrangian_probability

from app.events import RADAR_EVENTS

log = logging.getLogger("probability")

CALIBRATION_FILE = Path(__file__).with_name("data") / "calibration.json"
BINS = np.linspace(0.0, 1.0, 21)        # 20 probability bins
DEFAULT_SLOPE = 2.5                     # px / 5-min step at 2 km px ~ 1 km/min (Germann & Zawadzki)
DEFAULT_ALERT_P = {"RAIN_START": 0.7, "HEAVY_RAIN": 0.6, "CLOUDBURST": 0.5, "HAIL": 0.5, "SNOW": 0.7}
PROB_EVENTS = ["RAIN_START", "HEAVY_RAIN", "CLOUDBURST", "HAIL"]  # threshold events (SNOW uses RAIN_START x snow)


@lru_cache(maxsize=1)
def load_calibration() -> dict:
    if CALIBRATION_FILE.exists():
        try:
            return json.loads(CALIBRATION_FILE.read_text())
        except ValueError:
            log.warning("invalid calibration file, using defaults")
    return {}


def slope_for(event: str, dt_in_min: float, cal: dict | None = None) -> float:
    cal = cal if cal is not None else load_calibration()
    s = cal.get("slope_km_per_min", {}).get(event)
    if s is None:
        return DEFAULT_SLOPE * dt_in_min / 5.0
    px_km = cal.get("pixel_km", 2.0)
    return float(s) * dt_in_min / px_km  # km/min -> px per input step


def alert_threshold(event: str) -> float:
    return float(load_calibration().get("alert_p", {}).get(event, DEFAULT_ALERT_P.get(event, 0.6)))


def raw_probabilities(field_dbz: np.ndarray, velocity: np.ndarray, steps, threshold: float,
                      slope_px: float) -> np.ndarray:
    """(T, H, W) exceedance probabilities of `threshold` dBZ."""
    f = np.where(np.isfinite(field_dbz), field_dbz, -99.0).astype("float64")
    p = lagrangian_probability.forecast(f, velocity, steps, threshold, slope=slope_px)
    return np.clip(np.nan_to_num(np.asarray(p, dtype="float32"), nan=0.0), 0, 1)


def calibrate(p: np.ndarray, event: str, lead_idx: int, cal: dict | None = None) -> np.ndarray:
    """Map raw probabilities through the reliability table (piecewise-linear over bin centres)."""
    cal = cal if cal is not None else load_calibration()
    table = cal.get("reliability", {}).get(event)
    if not table or lead_idx >= len(table):
        return p
    freq = np.asarray(table[lead_idx], dtype="float64")
    centres = 0.5 * (BINS[:-1] + BINS[1:])
    ok = np.isfinite(freq)
    if ok.sum() < 3:
        return p
    xs = np.concatenate([[0.0], centres[ok], [1.0]])
    ys = np.concatenate([[0.0], np.maximum.accumulate(freq[ok]), [max(freq[ok].max(), freq[ok][-1])]])
    return np.interp(p, xs, ys).astype("float32")


def event_probabilities(result, cal: dict | None = None) -> dict[str, np.ndarray]:
    """Calibrated (T, H, W) probabilities for each threshold event. Empty dict if no motion field."""
    if result.velocity is None:
        return {}
    cal = cal if cal is not None else load_calibration()
    diffs = [(b.time - a.time).total_seconds() / 60 for a, b in zip(result.observed, result.observed[1:])]
    dt_in = float(np.median(diffs)) if diffs else 5.0
    field = result.observed_dbz
    out = {}
    for ev in PROB_EVENTS:
        thr = RADAR_EVENTS[ev]["dbz"]
        raw = raw_probabilities(field, result.velocity, result.steps, thr, slope_for(ev, dt_in, cal))
        if ev == "HAIL" and result.poh_fc is not None:
            # DPC Probability Of Hail inside convective cores raises the hail probability
            p45 = raw_probabilities(field, result.velocity, result.steps, 45, slope_for("CLOUDBURST", dt_in, cal))
            raw = np.maximum(raw, np.minimum(result.poh_fc, p45))
        out[ev] = np.stack([calibrate(raw[i], ev, i, cal) for i in range(raw.shape[0])])
        out[ev][:, np.isnan(result.forecast_dbz[0])] = 0.0  # outside radar coverage
    if result.snow_fc is not None and "RAIN_START" in out:
        out["SNOW"] = (out["RAIN_START"] * (result.snow_fc > 0.5)).astype("float32")
        out["RAIN_START"] = (out["RAIN_START"] * (result.snow_fc <= 0.5)).astype("float32")
    return out

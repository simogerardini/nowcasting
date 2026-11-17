"""Backtest + calibration of the probabilistic nowcast against DPC radar observations.

For many historical runs (DPC archive): build the nowcast exactly as in production, then compare every
forecast lead with the radar frame observed at the valid time. Accumulates, per event and lead:
  - reliability histograms of raw Lagrangian probabilities for several neighbourhood slopes
  - contingency counts of the deterministic S-PROG exceedance (baseline, i.e. the previous model)
Then fits: best slope per event (Brier score), reliability tables, and alert probability thresholds
meeting a target precision. Writes worker/data/calibration.json and a markdown report.

  python -m scripts.backtest_calibrate --train 2026-10-20,2026-10-21 --valid 2026-11-16 --runs-per-day 8
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

os.environ.setdefault("OMP_NUM_THREADS", "1")

from app.config import get_settings  # noqa: E402
from app.events import RADAR_EVENTS  # noqa: E402
from worker import probability as P  # noqa: E402
from worker.ingest_dpc import RadarFrame, clamp_reflectivity, crop_and_downsample, quality_control  # noqa: E402

log = logging.getLogger("backtest")
settings = get_settings()
EVENTS = P.PROB_EVENTS
SLOPES_KM_MIN = [0.25, 0.5, 1.0, 1.5, 2.0]
NB = len(P.BINS) - 1
CACHE = Path(os.environ.get("BT_CACHE", "/tmp/nowcast-bt"))


# ------------------------------------------------------------------ data
def load_frame(t: datetime) -> RadarFrame | None:
    """DPC VMI frame, cropped + downsampled, cached as compressed npz."""
    import httpx
    from rasterio.transform import Affine

    from worker.ingest_dpc import DPCClient, _read_geotiff

    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"VMI_{t:%Y%m%d%H%M}.npz"
    if f.exists():
        z = np.load(f, allow_pickle=True)
        from rasterio.crs import CRS
        return RadarFrame(t, z["dbz"].astype("float32"), Affine(*z["tr"]), CRS.from_wkt(str(z["crs"])), "DPC")
    try:
        with httpx.Client(timeout=60) as c:
            d = DPCClient(c)
            content = d._download(t, "VMI")
        raw = _read_geotiff(content, t, "DPC")
    except Exception as exc:  # noqa: BLE001
        log.warning("missing %s: %s", t, exc)
        return None
    fr = crop_and_downsample(raw)
    np.savez_compressed(f, dbz=fr.dbz.astype("float16"), tr=np.array(fr.transform)[:6], crs=fr.crs.to_wkt())
    return fr


# ------------------------------------------------------------------ one run
def one_run(t0_iso: str) -> dict | None:
    from scipy import ndimage

    from worker.nowcast_engine import preprocess, run_nowcast

    t0 = datetime.fromisoformat(t0_iso)
    ins = [load_frame(t0 - timedelta(minutes=5 * k)) for k in range(5, -1, -1)]
    if any(f is None for f in ins):
        return None
    frames = quality_control(ins)
    wet = np.nan_to_num(frames[-1].dbz, nan=-99, neginf=-99) >= 15
    if wet.mean() < 0.002:
        return None  # nothing to verify
    res = run_nowcast(frames)
    if res.velocity is None:
        return None
    leads = res.lead_minutes
    obs = []
    for L in leads:
        o = load_frame(t0 + timedelta(minutes=L))
        if o is None:
            return None
        obs.append(preprocess(clamp_reflectivity(o).dbz))
    acc = {"n": 0}
    hist = np.zeros((len(EVENTS), len(SLOPES_KM_MIN), len(leads), NB, 3), dtype="int64")  # n, obs, obs_nb
    brier = np.zeros((len(EVENTS), len(SLOPES_KM_MIN), len(leads)), dtype="float64")
    det = np.zeros((len(EVENTS), len(leads), 4), dtype="int64")  # hits, misses, false alarms, correct neg (nb obs)
    field = res.observed_dbz
    for ei, ev in enumerate(EVENTS):
        thr = RADAR_EVENTS[ev]["dbz"]
        for li in range(len(leads)):
            o = np.nan_to_num(obs[li], nan=np.nan, neginf=-99)
            valid = np.isfinite(o) & np.isfinite(np.nan_to_num(res.forecast_dbz[li], neginf=-99))
            yes = (o >= thr) & valid
            yes_nb = ndimage.maximum_filter(np.where(valid, o, -99), size=3) >= thr  # within ~3 km
            fc = np.nan_to_num(res.forecast_dbz[li], nan=-99, neginf=-99) >= thr
            det[ei, li] += [(fc & yes_nb & valid).sum(), (~fc & yes & valid).sum(),
                            (fc & ~yes_nb & valid).sum(), (~fc & ~yes_nb & valid).sum()]
        for si, s_km in enumerate(SLOPES_KM_MIN):
            p = P.raw_probabilities(field, res.velocity, res.steps, thr, s_km * 5.0 / 2.0)
            for li in range(len(leads)):
                o = np.nan_to_num(obs[li], neginf=-99)
                valid = np.isfinite(o) & np.isfinite(np.nan_to_num(res.forecast_dbz[li], neginf=-99))
                yes = (o >= thr)
                yes_nb = ndimage.maximum_filter(np.where(valid, o, -99), size=3) >= thr
                pv = p[li][valid]
                b = np.clip((pv * NB).astype(int), 0, NB - 1)
                hist[ei, si, li, :, 0] += np.bincount(b, minlength=NB)
                hist[ei, si, li, :, 1] += np.bincount(b, weights=yes[valid], minlength=NB).astype("int64")
                hist[ei, si, li, :, 2] += np.bincount(b, weights=yes_nb[valid], minlength=NB).astype("int64")
                brier[ei, si, li] += float(((pv - yes[valid]) ** 2).sum())
    acc.update(hist=hist, brier=brier, det=det, n=1)
    return acc


def run_split(days: list[str], runs_per_day: int, workers: int) -> dict:
    times = []
    for d in days:
        base = datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        for k in range(runs_per_day):
            times.append((base + timedelta(minutes=30 + int(k * 1440 / runs_per_day))).isoformat())
    total = None
    with ProcessPoolExecutor(workers) as ex:
        for i, r in enumerate(ex.map(one_run, times)):
            if r is None:
                continue
            if total is None:
                total = r
            else:
                for k in ("hist", "brier", "det", "n"):
                    total[k] = total[k] + r[k]
            log.info("run %d/%d done (%d used)", i + 1, len(times), total["n"] if total else 0)
    return total


# ------------------------------------------------------------------ fitting
def fit(train: dict, targets: dict[str, float]) -> dict:
    hist, brier = train["hist"], train["brier"]
    cal = {"pixel_km": 2.0, "bins": P.BINS.tolist(), "slope_km_per_min": {}, "reliability": {}, "alert_p": {},
           "fitted_on_runs": int(train["n"])}
    for ei, ev in enumerate(EVENTS):
        n_tot = hist[ei, :, :, :, 0].sum(axis=(1, 2))
        bs = brier[ei].sum(axis=1) / np.maximum(n_tot, 1)
        si = int(np.argmin(bs))
        cal["slope_km_per_min"][ev] = SLOPES_KM_MIN[si]
        h = hist[ei, si]  # leads x bins x 3
        rel = []
        for li in range(h.shape[0]):
            n, k = h[li, :, 0], h[li, :, 1]
            f = np.where(n >= 200, k / np.maximum(n, 1), np.nan)
            rel.append([None if not np.isfinite(x) else round(float(x), 4) for x in f])
        cal["reliability"][ev] = rel
        # alert threshold on CALIBRATED probability: smallest p with precision (obs within ~3 km) >= target
        max_li = RADAR_EVENTS[ev]["max_lead"] // 5
        cal_tab = {"reliability": {ev: rel}}
        centres = 0.5 * (P.BINS[:-1] + P.BINS[1:])
        pc = np.stack([P.calibrate(centres.astype("float32"), ev, li, cal_tab) for li in range(max_li)])
        n = h[:max_li, :, 0]
        hit_nb = h[:max_li, :, 2]
        best = None
        for thr_p in np.arange(0.3, 0.96, 0.05):
            sel = pc >= thr_p
            alerts = n[sel].sum()
            if alerts < 50:
                continue
            prec = hit_nb[sel].sum() / alerts
            if prec >= targets[ev]:
                best = round(float(thr_p), 2)
                break
        cal["alert_p"][ev] = best if best is not None else 0.95
    return cal


def evaluate(split: dict, cal: dict) -> dict:
    """Pixel-lead scores of the calibrated alert rule vs the old deterministic S-PROG rule."""
    out = {}
    centres = 0.5 * (P.BINS[:-1] + P.BINS[1:])
    for ei, ev in enumerate(EVENTS):
        si = SLOPES_KM_MIN.index(cal["slope_km_per_min"][ev])
        h = split["hist"][ei, si]
        max_li = RADAR_EVENTS[ev]["max_lead"] // 5
        pc = np.stack([P.calibrate(centres.astype("float32"), ev, li, cal) for li in range(max_li)])
        sel = pc >= cal["alert_p"][ev]
        n, hit_nb, yes = h[:max_li, :, 0], h[:max_li, :, 2], h[:max_li, :, 1]
        alerts = n[sel].sum()
        prec = hit_nb[sel].sum() / alerts if alerts else float("nan")
        pod = yes[sel].sum() / max(yes.sum(), 1)
        d = split["det"][ei, :max_li].sum(axis=0)
        old_prec = d[0] / max(d[0] + d[2], 1)
        old_pod = d[0] / max(d[0] + d[1], 1)
        # reliability (calibrated): mean forecast vs observed frequency, all leads
        mf = (pc * n).sum() / max(n.sum(), 1)
        of = yes.sum() / max(n.sum(), 1)
        out[ev] = {"alert_p": cal["alert_p"][ev], "precision": round(float(prec), 3), "pod": round(float(pod), 3),
                   "old_precision": round(float(old_prec), 3), "old_pod": round(float(old_pod), 3),
                   "mean_forecast_p": round(float(mf), 4), "observed_freq": round(float(of), 4),
                   "alert_pixels": int(alerts)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--valid", required=True)
    ap.add_argument("--runs-per-day", type=int, default=8)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--out", default=str(P.CALIBRATION_FILE))
    ap.add_argument("--report", default="docs/CALIBRATION.md")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for n in ("httpx", "nowcast", "vectorizer", "ingest"):
        logging.getLogger(n).setLevel(logging.WARNING)
    targets = {"RAIN_START": 0.8, "HEAVY_RAIN": 0.75, "CLOUDBURST": 0.6, "HAIL": 0.5}
    train = run_split(a.train.split(","), a.runs_per_day, a.workers)
    cal = fit(train, targets)
    valid = run_split(a.valid.split(","), a.runs_per_day, a.workers)
    cal["targets_precision"] = targets
    cal["train_days"], cal["valid_days"] = a.train.split(","), a.valid.split(",")
    cal["scores_train"] = evaluate(train, cal)
    cal["scores_valid"] = evaluate(valid, cal) if valid else {}
    Path(a.out).write_text(json.dumps(cal, indent=1))
    lines = ["# Calibrazione nowcast (backtest DPC)", "",
             f"Run di addestramento: {train['n']} · run di validazione: {valid['n'] if valid else 0}", "",
             "| Evento | slope km/min | soglia P | precisione (valid) | POD (valid) | precisione modello precedente | POD precedente |",
             "|---|---|---|---|---|---|---|"]
    for ev in EVENTS:
        sv = cal["scores_valid"].get(ev, {})
        lines.append(f"| {ev} | {cal['slope_km_per_min'][ev]} | {cal['alert_p'][ev]} | {sv.get('precision')} | "
                     f"{sv.get('pod')} | {sv.get('old_precision')} | {sv.get('old_pod')} |")
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()

"""Radar ingestion: DPC (Protezione Civile) national mosaic, RainViewer fallback, CAP alerts."""
from __future__ import annotations

import io
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import rasterio
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import Affine, from_bounds

from app.config import get_settings

from .rainviewer_palette import decode_with_snow, snow_mask

log = logging.getLogger("ingest")
settings = get_settings()

UA = {"User-Agent": "noctalix-nowcast/1.0 (+https://nowcast.noctalix.com)"}


@dataclass
class RadarFrame:
    time: datetime              # UTC, nominal scan time
    dbz: np.ndarray             # float32 (H, W), NaN = no data / outside coverage
    transform: Affine
    crs: CRS
    source: str
    snow: np.ndarray | None = None  # bool/fraction mask of snow echoes (RainViewer only)
    poh: np.ndarray | None = None   # probability of hail 0..1 (DPC POH product, newest frame only)


class IngestError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def _floor5(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def _read_geotiff(content: bytes, t: datetime, source: str) -> RadarFrame:
    with rasterio.MemoryFile(content) as mem, mem.open() as ds:
        arr = ds.read(1).astype("float32")
        nodata = ds.nodata
        dry = arr == -9998            # DPC: inside radar coverage, no echo
        missing = (arr == -9999) | (arr < -100) | (arr > 100)
        if nodata is not None:
            missing |= arr == nodata
        arr[dry] = -np.inf
        arr[missing & ~dry] = np.nan
        crs = ds.crs or CRS.from_epsg(4326)
        return RadarFrame(t, arr, ds.transform, crs, source)


# ------------------------------------------------------------------ DPC
class DPCClient:
    """Client for the DPC Radar REST API (https://dpc-radar.readthedocs.io/it/latest/api.html).

    Since 12/01/2026: GET /findLastProductByType?type=..., POST /downloadProduct -> pre-signed S3 URL;
    every request must carry an Origin header (documented requirement)."""

    def __init__(self, client: httpx.Client, product: str | None = None):
        self.c = client
        self.base = settings.dpc_api_base.rstrip("/")
        self.product = product or settings.radar_product
        self.cache = Path(settings.radar_cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.headers = {"Origin": settings.dpc_origin}

    def latest_time(self, product: str | None = None) -> datetime:
        r = self.c.get(f"{self.base}/findLastProductByType", params={"type": product or self.product},
                       headers=self.headers)
        r.raise_for_status()
        items = r.json().get("lastProducts") or []
        if not items:
            raise IngestError("DPC: no lastProducts")
        return datetime.fromtimestamp(int(items[0]["time"]) / 1000, tz=timezone.utc)

    def _download(self, t: datetime, product: str) -> bytes:
        ms = int(t.timestamp() * 1000)
        r = self.c.post(f"{self.base}/downloadProduct", json={"productType": product, "productDate": ms},
                        headers=self.headers)
        r.raise_for_status()
        url = r.json().get("url")
        if not url:
            raise IngestError(f"DPC: no url for {product} {t}")
        f = self.c.get(url)  # pre-signed S3 URL (expires in minutes)
        f.raise_for_status()
        return f.content

    def frame(self, t: datetime, product: str | None = None) -> RadarFrame:
        product = product or self.product
        path = self.cache / f"{product}_{t:%Y%m%d%H%M}.tif"
        if path.exists():
            content = path.read_bytes()
        else:
            content = self._download(t, product)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(content)
            tmp.replace(path)
        return _read_geotiff(content, t, "DPC")

    def poh(self, t: datetime) -> np.ndarray | None:
        """Probability of hail (0..1) at time t, NaN outside coverage; None if unavailable."""
        try:
            f = self.frame(t, "POH")
        except Exception as exc:  # noqa: BLE001
            log.info("DPC POH unavailable for %s: %s", t, exc)
            return None
        p = np.where(np.isfinite(f.dbz), f.dbz, 0.0).astype("float32")
        if np.nanmax(p) > 1.0:  # percent -> fraction
            p = p / 100.0
        return np.clip(p, 0, 1)

    def last_frames(self, n: int) -> list[RadarFrame]:
        latest = self.latest_time()
        frames: list[RadarFrame] = []
        for i in range(n - 1, -1, -1):
            t = latest - timedelta(minutes=settings.timestep_min * i)
            frames.append(self.frame(t))
        if settings.use_poh:
            poh = self.poh(latest)
            if poh is not None and poh.shape == frames[-1].dbz.shape:
                last = frames[-1]
                frames[-1] = RadarFrame(last.time, last.dbz, last.transform, last.crs, last.source, None, poh)
        self._prune_cache(latest)
        return frames

    def _prune_cache(self, latest: datetime) -> None:
        cutoff = latest - timedelta(hours=3)
        for p in self.cache.glob("*_*.tif"):
            try:
                ts = datetime.strptime(p.stem.split("_")[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    p.unlink()
            except (ValueError, IndexError):
                continue


# ------------------------------------------------------------------ RainViewer (fallback)
ITALY_BBOX = (6.0, 35.5, 19.0, 47.5)  # lon_min, lat_min, lon_max, lat_max
RV_ZOOM = 7
WEB_MERC = CRS.from_epsg(3857)
R_EARTH = 6378137.0


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def _tile_bounds_merc(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    size = 2 * math.pi * R_EARTH / 2 ** z
    left = -math.pi * R_EARTH + x * size
    top = math.pi * R_EARTH - y * size
    return left, top - size, left + size, top


_RV_CACHE: dict[str, RadarFrame] = {}  # frame path -> decoded frame (frames are immutable)
LAST_DPC_ERROR: str | None = None


class RainViewerClient:
    """Composite from RainViewer tiles (public API: zoom <= 7, "Universal Blue" scheme only, 10-min frames).
    Colours are decoded back to dBZ with the official RainViewer colour table."""

    def __init__(self, client: httpx.Client, bbox=ITALY_BBOX, zoom=RV_ZOOM):
        self.c, self.bbox, self.z = client, bbox, zoom

    def last_frames(self, n: int) -> list[RadarFrame]:
        meta = self.c.get(settings.rainviewer_api)
        meta.raise_for_status()
        m = meta.json()
        past = m["radar"]["past"]
        frames: list[RadarFrame] = []
        # newest -> oldest; a frame whose tiles are not all published yet is skipped (not cached)
        for p in reversed(past):
            if len(frames) == n:
                break
            if p["path"] not in _RV_CACHE:
                frame = self._frame(m["host"], p)
                if frame is None:
                    if not frames:
                        log.info("RainViewer frame %s incomplete, using the previous one", p["time"])
                        continue
                    break  # gap in the middle: stop, keep consecutive frames only
                _RV_CACHE[p["path"]] = frame
            frames.append(_RV_CACHE[p["path"]])
        # tiles can be served (HTTP 200) before rendering is complete: a newest frame with far less
        # echo than the previous one is treated as incomplete and re-downloaded next time
        if len(frames) >= 2:
            wet = [int((np.nan_to_num(f.dbz, nan=-np.inf, neginf=-np.inf) > 0).sum()) for f in frames[:2]]
            if wet[1] > 2000 and wet[0] < 0.3 * wet[1]:
                log.warning("RainViewer newest frame looks incomplete (%d vs %d wet px): skipped", *wet)
                stale = [k for k, v in _RV_CACHE.items() if v is frames[0]]
                for k in stale:
                    del _RV_CACHE[k]
                frames = frames[1:]
        if len(frames) < 3:
            raise IngestError("RainViewer: not enough complete frames")
        keep = {p["path"] for p in past}
        for k in [k for k in _RV_CACHE if k not in keep]:
            del _RV_CACHE[k]
        return list(reversed(frames))

    def _frame(self, host: str, p: dict) -> RadarFrame | None:
        from concurrent.futures import ThreadPoolExecutor

        x0, y0 = _tile_xy(self.bbox[0], self.bbox[3], self.z)
        x1, y1 = _tile_xy(self.bbox[2], self.bbox[1], self.z)
        nx, ny = x1 - x0 + 1, y1 - y0 + 1
        out = np.full((ny * 256, nx * 256), np.nan, dtype="float32")
        snow = np.zeros(out.shape, dtype=bool)

        def get(tx: int, ty: int, snow_opt: int):
            r = self.c.get(f"{host}{p['path']}/256/{self.z}/{tx}/{ty}/2/0_{snow_opt}.png")
            return tx, ty, snow_opt, (r.content if r.status_code == 200 else None)

        jobs = [(tx, ty, so) for ty in range(y0, y1 + 1) for tx in range(x0, x1 + 1) for so in (0, 1)]
        tiles = []
        for attempt in range(3):  # retry tiles not (yet) available
            with ThreadPoolExecutor(max_workers=4) as ex:
                got = list(ex.map(lambda j: get(*j), jobs))
            tiles += [t for t in got if t[3] is not None]
            jobs = [(t[0], t[1], t[2]) for t in got if t[3] is None]
            if not jobs:
                break
            time.sleep(2 + 3 * attempt)
        if jobs:
            log.warning("RainViewer frame %s: %d tiles missing after retries", p["time"], len(jobs))
            return None
        for tx, ty, snow_opt, content in tiles:
            if content is None:
                continue
            img = np.asarray(Image.open(io.BytesIO(content)).convert("RGBA"))
            oy, ox = (ty - y0) * 256, (tx - x0) * 256
            if snow_opt == 0:
                # intensity from the rain-only rendering: every colour is in the official table
                # (snow=1 tiles add undocumented pink "mixed" colours that would decode as hail).
                # transparent = no echo: inside the mosaic that is "dry" (-inf), not missing data
                dbz, _ = decode_with_snow(img)
                out[oy:oy + 256, ox:ox + 256] = np.nan_to_num(dbz, nan=-np.inf)
            else:
                snow[oy:oy + 256, ox:ox + 256] = snow_mask(img)
        left, _, _, top = _tile_bounds_merc(x0, y0, self.z)
        _, bottom, right, _ = _tile_bounds_merc(x1, y1, self.z)
        transform = from_bounds(left, bottom, right, top, out.shape[1], out.shape[0])
        t = datetime.fromtimestamp(int(p["time"]), tz=timezone.utc)
        return RadarFrame(t, out, transform, WEB_MERC, "RAINVIEWER", snow)


# ------------------------------------------------------------------ grid reduction
def crop_and_downsample(f: RadarFrame, bbox=ITALY_BBOX, factor: int | None = None) -> RadarFrame:
    """Crop to the Italy bbox and block-average in linear Z (keeps dry=-inf, missing=NaN semantics)."""
    from pyproj import Transformer
    from rasterio.windows import from_bounds as win_from_bounds

    factor = factor or settings.radar_downsample
    arr, tr, snow, poh = f.dbz, f.transform, f.snow, f.poh
    if f.crs and not f.crs.is_geographic:
        t = Transformer.from_crs("EPSG:4326", f.crs, always_xy=True)
        (x0, x1), (y0, y1) = t.transform([bbox[0], bbox[2]], [bbox[1], bbox[3]])
    else:
        x0, y0, x1, y1 = bbox
    win = win_from_bounds(x0, y0, x1, y1, transform=tr).round_offsets().round_lengths()
    r0, c0 = max(int(win.row_off), 0), max(int(win.col_off), 0)
    r1, c1 = min(r0 + int(win.height), arr.shape[0]), min(c0 + int(win.width), arr.shape[1])
    if r1 > r0 and c1 > c0:
        arr = arr[r0:r1, c0:c1]
        snow = snow[r0:r1, c0:c1] if snow is not None else None
        poh = poh[r0:r1, c0:c1] if poh is not None else None
        tr = tr * Affine.translation(c0, r0)
    if factor and factor > 1:
        h, w = (arr.shape[0] // factor) * factor, (arr.shape[1] // factor) * factor
        a = arr[:h, :w]
        z = np.where(np.isfinite(a), np.power(10.0, a / 10.0), 0.0)
        blocks = z.reshape(h // factor, factor, w // factor, factor)
        missing = np.isnan(a).reshape(blocks.shape).all(axis=(1, 3))
        zmean = blocks.mean(axis=(1, 3))
        with np.errstate(divide="ignore"):
            out = np.where(zmean > 0, 10.0 * np.log10(zmean), -np.inf).astype("float32")
        out[missing] = np.nan
        if snow is not None:
            snow = snow[:h, :w].astype("float32").reshape(blocks.shape).mean(axis=(1, 3))
        if poh is not None:  # max-pool: a hail core must not be diluted away
            poh = poh[:h, :w].reshape(blocks.shape).max(axis=(1, 3))
        arr, tr = out, tr * Affine.scale(factor, factor)
    return RadarFrame(f.time, arr, tr, f.crs, f.source, snow, poh)


# ------------------------------------------------------------------ quality control
def remove_static_clutter(frames: list[RadarFrame], strong: float = 40.0, persist: int = 4,
                          context_max: float = 25.0, win: int = 11) -> list[RadarFrame]:
    """Remove ground clutter / mountain artefacts: strong echoes that stay on the SAME pixels in most
    frames (real storms move) while their surroundings are weak (real cores sit inside rain >= ~25 dBZ).
    Affected pixels are replaced by the local background level."""
    from scipy import ndimage

    if len(frames) < 3:
        return frames
    stack = np.stack([f.dbz for f in frames])
    strong_count = (np.nan_to_num(stack, nan=-np.inf) >= strong).sum(axis=0)
    candidate = strong_count >= min(persist, len(frames) - 1)
    if not candidate.any():
        return frames
    out = []
    removed = 0
    for f in frames:
        d = np.nan_to_num(f.dbz, nan=-np.inf, neginf=-np.inf)
        base = np.where(np.isfinite(d), np.clip(d, 0, None), 0.0).astype("float32")
        context = ndimage.median_filter(base, size=win, mode="nearest")
        bad = candidate & (d >= strong) & (context < context_max)
        if bad.any():
            # grow by 1 px to catch the halo of the artefact
            bad = ndimage.binary_dilation(bad, iterations=1) & (d > context)
            new = f.dbz.copy()
            new[bad] = np.where(context[bad] >= settings.min_dbz, context[bad], -np.inf)
            removed += int(bad.sum())
            out.append(RadarFrame(f.time, new, f.transform, f.crs, f.source, f.snow, f.poh))
        else:
            out.append(f)
    if removed:
        log.info("Clutter filter: removed %d pixels (static strong echoes in weak context)", removed)
    return out


def clamp_reflectivity(f: RadarFrame, max_dbz: float = 70.0) -> RadarFrame:
    d = f.dbz.copy()
    d[np.isfinite(d) & (d > max_dbz)] = max_dbz
    return RadarFrame(f.time, d, f.transform, f.crs, f.source, f.snow, f.poh)


def quality_control(frames: list[RadarFrame]) -> list[RadarFrame]:
    return remove_static_clutter([clamp_reflectivity(f) for f in frames])


def attach_rainviewer_snow(c: httpx.Client, f: RadarFrame) -> RadarFrame:
    """DPC has no precipitation-type product: borrow RainViewer's snow mask (newest complete frame),
    reprojected onto the DPC grid. Best effort: on any failure the frame is returned unchanged."""
    from rasterio.warp import Resampling, reproject

    try:
        rv = RainViewerClient(c).last_frames(3)[-1]
        if rv.snow is None or not rv.snow.any():
            return f
        dst = np.zeros(f.dbz.shape, dtype="float32")
        reproject(rv.snow.astype("float32"), dst, src_transform=rv.transform, src_crs=rv.crs,
                  dst_transform=f.transform, dst_crs=f.crs, resampling=Resampling.average)
        return RadarFrame(f.time, f.dbz, f.transform, f.crs, f.source, dst, f.poh)
    except Exception as exc:  # noqa: BLE001
        log.info("RainViewer snow mask unavailable: %s", exc)
        return f


# ------------------------------------------------------------------ public API
def fetch_latest_frames(n: int | None = None) -> list[RadarFrame]:
    """Last n consecutive frames, oldest first. DPC primary, RainViewer fallback."""
    global LAST_DPC_ERROR
    n = n or settings.n_input_frames
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        try:
            raw = DPCClient(c).last_frames(n)
            raw[-1] = attach_rainviewer_snow(c, raw[-1])
            frames = quality_control([crop_and_downsample(f) for f in raw])
            LAST_DPC_ERROR = None
            log.info("DPC: %d frames, latest %s, grid %s", len(frames), frames[-1].time, frames[-1].dbz.shape)
            return frames
        except Exception as exc:  # noqa: BLE001
            LAST_DPC_ERROR = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("DPC ingest failed (%s); falling back to RainViewer", exc)
        frames = quality_control([crop_and_downsample(f) for f in RainViewerClient(c).last_frames(n)])
        log.info("RainViewer: %d frames, latest %s, grid %s", len(frames), frames[-1].time, frames[-1].dbz.shape)
        return frames


def latest_available_time() -> datetime | None:
    """Cheap probe used by the scheduler to detect a new radar release."""
    with httpx.Client(timeout=15, headers=UA) as c:
        try:
            return DPCClient(c).latest_time()
        except Exception as exc:  # noqa: BLE001
            log.debug("DPC probe failed: %s", exc)
        try:
            m = c.get(settings.rainviewer_api).json()
            return datetime.fromtimestamp(int(m["radar"]["past"][-1]["time"]), tz=timezone.utc)
        except Exception as exc:  # noqa: BLE001
            log.warning("All radar probes failed: %s", exc)
            return None

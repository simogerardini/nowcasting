"""Radar ingestion: DPC (Protezione Civile) national mosaic, RainViewer fallback, CAP alerts."""
from __future__ import annotations

import io
import logging
import math
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


class IngestError(RuntimeError):
    pass


# ------------------------------------------------------------------ helpers
def _floor5(t: datetime) -> datetime:
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def _read_geotiff(content: bytes, t: datetime, source: str) -> RadarFrame:
    with rasterio.MemoryFile(content) as mem, mem.open() as ds:
        arr = ds.read(1).astype("float32")
        nodata = ds.nodata
        if nodata is not None:
            arr[arr == nodata] = np.nan
        arr[(arr < -100) | (arr > 100)] = np.nan  # DPC uses -9999 / large sentinels
        crs = ds.crs or CRS.from_epsg(4326)
        return RadarFrame(t, arr, ds.transform, crs, source)


# ------------------------------------------------------------------ DPC
class DPCClient:
    """Client for the DPC Radar open API (https://radar.protezionecivile.it)."""

    def __init__(self, client: httpx.Client, product: str | None = None):
        self.c = client
        self.base = settings.dpc_api_base.rstrip("/")
        self.product = product or settings.radar_product
        self.cache = Path(settings.radar_cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    def latest_time(self) -> datetime:
        r = self.c.get(f"{self.base}/wide/product/findLastProductByType", params={"type": self.product})
        r.raise_for_status()
        items = r.json().get("lastProducts") or []
        if not items:
            raise IngestError("DPC: no lastProducts")
        return datetime.fromtimestamp(int(items[0]["time"]) / 1000, tz=timezone.utc)

    def _download(self, t: datetime) -> bytes:
        ms = int(t.timestamp() * 1000)
        r = self.c.post(f"{self.base}/wide/product/downloadProduct",
                        json={"productType": self.product, "productDate": ms})
        r.raise_for_status()
        if r.headers.get("content-type", "").startswith("application/json"):
            url = r.json().get("url")
            if not url:
                raise IngestError(f"DPC: no url for {t}")
            r = self.c.get(url)
            r.raise_for_status()
        return r.content

    def frame(self, t: datetime) -> RadarFrame:
        path = self.cache / f"{self.product}_{t:%Y%m%d%H%M}.tif"
        if path.exists():
            content = path.read_bytes()
        else:
            content = self._download(t)
            path.write_bytes(content)
        return _read_geotiff(content, t, "DPC")

    def last_frames(self, n: int) -> list[RadarFrame]:
        latest = self.latest_time()
        frames: list[RadarFrame] = []
        for i in range(n - 1, -1, -1):
            t = latest - timedelta(minutes=settings.timestep_min * i)
            frames.append(self.frame(t))
        self._prune_cache(latest)
        return frames

    def _prune_cache(self, latest: datetime) -> None:
        cutoff = latest - timedelta(hours=3)
        for p in self.cache.glob(f"{self.product}_*.tif"):
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
        past = m["radar"]["past"][-n:]
        if len(past) < n:
            raise IngestError("RainViewer: not enough frames")
        frames = []
        for p in past:
            if p["path"] not in _RV_CACHE:
                _RV_CACHE[p["path"]] = self._frame(m["host"], p)
            frames.append(_RV_CACHE[p["path"]])
        keep = {p["path"] for p in m["radar"]["past"]}
        for k in [k for k in _RV_CACHE if k not in keep]:
            del _RV_CACHE[k]
        return frames

    def _frame(self, host: str, p: dict) -> RadarFrame:
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
        with ThreadPoolExecutor(max_workers=6) as ex:
            tiles = list(ex.map(lambda j: get(*j), jobs))
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
    arr, tr, snow = f.dbz, f.transform, f.snow
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
        arr, tr = out, tr * Affine.scale(factor, factor)
    return RadarFrame(f.time, arr, tr, f.crs, f.source, snow)


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
            out.append(RadarFrame(f.time, new, f.transform, f.crs, f.source, f.snow))
        else:
            out.append(f)
    if removed:
        log.info("Clutter filter: removed %d pixels (static strong echoes in weak context)", removed)
    return out


def clamp_reflectivity(f: RadarFrame, max_dbz: float = 70.0) -> RadarFrame:
    d = f.dbz.copy()
    d[np.isfinite(d) & (d > max_dbz)] = max_dbz
    return RadarFrame(f.time, d, f.transform, f.crs, f.source, f.snow)


def quality_control(frames: list[RadarFrame]) -> list[RadarFrame]:
    return remove_static_clutter([clamp_reflectivity(f) for f in frames])


# ------------------------------------------------------------------ public API
def fetch_latest_frames(n: int | None = None) -> list[RadarFrame]:
    """Last n consecutive frames, oldest first. DPC primary, RainViewer fallback."""
    global LAST_DPC_ERROR
    n = n or settings.n_input_frames
    with httpx.Client(timeout=30, headers=UA, follow_redirects=True) as c:
        try:
            frames = quality_control([crop_and_downsample(f) for f in DPCClient(c).last_frames(n)])
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

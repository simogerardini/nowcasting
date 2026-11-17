"""Radar outside the DPC (Italy) mosaic.

* Europe: EUMETNET OPERA CIRRUS maximum-reflectivity composite (DBZH, 1 km, 5 min, CC BY 4.0), the
  cloud-optimised GeoTIFFs of the ORD 24 h cache (anonymous S3, no key). Only a window around each
  regional domain is read; a 4 km overview of the whole composite feeds the Europe map layer.
* Rest of the world: RainViewer tiles (zoom 7, 10 min) around each domain.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from app.config import get_settings

from .ingest_dpc import UA, IngestError, RadarFrame, RainViewerClient, crop_and_downsample, quality_control

log = logging.getLogger("ingest.regional")
settings = get_settings()

OPERA_NODATA = -9999000.0
_KEY_RE = re.compile(r"OPERA@(\d{8}T\d{4})@0@DBZH\.tiff")
LAST_OPERA_ERROR: str | None = None


@dataclass
class Domain:
    id: str                      # e.g. "R+011+048" (1-degree cell of the centre)
    lon: float
    lat: float
    bbox: tuple[float, float, float, float]   # lon_min, lat_min, lon_max, lat_max
    sub_ids: list = field(default_factory=list)
    viewers: int = 0
    source: str | None = None    # "OPERA" | "RAINVIEWER"


def cell_of(lon: float, lat: float) -> tuple[int, int]:
    return int(round(lon)), int(round(lat))


def cell_id(cx: int, cy: int) -> str:
    return f"R{cx:+04d}{cy:+04d}"


def make_domain(cx: int, cy: int, half_km: float | None = None) -> Domain:
    half = half_km or settings.domain_half_km
    lat = float(max(min(cy, 80), -80))
    dlat = half / 111.0
    dlon = half / (111.0 * max(np.cos(np.radians(lat)), 0.2))
    bbox = (cx - dlon, max(lat - dlat, -84.0), cx + dlon, min(lat + dlat, 84.0))
    return Domain(cell_id(cx, cy), float(cx), lat, bbox)


# ------------------------------------------------------------------ OPERA
class OperaClient:
    def __init__(self, client: httpx.Client):
        self.c = client
        self.base = settings.opera_bucket_url.rstrip("/")
        self.cache = Path(settings.radar_cache_dir) / "opera"
        self.cache.mkdir(parents=True, exist_ok=True)

    def available_times(self, now: datetime | None = None, hours: int = 2) -> list[datetime]:
        """DBZH composite times in the 24 h cache over the last `hours` hours (ascending)."""
        now = now or datetime.now(timezone.utc)
        times: set[datetime] = set()
        for h in range(hours, -1, -1):
            t = now - timedelta(hours=h)
            prefix = f"{t:%Y/%m/%d}/OPERA/COMP/OPERA@{t:%Y%m%dT%H}"
            r = self.c.get(f"{self.base}/", params={"list-type": "2", "prefix": prefix, "max-keys": "1000"})
            r.raise_for_status()
            for m in _KEY_RE.finditer(r.text):
                times.add(datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc))
        return sorted(times)

    def latest_time(self) -> datetime | None:
        ts = self.available_times(hours=1)
        return ts[-1] if ts else None

    def path(self, t: datetime) -> Path:
        p = self.cache / f"OPERA_{t:%Y%m%d%H%M}.tif"
        if not p.exists():
            url = f"{self.base}/{t:%Y/%m/%d}/OPERA/COMP/OPERA@{t:%Y%m%dT%H%M}@0@DBZH.tiff"
            r = self.c.get(url, timeout=60)
            r.raise_for_status()
            tmp = p.with_suffix(".part")
            tmp.write_bytes(r.content)
            tmp.replace(p)
        return p

    def prune(self, keep: int = 10) -> None:
        files = sorted(self.cache.glob("OPERA_*.tif"))
        for p in files[:-keep]:
            p.unlink(missing_ok=True)

    def last_times(self, n: int) -> list[datetime]:
        """Newest run of up to n consecutive 5-min composites."""
        ts = self.available_times()
        if not ts:
            raise IngestError("OPERA: no composites in the cache")
        out = [ts[-1]]
        for t in reversed(ts[:-1]):
            if len(out) == n or out[-1] - t != timedelta(minutes=5):
                break
            out.append(t)
        return list(reversed(out))


def read_window(path: Path, bbox, t: datetime) -> RadarFrame:
    """Window of the OPERA composite covering a lon/lat bbox (native 1 km LAEA grid).
    file: nodata (-9999000) = outside coverage -> NaN; NaN ("undetect") = observed dry -> -inf."""
    with rasterio.open(path) as ds:
        tr = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True)
        lons = np.linspace(bbox[0], bbox[2], 9)
        lats = np.linspace(bbox[1], bbox[3], 9)
        gx, gy = np.meshgrid(lons, lats)
        x, y = tr.transform(gx.ravel(), gy.ravel())
        inv = ~ds.transform
        cols, rows = inv * (np.asarray(x), np.asarray(y))
        c0, c1 = int(np.floor(cols.min())), int(np.ceil(cols.max()))
        r0, r1 = int(np.floor(rows.min())), int(np.ceil(rows.max()))
        win = Window(c0, r0, c1 - c0, r1 - r0)
        arr = ds.read(1, window=win, boundless=True, fill_value=OPERA_NODATA).astype("float32")
        transform = ds.window_transform(win)
        crs = ds.crs
    dry = np.isnan(arr)
    missing = (arr <= -1e5) | (arr > 100)
    arr[dry] = -np.inf
    arr[missing] = np.nan
    return RadarFrame(t, arr, transform, crs, "OPERA")


def opera_coverage(frame: RadarFrame) -> float:
    return float(np.isfinite(frame.dbz).mean() + np.isneginf(frame.dbz).mean())


def europe_overview(path: Path, t: datetime, level: int = 1) -> RadarFrame:
    """Whole composite from a COG overview (level 1 = 4 km): map layer only."""
    with rasterio.open(path, overview_level=level) as ds:
        arr = ds.read(1).astype("float32")
        transform, crs = ds.transform, ds.crs
    dry = np.isnan(arr)
    missing = (arr <= -1e5) | (arr > 100)
    arr[dry] = -np.inf
    arr[missing] = np.nan
    return RadarFrame(t, arr, transform, crs, "OPERA")


# ------------------------------------------------------------------ domain frames
def fetch_domain_frames(c: httpx.Client, d: Domain, opera: OperaClient | None, n: int | None = None
                        ) -> list[RadarFrame]:
    """Last n frames for a regional domain, oldest first. OPERA where it covers the domain,
    RainViewer elsewhere (and as fallback)."""
    global LAST_OPERA_ERROR
    n = n or settings.n_input_frames
    if opera is not None and d.source != "RAINVIEWER":
        try:
            times = opera.last_times(n)
            raw = [read_window(opera.path(t), d.bbox, t) for t in times]
            cov = opera_coverage(raw[-1])
            if cov >= 0.3 and len(raw) >= 3:
                from .ingest_dpc import attach_rainviewer_snow
                raw[-1] = attach_rainviewer_snow(c, raw[-1], bbox=d.bbox)
                d.source = "OPERA"
                LAST_OPERA_ERROR = None
                return quality_control([crop_and_downsample(f, bbox=d.bbox) for f in raw])
            log.info("Domain %s: OPERA coverage %.0f%% -> RainViewer", d.id, cov * 100)
            d.source = "RAINVIEWER"
        except Exception as exc:  # noqa: BLE001
            LAST_OPERA_ERROR = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("OPERA ingest failed for %s (%s); RainViewer fallback", d.id, exc)
    frames = RainViewerClient(c, bbox=d.bbox).last_frames(n)
    return quality_control([crop_and_downsample(f, bbox=d.bbox) for f in frames])


def latest_source_time(c: httpx.Client, d: Domain, opera: OperaClient | None) -> datetime | None:
    try:
        if d.source == "OPERA" and opera is not None:
            return opera.latest_time()
        m = c.get(settings.rainviewer_api, timeout=15).json()
        return datetime.fromtimestamp(int(m["radar"]["past"][-1]["time"]), tz=timezone.utc)
    except Exception as exc:  # noqa: BLE001
        log.info("latest time probe failed for %s: %s", d.id, exc)
        return None


def http_client() -> httpx.Client:
    return httpx.Client(timeout=30, headers=UA, follow_redirects=True)

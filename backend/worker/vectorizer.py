"""Vectorize forecast reflectivity into risk polygons (with uncertainty cone) + radar PNG frames."""
from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import shapely
from PIL import Image
from pyproj import Transformer
from rasterio import features
from rasterio.crs import CRS
from rasterio.warp import Resampling, calculate_default_transform, reproject
from scipy import ndimage
from shapely.geometry import MultiPolygon, Polygon, shape

from app.config import get_settings
from app.models import severity_for_dbz

from .nowcast_engine import NowcastResult, dbz_to_rain
from .storage import CACHE_FRESH, CACHE_IMMUTABLE, Storage

log = logging.getLogger("vectorizer")
settings = get_settings()

METRIC = CRS.from_epsg(3035)   # ETRS89-LAEA Europe, metres
WGS84 = CRS.from_epsg(4326)
MERC = CRS.from_epsg(3857)


def buffer_km(lead_min: int) -> float:
    """Uncertainty cone: 1.5 km + 0.1 km per forecast minute."""
    return 1.5 + 0.1 * lead_min


@dataclass
class RiskPolygon:
    lead_time_minutes: int
    severity_level: str
    threshold_dbz: int
    max_dbz: float
    estimated_rain_mmh: float
    geom: MultiPolygon  # EPSG:4326


def _xform(src: CRS, dst: CRS):
    tr = Transformer.from_crs(src, dst, always_xy=True)

    def f(coords: np.ndarray) -> np.ndarray:
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])
    return f


def _as_multi(g) -> MultiPolygon | None:
    if g.is_empty:
        return None
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    polys = [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    return MultiPolygon(polys) if polys else None


def vectorize_field(dbz: np.ndarray, transform, crs, lead_min: int) -> list[RiskPolygon]:
    to_metric = _xform(crs, METRIC)
    to_wgs = _xform(METRIC, WGS84)
    field = np.where(np.isfinite(dbz), dbz, -np.inf)
    out: list[RiskPolygon] = []
    structure = np.ones((3, 3), dtype=bool)  # 8-connectivity
    buf_m = buffer_km(lead_min) * 1000.0
    for thr in settings.thresholds:
        mask = field >= thr
        if not mask.any():
            continue
        labels, n = ndimage.label(mask, structure=structure)
        sizes = ndimage.sum_labels(mask, labels, index=np.arange(1, n + 1))
        keep = np.zeros(n + 1, dtype=bool)
        keep[1:] = sizes >= settings.min_polygon_pixels
        labels[~keep[labels]] = 0
        if not labels.any():
            continue
        objs = ndimage.find_objects(labels)
        stats: dict[int, tuple[float, float]] = {}
        for idx, sl in enumerate(objs, start=1):
            if sl is None or not keep[idx]:
                continue
            vals = field[sl][labels[sl] == idx]
            stats[idx] = (float(vals.max()), float(np.percentile(vals, 90)))
        parts: dict[int, list] = {}
        for geom, val in features.shapes(labels.astype("int32"), mask=labels > 0, transform=transform):
            parts.setdefault(int(val), []).append(shape(geom))
        severity = severity_for_dbz(thr)
        for idx, polys in parts.items():
            g = shapely.union_all(polys)
            g = shapely.transform(g, to_metric)
            g = g.buffer(buf_m, quad_segs=4).simplify(250, preserve_topology=True)
            g = shapely.transform(g, to_wgs)
            g = _as_multi(shapely.make_valid(g))
            if g is None:
                continue
            max_dbz, p90 = stats[idx]
            out.append(RiskPolygon(lead_min, severity, thr, round(max_dbz, 1),
                                   round(float(dbz_to_rain(np.array(p90))), 1), g))
    return out


def vectorize(result: NowcastResult) -> list[RiskPolygon]:
    polys: list[RiskPolygon] = []
    for i, lead in enumerate(result.lead_minutes):
        polys.extend(vectorize_field(result.forecast_dbz[i], result.transform, result.crs, lead))
    log.info("Vectorized %d risk polygons", len(polys))
    return polys


def to_geojson(result: NowcastResult, polys: list[RiskPolygon]) -> dict:
    feats = []
    for p in polys:
        feats.append({
            "type": "Feature",
            "geometry": shapely.geometry.mapping(shapely.set_precision(p.geom, 1e-5)),
            "properties": {
                "lead_time_minutes": p.lead_time_minutes,
                "valid_time": (result.run_time + timedelta(minutes=p.lead_time_minutes)).isoformat(),
                "severity_level": p.severity_level,
                "threshold_dbz": p.threshold_dbz,
                "max_dbz": p.max_dbz,
                "estimated_rain_mmh": p.estimated_rain_mmh,
            },
        })
    return {"type": "FeatureCollection", "run_timestamp": result.run_time.isoformat(),
            "source": result.source, "features": feats}


# ------------------------------------------------------------------ radar PNG rendering
# NWS-like reflectivity palette (dBZ lower bound -> RGBA)
PALETTE = [
    (15, (4, 233, 231, 120)), (20, (1, 159, 244, 150)), (25, (3, 0, 244, 170)),
    (30, (2, 253, 2, 180)), (35, (1, 197, 1, 190)), (40, (0, 142, 0, 200)),
    (45, (253, 248, 2, 210)), (50, (229, 188, 0, 215)), (55, (253, 149, 0, 220)),
    (60, (253, 0, 0, 225)), (65, (212, 0, 0, 230)), (70, (188, 0, 0, 235)), (75, (248, 0, 253, 240)),
]


def colorize(dbz: np.ndarray) -> np.ndarray:
    rgba = np.zeros(dbz.shape + (4,), dtype=np.uint8)
    f = np.where(np.isfinite(dbz), dbz, -np.inf)
    for lo, color in PALETTE:
        rgba[f >= lo] = color
    return rgba


class MercatorWarper:
    """Pre-computes the Web-Mercator target grid once per run (MapLibre image sources are Mercator)."""

    def __init__(self, shape, transform, crs):
        h, w = shape
        left, top = transform @ (0, 0)
        right, bottom = transform @ (w, h)
        self.src_transform, self.src_crs = transform, crs
        self.dst_transform, self.w, self.h = calculate_default_transform(
            crs, MERC, w, h, left=min(left, right), bottom=min(top, bottom),
            right=max(left, right), top=max(top, bottom))
        tr = Transformer.from_crs(MERC, WGS84, always_xy=True)
        x0, y0 = self.dst_transform @ (0, 0)
        x1, y1 = self.dst_transform @ (self.w, self.h)
        tl, br = tr.transform(x0, y0), tr.transform(x1, y1)
        # MapLibre order: top-left, top-right, bottom-right, bottom-left
        self.coordinates = [[tl[0], tl[1]], [br[0], tl[1]], [br[0], br[1]], [tl[0], br[1]]]

    def warp(self, dbz: np.ndarray) -> np.ndarray:
        dst = np.full((self.h, self.w), np.nan, dtype="float32")
        src = np.where(np.isfinite(dbz), dbz, np.nan).astype("float32")
        reproject(src, dst, src_transform=self.src_transform, src_crs=self.src_crs, src_nodata=np.nan,
                  dst_transform=self.dst_transform, dst_crs=MERC, dst_nodata=np.nan,
                  resampling=Resampling.nearest)
        return dst


def png_bytes(dbz: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(colorize(dbz), "RGBA").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def publish(result: NowcastResult, polys: list[RiskPolygon], storage: Storage, cap_geojson: dict | None = None) -> dict:
    run = result.run_time.strftime("%Y%m%d%H%M")
    warper = MercatorWarper(result.forecast_dbz.shape[1:], result.transform, result.crs)
    frames = []
    for f in result.observed:
        offset = int(round((f.time - result.run_time).total_seconds() / 60))
        key = f"frames/{run}/obs_{abs(offset):02d}.png"
        url = storage.put(key, png_bytes(warper.warp(f.dbz)), "image/png", CACHE_IMMUTABLE)
        frames.append({"time": f.time.isoformat(), "offset_min": offset, "kind": "observed", "url": url})
    for i, lead in enumerate(result.lead_minutes):
        key = f"frames/{run}/fc_{lead:02d}.png"
        url = storage.put(key, png_bytes(warper.warp(result.forecast_dbz[i])), "image/png", CACHE_IMMUTABLE)
        frames.append({"time": (result.run_time + timedelta(minutes=lead)).isoformat(),
                       "offset_min": lead, "kind": "forecast", "url": url})

    geo = to_geojson(result, polys)
    geo_url = storage.put("latest_forecast.geojson", json.dumps(geo, separators=(",", ":")).encode(),
                          "application/geo+json", CACHE_FRESH, compress=True)
    storage.put(f"frames/{run}/forecast.geojson", json.dumps(geo, separators=(",", ":")).encode(),
                "application/geo+json", CACHE_IMMUTABLE, compress=True)
    manifest = {
        "run_timestamp": result.run_time.isoformat(),
        "source": result.source,
        "coordinates": warper.coordinates,
        "frames": frames,
        "forecast_geojson": geo_url,
        "cap_geojson": f"{storage.base_url}/cap_alerts.geojson",
        "legend": [{"dbz": lo, "color": "rgba(%d,%d,%d,%.2f)" % (c[0], c[1], c[2], c[3] / 255)} for lo, c in PALETTE],
    }
    storage.put("latest.json", json.dumps(manifest).encode(), "application/json", "public, max-age=60")
    if cap_geojson is not None:
        storage.put("cap_alerts.geojson", json.dumps(cap_geojson).encode(), "application/geo+json", CACHE_FRESH,
                    compress=True)
    pruned = storage.prune_runs(keep_hours=settings.forecast_retention_hours)
    log.info("Published run %s (%d frames, pruned %d)", run, len(frames), pruned)
    return manifest

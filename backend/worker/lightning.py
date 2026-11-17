"""Lightning from satellites (free, official):

* EUMETSAT MTG Lightning Imager (LI Level 2 Lightning Flashes, collection EO:EUM:DAT:0691, licence
  "NoConditions"): Europe, Africa, Middle East, Atlantic, eastern South America. 10-minute products
  published ~1 min after the period ends; needs a free EUMETSAT Data Store key.
* NOAA GOES-19 / GOES-18 Geostationary Lightning Mapper (GLM L2 LCFA, public AWS bucket, no key):
  the Americas; read only while a regional domain over the Americas is active.
Flashes over Europe and over the active regional domains are stored in PostGIS (`lightning_flashes`)
and published as GeoJSON.
"""
from __future__ import annotations

import io
import json
import logging
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

log = logging.getLogger("lightning")
settings = get_settings()

API = "https://api.eumetsat.int"
COLLECTION = "EO:EUM:DAT:0691"  # LI Lightning Flashes - MTG - 0 degree
BBOX = (-25.0, 30.0, 45.0, 72.0)   # lon_min, lat_min, lon_max, lat_max (Europe)
EXTRA_BBOXES: list[tuple[float, float, float, float]] = []  # active regional domains (set by the pipeline)
EPOCH_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc)

_token: tuple[str, float] | None = None
_seen: dict[str, float] = {}  # product id -> processed at (monotonic)
LAST_STATUS: dict = {}
STATS = {"products": 0, "flashes_full_disc": 0, "last_product": None}


def enabled() -> bool:
    return bool(settings.eumetsat_consumer_key and settings.eumetsat_consumer_secret)


def _get_token(c: httpx.Client) -> str:
    global _token
    if _token and _token[1] > time.monotonic() + 60:
        return _token[0]
    r = c.post(f"{API}/token", data={"grant_type": "client_credentials"},
               auth=(settings.eumetsat_consumer_key, settings.eumetsat_consumer_secret))
    r.raise_for_status()
    d = r.json()
    _token = (d["access_token"], time.monotonic() + float(d.get("expires_in", 3600)))
    return _token[0]


def search_products(c: httpx.Client, since: datetime, until: datetime) -> list[dict]:
    r = c.get(f"{API}/data/search-products/1.0.0/os", params={
        "format": "json", "pi": COLLECTION, "si": 0, "c": 20, "sort": "start,time,0",
        "dtstart": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "dtend": until.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    r.raise_for_status()
    out = []
    for f in r.json().get("features", []):
        links = f.get("properties", {}).get("links", {}).get("data", [])
        if links:
            out.append({"id": f["id"], "href": links[0]["href"], "date": f["properties"].get("date", "")})
    return out


def parse_lfl_netcdf(data: bytes) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]:
    """LFL BODY NetCDF -> (lat, lon, unix_seconds, radiance|None). Scale factors / fill values applied."""
    import netCDF4

    with netCDF4.Dataset("lfl.nc", mode="r", memory=data) as ds:
        names = {v.lower(): v for v in ds.variables}

        def var(*cands):
            for cnd in cands:
                if cnd in names:
                    return ds.variables[names[cnd]]
            return None

        vlat, vlon = var("latitude", "lat", "flash_lat"), var("longitude", "lon", "flash_lon")
        vt = var("flash_time", "flash_time_offset_of_first_event", "time", "flash_start_time")
        if vlat is None or vlon is None or vt is None:
            raise ValueError(f"unexpected LFL variables: {sorted(names)[:30]}")
        lat = np.ma.filled(vlat[:].astype("float64"), np.nan)
        lon = np.ma.filled(vlon[:].astype("float64"), np.nan)
        traw = np.ma.filled(vt[:].astype("float64"), np.nan)
        units = getattr(vt, "units", "seconds since 2000-01-01 00:00:00")
        if "since" in units:
            dts = netCDF4.num2date(traw, units, only_use_cftime_datetimes=False, only_use_python_datetimes=True)
            secs = np.array([d.replace(tzinfo=timezone.utc).timestamp() if d is not None else np.nan
                             for d in np.atleast_1d(dts)], dtype="float64")
        else:
            secs = EPOCH_2000.timestamp() + traw
        vr = var("radiance", "flash_radiance", "flash_energy")
        rad = np.ma.filled(vr[:].astype("float64"), np.nan) if vr is not None else None
    return lat, lon, secs, rad


def _flashes_from_zip(content: bytes) -> list[tuple[float, float, float, float | None]]:
    out = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for name in z.namelist():
            if not name.endswith(".nc") or "TRAIL" in name.upper():
                continue
            lat, lon, secs, rad = parse_lfl_netcdf(z.read(name))
            STATS["flashes_full_disc"] += int(np.isfinite(lat).sum())
            out.extend(_select(lat, lon, secs, rad, [BBOX, *EXTRA_BBOXES]))
    return out


def _select(lat, lon, secs, rad, boxes) -> list[tuple[float, float, float, float | None]]:
    ok = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(secs)
    inside = np.zeros(lat.shape, dtype=bool)
    for b in boxes:
        inside |= (lon >= b[0]) & (lon <= b[2]) & (lat >= b[1]) & (lat <= b[3])
    m = ok & inside
    return [(float(lat[i]), float(lon[i]), float(secs[i]),
             float(rad[i]) if rad is not None and np.isfinite(rad[i]) else None) for i in np.where(m)[0]]


def fetch_new_flashes(lookback_min: int = 40) -> list[tuple[float, float, float, float | None]]:
    """Download LFL products not processed yet (last `lookback_min`), return flashes of interest."""
    now = datetime.now(timezone.utc)
    flashes: list = []
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        products = search_products(c, now - timedelta(minutes=lookback_min), now)
        todo = [p for p in products if p["id"] not in _seen]
        if not todo:
            return []
        tok = _get_token(c)
        for p in todo:
            r = c.get(p["href"], headers={"Authorization": f"Bearer {tok}"})
            r.raise_for_status()
            fl = _flashes_from_zip(r.content)
            flashes.extend(fl)
            _seen[p["id"]] = time.monotonic()
            STATS["products"] += 1
            STATS["last_product"] = p["date"]
            log.info("LI product %s: %d flashes over Europe / active regions", p["date"], len(fl))
    for k in [k for k, v in _seen.items() if v < time.monotonic() - 6 * 3600]:
        del _seen[k]
    return flashes


async def store_flashes(session: AsyncSession, flashes: list) -> None:
    if flashes:
        await session.execute(text("""
            INSERT INTO lightning_flashes (flash_time, radiance, geom)
            VALUES (to_timestamp(:t), :r, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))"""),
            [{"t": t, "r": r, "lon": lon, "lat": lat} for lat, lon, t, r in flashes])
    await session.execute(text("DELETE FROM lightning_flashes WHERE flash_time < NOW() - INTERVAL '3 hours'"))
    await session.commit()


async def lightning_geojson(session: AsyncSession, minutes: int = 60) -> dict:
    rows = (await session.execute(text("""
        SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, flash_time FROM lightning_flashes
        WHERE flash_time > NOW() - make_interval(mins => CAST(:m AS INT))
        ORDER BY flash_time DESC LIMIT 20000"""), {"m": minutes})).mappings().all()
    now = datetime.now(timezone.utc)
    return {"type": "FeatureCollection", "generated": now.isoformat(), "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(r["lon"], 4), round(r["lat"], 4)]},
        "properties": {"age_min": round((now - r["flash_time"]).total_seconds() / 60, 1)},
    } for r in rows]}


# ------------------------------------------------------------------ GOES GLM (Americas)
GLM_SATS = {"G19": "noaa-goes19", "G18": "noaa-goes18"}   # GOES-East (75.2 W), GOES-West (137.2 W)
_glm_seen: dict[str, float] = {}


def glm_boxes() -> dict[str, list]:
    """Active regional domains over the Americas, per satellite (West of 110 W -> GOES-West)."""
    out: dict[str, list] = {"G19": [], "G18": []}
    for b in EXTRA_BBOXES:
        lon = 0.5 * (b[0] + b[2])
        if -180 <= lon <= -25 and -60 <= 0.5 * (b[1] + b[3]) <= 60:
            out["G18" if lon < -110 else "G19"].append(b)
    return {k: v for k, v in out.items() if v}


def fetch_glm_flashes(lookback_min: int = 4) -> list[tuple[float, float, float, float | None]]:
    """GLM L2 LCFA files (one per 20 s) of the last minutes not processed yet, flashes in active boxes."""
    import re as _re

    boxes = glm_boxes() if settings.glm_enabled else {}
    if not boxes:
        return []
    now = datetime.now(timezone.utc)
    flashes: list = []
    with httpx.Client(timeout=60) as c:
        for sat, bucket in ((s, GLM_SATS[s]) for s in boxes):
            keys = []
            for h in (now - timedelta(hours=1), now):
                prefix = f"GLM-L2-LCFA/{h:%Y}/{h:%j}/{h:%H}/"
                r = c.get(f"https://{bucket}.s3.amazonaws.com/", params={"list-type": "2", "prefix": prefix})
                r.raise_for_status()
                keys += _re.findall(r"<Key>([^<]+\.nc)</Key>", r.text)
            for k in keys:
                m = _re.search(r"_s(\d{4})(\d{3})(\d{2})(\d{2})(\d{2})", k)
                if not m or k in _glm_seen:
                    continue
                start = datetime.strptime("".join(m.groups()), "%Y%j%H%M%S").replace(tzinfo=timezone.utc)
                if start < now - timedelta(minutes=lookback_min):
                    _glm_seen[k] = time.monotonic()
                    continue
                r = c.get(f"https://{bucket}.s3.amazonaws.com/{k}")
                r.raise_for_status()
                lat, lon, secs, energy = parse_lfl_netcdf(r.content)
                flashes.extend(_select(lat, lon, secs, energy, boxes[sat]))
                _glm_seen[k] = time.monotonic()
                STATS["glm_files"] = STATS.get("glm_files", 0) + 1
    for k in [k for k, v in _glm_seen.items() if v < time.monotonic() - 3 * 3600]:
        del _glm_seen[k]
    if flashes:
        log.info("GLM: %d flashes in active American regions", len(flashes))
    return flashes


def save_status(**kw) -> None:
    LAST_STATUS.update(kw)
    try:
        p = Path(settings.radar_cache_dir).parent / "lightning_status.json"
        p.write_text(json.dumps(LAST_STATUS, default=str))
    except OSError:
        pass

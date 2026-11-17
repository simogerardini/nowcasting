"""Official warnings (CAP): MeteoAlarm (38 European countries) and the US National Weather Service.

Which feeds are read: Italy always, plus the countries where the app is used (subscribers and recent
viewers), so the worker never downloads all of Europe every 10 minutes.

Geometry of each warning:
  1. CAP polygons in the warning, when present (UK, NWS, ...)
  2. geocodes looked up in `cap_areas` (PostGIS): MeteoAlarm EMMA_ID areas (bundled, loaded once),
     NWS forecast/county zones (fetched once from api.weather.gov and cached)
  3. Italian region name (areaDesc) -> ISTAT boundaries (data/it_regions.geojson), last resort
Warnings without a resolvable geometry are skipped.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events import cap_event_type

log = logging.getLogger("cap")
settings = get_settings()

DATA = Path(__file__).with_name("data")
UA = {"User-Agent": "noctalix-nowcast/1.0 (weather alerts PWA)"}

# ISO 3166 alpha-2 -> MeteoAlarm feed slug
METEOALARM_SLUGS = {
    "AT": "austria", "BE": "belgium", "BA": "bosnia-herzegovina", "BG": "bulgaria", "HR": "croatia",
    "CY": "cyprus", "CZ": "czechia", "DK": "denmark", "EE": "estonia", "FI": "finland", "FR": "france",
    "DE": "germany", "GR": "greece", "HU": "hungary", "IS": "iceland", "IE": "ireland", "IL": "israel",
    "IT": "italy", "LV": "latvia", "LT": "lithuania", "LU": "luxembourg", "MT": "malta", "MD": "moldova",
    "ME": "montenegro", "NL": "netherlands", "MK": "republic-of-north-macedonia", "NO": "norway",
    "PL": "poland", "PT": "portugal", "RO": "romania", "RS": "serbia", "SK": "slovakia", "SI": "slovenia",
    "ES": "spain", "SE": "sweden", "CH": "switzerland", "UA": "ukraine", "GB": "united-kingdom",
}
# countries whose warning areas are not in the EMMA file (their warnings carry polygons): rough boxes
COUNTRY_BOXES = {
    "GB": (-8.7, 49.8, 1.8, 60.9), "CH": (5.9, 45.8, 10.5, 47.8), "UA": (22.1, 44.3, 40.2, 52.4),
}
US_BOX = (-179.9, 17.5, -64.0, 71.5)   # CONUS, Alaska, Hawaii, Puerto Rico
LEVEL_SEVERITY = {"2": "Moderate", "3": "Severe", "4": "Extreme"}  # MeteoAlarm awareness levels


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_cap_polygon(s: str) -> Polygon | None:
    """CAP polygon: space-separated 'lat,lon' pairs, closed ring."""
    pts = []
    for pair in s.split():
        try:
            lat, lon = (float(x) for x in pair.split(",")[:2])
        except ValueError:
            return None
        pts.append((lon, lat))
    if len(pts) < 4:
        return None
    p = Polygon(pts)
    return p if p.is_valid else p.buffer(0)


def _as_multi(g) -> MultiPolygon | None:
    if g is None or g.is_empty:
        return None
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    polys = [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
    return MultiPolygon(polys) if polys else None


_REGIONS: dict[str, object] | None = None


def _region_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())[:6]


def region_geometry(area_desc: str | None):
    """Italian region (MeteoAlarm areaDesc, e.g. 'Emilia e Romagna') -> boundary geometry."""
    global _REGIONS
    if _REGIONS is None:
        _REGIONS = {f["properties"]["code"]: shape(f["geometry"])
                    for f in json.loads((DATA / "it_regions.geojson").read_text())["features"]}
    return _REGIONS.get(_region_key(area_desc or ""))


# ------------------------------------------------------------------ areas table
async def ensure_areas(session: AsyncSession) -> None:
    """Load the bundled MeteoAlarm EMMA_ID areas into cap_areas once."""
    n = (await session.execute(text("SELECT count(*) FROM cap_areas WHERE code !~ '^NWS:'"))).scalar()
    path = DATA / "emma_areas.geojson.gz"
    if n and n >= 1000 or not path.exists():
        return
    feats = json.loads(gzip.decompress(path.read_bytes()))["features"]
    await session.execute(text("""
        INSERT INTO cap_areas (code, country, name, geom)
        VALUES (:code, :country, :name, ST_Multi(ST_CollectionExtract(ST_MakeValid(
                ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326)), 3)))
        ON CONFLICT (code) DO NOTHING"""),
        [{"code": f["properties"]["code"], "country": f["properties"].get("country"),
          "name": f["properties"].get("name"), "g": json.dumps(f["geometry"])} for f in feats])
    await session.commit()
    log.info("Loaded %d MeteoAlarm warning areas", len(feats))


async def demand_points(session: AsyncSession) -> dict:
    """Where alerts are needed: subscribers + recent viewers -> MeteoAlarm countries and US points."""
    rows = (await session.execute(text("""
        SELECT ST_X(location) AS lon, ST_Y(location) AS lat FROM user_subscriptions
        UNION ALL
        SELECT lon, lat FROM coverage_demand
        WHERE last_seen > NOW() - make_interval(mins => CAST(:m AS INT))"""),
        {"m": max(settings.demand_ttl_min, 60)})).mappings().all()
    pts = [(float(r["lon"]), float(r["lat"])) for r in rows]
    countries = {"IT"}
    if pts:
        found = (await session.execute(text("""
            SELECT DISTINCT a.country FROM unnest(CAST(:lons AS float8[]), CAST(:lats AS float8[])) AS p(lon, lat)
            JOIN cap_areas a ON ST_Intersects(a.geom, ST_SetSRID(ST_MakePoint(p.lon, p.lat), 4326))
            WHERE a.code !~ '^NWS:' AND a.country IS NOT NULL"""),
            {"lons": [p[0] for p in pts], "lats": [p[1] for p in pts]})).scalars().all()
        countries |= {c for c in found if c in METEOALARM_SLUGS}
    for iso, b in COUNTRY_BOXES.items():
        if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for x, y in pts):
            countries.add(iso)
    us = sorted({(round(x, 1), round(y, 1)) for x, y in pts
                 if US_BOX[0] <= x <= US_BOX[2] and US_BOX[1] <= y <= US_BOX[3]})
    return {"countries": sorted(countries), "us_points": us[:50]}


# ------------------------------------------------------------------ MeteoAlarm
def _param(info: dict, name: str) -> str | None:
    for p in info.get("parameter") or []:
        if p.get("valueName") == name:
            return p.get("value")
    return None


def _pick_info(infos: list[dict]) -> dict | None:
    if not infos:
        return None
    for lang in ("it", "en"):
        for i in infos:
            if (i.get("language") or "").lower().startswith(lang):
                return i
    return infos[0]


def parse_meteoalarm(data: dict, iso: str) -> list[dict]:
    out = []
    for w in data.get("warnings", []):
        a = w.get("alert") or {}
        if a.get("msgType") == "Cancel" or a.get("status", "Actual") != "Actual":
            continue
        info = _pick_info(a.get("info") or [])
        if not info:
            continue
        level = (_param(info, "awareness_level") or "").split(";")[0].strip()
        severity = LEVEL_SEVERITY.get(level) or info.get("severity") or "Unknown"
        if level == "1":
            continue  # green: no warning
        atype = _param(info, "awareness_type") or ""
        etype = cap_event_type(atype) or cap_event_type(info.get("event") or "")
        polys, codes, descs = [], [], []
        for ar in info.get("area") or []:
            descs.append(ar.get("areaDesc") or "")
            for ptxt in ar.get("polygon") or []:
                p = parse_cap_polygon(ptxt)
                if p is not None and not p.is_empty:
                    polys.append(p)
            for gc in ar.get("geocode") or []:
                if gc.get("valueName") == "EMMA_ID" and gc.get("value"):
                    codes.append(gc["value"].strip())
        geom = _as_multi(unary_union(polys)) if polys else None
        if geom is None and not codes and iso == "IT":
            geom = _as_multi(region_geometry(descs[0] if descs else None))
        if geom is None and not codes:
            continue
        area = ", ".join(d for d in descs[:3] if d) + (" …" if len(descs) > 3 else "")
        out.append({
            "identifier": a.get("identifier") or w.get("uuid"),
            "event": info.get("event") or atype or "Warning",
            "event_type": etype,
            "severity": severity,
            "headline": info.get("headline"),
            "description": (info.get("description") or "")[:2000] or None,
            "area_desc": area[:300],
            "onset": _ts(info.get("onset") or info.get("effective")),
            "expires": _ts(info.get("expires")),
            "geom": geom, "codes": codes,
            "provider": "MeteoAlarm / Protezione Civile" if iso == "IT" else "MeteoAlarm",
            "country": iso,
        })
    return out


# ------------------------------------------------------------------ NWS (USA)
def parse_nws(data: dict) -> list[dict]:
    out = []
    for f in data.get("features", []):
        p = f.get("properties") or {}
        if p.get("status") != "Actual" or p.get("messageType") == "Cancel":
            continue
        geom = _as_multi(shape(f["geometry"])) if f.get("geometry") else None
        codes = [] if geom else ["NWS:" + z.rstrip("/").split("/")[-1] for z in p.get("affectedZones") or []]
        if geom is None and not codes:
            continue
        out.append({
            "identifier": p.get("id") or f.get("id"),
            "event": p.get("event") or "Alert",
            "event_type": cap_event_type(p.get("event") or ""),
            "severity": p.get("severity") or "Unknown",
            "headline": p.get("headline"),
            "description": (p.get("description") or "")[:2000] or None,
            "area_desc": (p.get("areaDesc") or "")[:300],
            "onset": _ts(p.get("onset") or p.get("effective")),
            "expires": _ts(p.get("ends") or p.get("expires")),
            "geom": geom, "codes": codes, "zone_urls": p.get("affectedZones") or [],
            "provider": "NWS (NOAA)", "country": "US",
        })
    return out


def fetch_cap_alerts(demand: dict | None = None) -> list[dict]:
    demand = demand or {"countries": ["IT"], "us_points": []}
    out: dict[str, dict] = {}
    with httpx.Client(timeout=40, follow_redirects=True, headers=UA) as c:
        for iso in demand["countries"]:
            slug = METEOALARM_SLUGS.get(iso)
            if not slug:
                continue
            try:
                r = c.get(settings.meteoalarm_api.format(slug=slug))
                r.raise_for_status()
                for a in parse_meteoalarm(r.json(), iso):
                    out[a["identifier"]] = a
            except Exception as exc:  # noqa: BLE001
                log.warning("MeteoAlarm %s failed: %s", iso, exc)
        if settings.nws_enabled:
            for lon, lat in demand["us_points"]:
                try:
                    r = c.get("https://api.weather.gov/alerts/active", params={"point": f"{lat:.4f},{lon:.4f}"},
                              headers={"Accept": "application/geo+json"})
                    r.raise_for_status()
                    for a in parse_nws(r.json()):
                        out[a["identifier"]] = a
                except Exception as exc:  # noqa: BLE001
                    log.warning("NWS %.1f,%.1f failed: %s", lat, lon, exc)
    now = datetime.now(timezone.utc)
    out = {k: a for k, a in out.items() if a["expires"] is None or a["expires"] > now}
    log.info("CAP: %d active warnings (%s%s)", len(out), ",".join(demand["countries"]),
             f", US points {len(demand['us_points'])}" if demand["us_points"] else "")
    return list(out.values())


async def resolve_nws_zones(session: AsyncSession, alerts: list[dict]) -> None:
    """Fetch (once) the geometry of NWS zones referenced by alerts without polygons."""
    urls = {u.rstrip("/").split("/")[-1]: u for a in alerts for u in a.get("zone_urls") or [] if not a.get("geom")}
    if not urls:
        return
    have = set((await session.execute(text("SELECT code FROM cap_areas WHERE code = ANY(:c)"),
                                      {"c": ["NWS:" + k for k in urls]})).scalars().all())
    todo = [(k, u) for k, u in urls.items() if "NWS:" + k not in have][:60]
    if not todo:
        return
    async with httpx.AsyncClient(timeout=30, headers={**UA, "Accept": "application/geo+json"}) as c:
        for k, u in todo:
            try:
                r = await c.get(u)
                r.raise_for_status()
                f = r.json()
                if not f.get("geometry"):
                    continue
                await session.execute(text("""
                    INSERT INTO cap_areas (code, country, name, geom)
                    VALUES (:code, 'US', :name, ST_Multi(ST_CollectionExtract(ST_MakeValid(ST_SimplifyPreserveTopology(
                            ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326), 0.005)), 3)))
                    ON CONFLICT (code) DO NOTHING"""),
                    {"code": "NWS:" + k, "name": (f.get("properties") or {}).get("name"),
                     "g": json.dumps(f["geometry"])})
            except Exception as exc:  # noqa: BLE001
                log.info("NWS zone %s: %s", k, exc)
    await session.commit()


# ------------------------------------------------------------------ storage
_COLS = "identifier, event, event_type, severity, headline, description, area_desc, onset, expires, provider, country"
_VALS = ":identifier, :event, :event_type, :severity, :headline, :description, :area_desc, :onset, :expires, :provider, :country"
_UPSERT = """ON CONFLICT (identifier) DO UPDATE SET severity = EXCLUDED.severity, expires = EXCLUDED.expires,
    event_type = EXCLUDED.event_type, headline = EXCLUDED.headline, description = EXCLUDED.description,
    geom = EXCLUDED.geom, fetched_at = NOW()"""


async def store_cap_alerts(session: AsyncSession, alerts: list[dict]) -> None:
    stored = 0
    for a in alerts:
        params = {k: a.get(k) for k in _COLS.replace(" ", "").split(",")}
        if a.get("geom") is not None:
            await session.execute(text(f"""
                INSERT INTO cap_alerts ({_COLS}, geom)
                VALUES ({_VALS}, ST_Multi(ST_GeomFromText(:wkt, 4326))) {_UPSERT}"""),
                {**params, "wkt": a["geom"].wkt})
            stored += 1
        elif a.get("codes"):
            res = await session.execute(text(f"""
                INSERT INTO cap_alerts ({_COLS}, geom)
                SELECT {_VALS}, ST_Multi(ST_CollectionExtract(ST_Union(geom), 3))
                FROM cap_areas WHERE code = ANY(:codes) HAVING count(*) > 0 {_UPSERT}"""),
                {**params, "codes": a["codes"]})
            stored += res.rowcount or 0
    await session.execute(text("DELETE FROM cap_alerts WHERE expires < NOW() - INTERVAL '1 day'"))
    await session.commit()
    log.info("CAP: %d warnings stored with geometry", stored)


async def cap_geojson(session: AsyncSession) -> dict:
    rows = (await session.execute(text("""
        SELECT identifier, event, event_type, severity, headline, area_desc, onset, expires, provider,
               ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, 0.01), 3) AS g
        FROM cap_alerts WHERE (expires IS NULL OR expires > NOW()) AND severity IN ('Moderate', 'Severe', 'Extreme')
        """))).mappings().all()
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": json.loads(r["g"]),
        "properties": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "g"},
    } for r in rows]}

"""CAP (Common Alerting Protocol) ingestion: MeteoAlarm / Protezione Civile feeds -> cap_alerts (PostGIS).

Geometry resolution order for each alert:
  1. <cap:polygon> / <polygon> in the Atom entry or in the linked CAP document ("lat,lon lat,lon ...")
  2. <geocode> (EMMA_ID / NUTS) looked up in an optional geocode GeoJSON (CAP_GEOCODES_URL or file)
  3. <cap:areaDesc> = Italian region name -> bundled ISTAT region boundaries (data/it_regions.geojson)
Alerts with no resolvable geometry are skipped (logged).
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import httpx
from defusedxml import ElementTree as ET
from shapely.geometry import MultiPolygon, Polygon, shape
from shapely.ops import unary_union
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events import cap_event_type

log = logging.getLogger("cap")
settings = get_settings()

NS = {"a": "http://www.w3.org/2005/Atom", "cap": "urn:oasis:names:tc:emergency:cap:1.2"}
GEOCODES_URL = os.environ.get("CAP_GEOCODES_URL", "")
GEOCODES_FILE = Path(os.environ.get("CAP_GEOCODES_FILE", "/data/cap_geocodes.geojson"))
_geocodes: dict[str, object] | None = None


def _txt(el, path: str) -> str | None:
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


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


def _load_geocodes(client: httpx.Client) -> dict[str, object]:
    global _geocodes
    if _geocodes is not None:
        return _geocodes
    _geocodes = {}
    try:
        if not GEOCODES_FILE.exists() and GEOCODES_URL:
            r = client.get(GEOCODES_URL)
            r.raise_for_status()
            GEOCODES_FILE.parent.mkdir(parents=True, exist_ok=True)
            GEOCODES_FILE.write_bytes(r.content)
        if GEOCODES_FILE.exists():
            for f in json.loads(GEOCODES_FILE.read_text()).get("features", []):
                props = f.get("properties", {})
                code = props.get("code") or props.get("EMMA_ID") or props.get("geocode")
                if code and f.get("geometry"):
                    _geocodes[str(code)] = shape(f["geometry"])
            log.info("Loaded %d CAP geocode areas", len(_geocodes))
    except Exception as exc:  # noqa: BLE001
        log.warning("CAP geocodes unavailable: %s", exc)
    return _geocodes


_REGIONS: dict[str, object] | None = None


def _region_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())[:6]


def region_geometry(area_desc: str | None):
    """Italian region (MeteoAlarm areaDesc, e.g. 'Emilia e Romagna') -> boundary geometry."""
    global _REGIONS
    if _REGIONS is None:
        path = Path(__file__).with_name("data") / "it_regions.geojson"
        _REGIONS = {f["properties"]["code"]: shape(f["geometry"])
                    for f in json.loads(path.read_text())["features"]}
    return _REGIONS.get(_region_key(area_desc or ""))


def _area_geometry(el, client: httpx.Client) -> MultiPolygon | None:
    polys = [parse_cap_polygon(p.text) for p in el.iter() if p.tag.endswith("polygon") and p.text]
    polys = [p for p in polys if p is not None and not p.is_empty]
    if not polys:
        codes = []
        for gc in el.iter():
            if gc.tag.endswith("geocode"):
                val = next((c.text for c in gc if c.tag.endswith("value") and c.text), None)
                if val:
                    codes.append(val.strip())
        lookup = _load_geocodes(client) if codes else {}
        polys = [lookup[c] for c in codes if c in lookup]
    if not polys:
        return None
    g = unary_union(polys)
    if isinstance(g, Polygon):
        return MultiPolygon([g])
    if isinstance(g, MultiPolygon):
        return g
    return None


def fetch_cap_alerts() -> list[dict]:
    out = []
    with httpx.Client(timeout=30, follow_redirects=True,
                      headers={"User-Agent": "noctalix-nowcast/1.0"}) as c:
        r = c.get(settings.cap_feed_url)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for e in root.findall("a:entry", NS):
            ident = _txt(e, "cap:identifier") or _txt(e, "a:id")
            if not ident:
                continue
            geom = _area_geometry(e, c)
            if geom is None:
                link = e.find("a:link[@type='application/cap+xml']", NS)
                if link is None:
                    link = e.find("a:link", NS)
                href = link.get("href") if link is not None else None
                if href:
                    try:
                        doc = c.get(href)
                        if doc.status_code == 200 and doc.content.lstrip().startswith(b"<"):
                            geom = _area_geometry(ET.fromstring(doc.content), c)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("CAP doc fetch failed %s: %s", href, exc)
            if geom is None:
                reg = region_geometry(_txt(e, "cap:areaDesc"))
                if reg is not None:
                    geom = reg if isinstance(reg, MultiPolygon) else MultiPolygon([reg]) if isinstance(reg, Polygon) else None
            if geom is None:
                log.debug("CAP %s: no geometry, skipped", ident)
                continue
            out.append({
                "identifier": ident,
                "event": _txt(e, "cap:event") or _txt(e, "a:title") or "Allerta",
                "event_type": cap_event_type(_txt(e, "cap:event") or _txt(e, "a:title") or ""),
                "severity": _txt(e, "cap:severity") or "Unknown",
                "headline": _txt(e, "a:title"),
                "description": _txt(e, "a:summary"),
                "area_desc": _txt(e, "cap:areaDesc"),
                "onset": _ts(_txt(e, "cap:onset") or _txt(e, "cap:effective")),
                "expires": _ts(_txt(e, "cap:expires")),
                "geom": geom,
            })
    log.info("CAP: %d alerts with geometry", len(out))
    return out


async def store_cap_alerts(session: AsyncSession, alerts: list[dict]) -> None:
    for a in alerts:
        await session.execute(text("""
            INSERT INTO cap_alerts (identifier, event, event_type, severity, headline, description, area_desc,
                                    onset, expires, geom)
            VALUES (:identifier, :event, :event_type, :severity, :headline, :description, :area_desc, :onset, :expires,
                    ST_Multi(ST_GeomFromText(:wkt, 4326)))
            ON CONFLICT (identifier) DO UPDATE SET severity = EXCLUDED.severity, expires = EXCLUDED.expires,
                event_type = EXCLUDED.event_type,
                headline = EXCLUDED.headline, description = EXCLUDED.description, fetched_at = NOW()
        """), {**{k: v for k, v in a.items() if k != "geom"}, "wkt": a["geom"].wkt})
    await session.execute(text("DELETE FROM cap_alerts WHERE expires < NOW() - INTERVAL '1 day'"))
    await session.commit()


async def cap_geojson(session: AsyncSession) -> dict:
    rows = (await session.execute(text("""
        SELECT identifier, event, event_type, severity, headline, area_desc, onset, expires, ST_AsGeoJSON(geom, 4) AS g
        FROM cap_alerts WHERE expires IS NULL OR expires > NOW()"""))).mappings().all()
    return {"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": json.loads(r["g"]),
        "properties": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items() if k != "g"},
    } for r in rows]}

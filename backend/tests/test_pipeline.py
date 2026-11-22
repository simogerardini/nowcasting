from datetime import datetime, time, timedelta, timezone

import numpy as np
import pytest
from rasterio.crs import CRS
from rasterio.transform import from_origin

from app.models import severity_for_dbz
from worker.ingest_dpc import RadarFrame
from worker.notifier import in_quiet_hours, should_send
from worker.nowcast_engine import dbz_to_rain, rain_to_dbz, run_nowcast
from worker.vectorizer import MercatorWarper, buffer_km, png_bytes, vectorize


def synthetic_frames(n=6, shape=(300, 400), speed=3, x0=100, t0=None):
    """Gaussian convective cell (peak 60 dBZ) drifting east `speed` px / 5 min over N Italy."""
    t0 = t0 or datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    tr = from_origin(9.0, 46.0, 0.01, 0.01)
    yy, xx = np.mgrid[0:shape[0], 0:shape[1]]
    frames = []
    for i in range(n):
        cx, cy = x0 + i * speed, 150
        d2 = (xx - cx) ** 2 + (yy - cy) ** 2
        dbz = 10 + 50 * np.exp(-d2 / (2 * 18 ** 2))
        dbz += np.random.default_rng(i).normal(0, 0.5, shape)
        frames.append(RadarFrame(t0 + timedelta(minutes=5 * i), dbz.astype("float32"), tr,
                                 CRS.from_epsg(4326), "TEST"))
    return frames


def test_zr_roundtrip():
    r = np.array([1.0, 10.0, 50.0])
    assert np.allclose(dbz_to_rain(rain_to_dbz(r)), r)
    assert abs(float(rain_to_dbz(np.array([1.0]))[0]) - 23.0) < 0.1  # 200 R^1.6 at 1 mm/h


def test_severity():
    assert severity_for_dbz(30) is None
    assert severity_for_dbz(35) == "MODERATE"
    assert severity_for_dbz(50) == "SEVERE"
    assert severity_for_dbz(58) == "EXTREME"
    assert buffer_km(60) == pytest.approx(7.5)


def test_nowcast_and_vectorize():
    frames = synthetic_frames()
    res = run_nowcast(frames)
    assert res.forecast_dbz.shape == (12, 300, 400)
    assert res.lead_minutes[-1] == 60
    # cell must keep moving east
    last_obs_x = np.nanargmax(np.nanmax(frames[-1].dbz, axis=0))
    fc_x = np.nanargmax(np.nanmax(np.where(np.isfinite(res.forecast_dbz[5]), res.forecast_dbz[5], -99), axis=0))
    assert fc_x > last_obs_x
    polys = vectorize(res)
    sev = {p.severity_level for p in polys}
    assert "MODERATE" in sev and "SEVERE" in sev
    ev = {p.event_type for p in polys}
    assert {"RAIN_START", "HEAVY_RAIN", "CLOUDBURST"} <= ev
    assert any(p.lead_time_minutes == 0 and p.event_type == "RAIN_START" for p in polys)
    leads = {p.lead_time_minutes for p in polys}
    assert 5 in leads
    for p in polys:
        assert p.geom.is_valid
        minx, miny, maxx, maxy = p.geom.bounds
        assert 8.5 < minx < maxx < 13.5 and 42.5 < miny < maxy < 46.5
    # buffer grows with lead time
    a5 = max(p.geom.area for p in polys if p.lead_time_minutes == 5 and p.threshold_dbz == 35)
    a60 = [p.geom.area for p in polys if p.lead_time_minutes == 60 and p.threshold_dbz == 35]
    if a60:
        assert max(a60) > a5 * 0.8


def test_render():
    f = synthetic_frames(n=1)[0]
    w = MercatorWarper(f.dbz.shape, f.transform, f.crs)
    assert len(w.coordinates) == 4
    assert png_bytes(w.warp(f.dbz))[:4] == b"\x89PNG"


def test_quiet_hours():
    now = datetime(2026, 7, 1, 22, 30, tzinfo=timezone.utc)  # 00:30 Europe/Rome (CEST)
    assert in_quiet_hours(time(23, 0), time(7, 0), "Europe/Rome", now)
    assert not in_quiet_hours(time(1, 0), time(7, 0), "Europe/Rome", now)
    assert not in_quiet_hours(None, None, "Europe/Rome", now)
    assert not should_send("MODERATE", True) and not should_send("INFO", True)
    assert should_send("SEVERE", True) and should_send("EXTREME", True) and should_send("MODERATE", False)


def test_ten_minute_input_cadence():
    """RainViewer cadence (10 min) must still yield 12 x 5-min lead times."""
    frames = synthetic_frames()
    for i, f in enumerate(frames):
        f.time = frames[0].time + timedelta(minutes=10 * i)
    res = run_nowcast(frames)
    assert res.forecast_dbz.shape[0] == 12 and res.lead_minutes == [5 * (i + 1) for i in range(12)]


def test_crop_and_downsample():
    from worker.ingest_dpc import crop_and_downsample
    f = synthetic_frames(n=1)[0]
    f.dbz[0:2, 0:2] = np.nan
    g = crop_and_downsample(f, bbox=(9.0, 43.0, 13.0, 46.0), factor=2)
    assert g.dbz.shape == (150, 200)
    assert np.isnan(g.dbz[0, 0])
    assert abs(np.nanmax(g.dbz) - np.nanmax(f.dbz)) < 1.0


def test_snow_polygons():
    frames = synthetic_frames()
    for f in frames:
        f.snow = np.ones(f.dbz.shape, dtype="float32")
    res = run_nowcast(frames)
    assert res.snow_fc is not None and res.snow_fc.shape == res.forecast_dbz.shape
    polys = vectorize(res)
    ev = {p.event_type for p in polys}
    assert "SNOW" in ev and "RAIN_START" not in ev


def test_cap_event_mapping_and_regions():
    from app.events import cap_event_type
    from worker.cap import region_geometry
    assert cap_event_type("Yellow Thunderstorm Warning") == "CAP_THUNDERSTORM"
    assert cap_event_type("Orange Rain-Flood Warning") == "CAP_RAIN"
    assert cap_event_type("Yellow Snow-Ice Warning") == "CAP_SNOW_ICE"
    assert cap_event_type("Red High-Temperature Warning") == "CAP_HEAT"
    g = region_geometry("Emilia e Romagna")
    assert g is not None and g.contains(__import__("shapely.geometry", fromlist=["Point"]).Point(11.34, 44.49))


def test_rainviewer_unknown_colours_never_hail():
    from worker.rainviewer_palette import decode, snow_mask
    img = np.zeros((2, 2, 4), dtype=np.uint8)
    img[0, 0] = (255, 178, 201, 255)   # undocumented pink (mixed precipitation in snow=1 tiles)
    img[0, 1] = (0x88, 0xdd, 0xee, 0xff)  # 15 dBZ rain colour
    img[1, 0] = (0xbf, 0xff, 0xff, 0xff)  # 10 dBZ snow colour
    d = decode(img)
    assert d[0, 0] <= 50 and abs(d[0, 1] - 15) < 0.5 and np.isnan(d[1, 1])
    m = snow_mask(img)
    assert m[1, 0] and not m[0, 0] and not m[0, 1]


def test_dpc_geotiff_sentinels():
    """DPC: -9999 = outside coverage (NaN), -9998 = no echo inside coverage (dry, -inf)."""
    import rasterio
    from rasterio.transform import from_origin as fo
    from worker.ingest_dpc import _read_geotiff
    arr = np.array([[-9999, -9998], [25.5, 40.0]], dtype="float32")
    with rasterio.MemoryFile() as mem:
        with mem.open(driver="GTiff", height=2, width=2, count=1, dtype="float32",
                      crs="EPSG:4326", transform=fo(10, 45, 1, 1)) as ds:
            ds.write(arr, 1)
        f = _read_geotiff(mem.read(), datetime(2026, 1, 1, tzinfo=timezone.utc), "DPC")
    assert np.isnan(f.dbz[0, 0]) and np.isneginf(f.dbz[0, 1]) and f.dbz[1, 1] == 40.0


def _fake_lfl_zip(points):
    """Build a zip with an LI-2-LFL-like BODY NetCDF (scaled lat/lon, flash_time since 2000)."""
    import io
    import tempfile
    import zipfile
    import netCDF4
    with tempfile.NamedTemporaryFile(suffix=".nc") as tmp:
        with netCDF4.Dataset(tmp.name, "w") as ds:
            ds.createDimension("flashes", len(points))
            la = ds.createVariable("latitude", "i2", ("flashes",)); la.scale_factor = 0.0027
            lo = ds.createVariable("longitude", "i2", ("flashes",)); lo.scale_factor = 0.0027
            ft = ds.createVariable("flash_time", "f8", ("flashes",)); ft.units = "seconds since 2000-01-01 00:00:00.0"
            ra = ds.createVariable("radiance", "f4", ("flashes",))
            la[:] = [p[0] for p in points]; lo[:] = [p[1] for p in points]
            ft[:] = [p[2] for p in points]; ra[:] = [1.0] * len(points)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.write(tmp.name, "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+LI-2-LFL--FD--BODY_0001.nc")
        return buf.getvalue()


def test_lightning_lfl_parsing():
    from worker.lightning import EPOCH_2000, _flashes_from_zip
    t = (datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc) - EPOCH_2000).total_seconds()
    from worker import lightning
    # Granarolo + London (Europe: kept) + Sao Paulo (kept only while a regional domain covers it)
    data = _fake_lfl_zip([(44.52, 11.43, t), (51.0, 0.0, t), (-23.5, -46.6, t)])
    lightning.EXTRA_BBOXES[:] = []
    fl = _flashes_from_zip(data)
    assert len(fl) == 2
    lat, lon, secs, rad = fl[0]
    assert abs(lat - 44.52) < 0.01 and abs(lon - 11.43) < 0.01
    assert abs(secs - datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc).timestamp()) < 1
    lightning.EXTRA_BBOXES[:] = [(-48.6, -25.3, -44.6, -21.7)]
    assert len(_flashes_from_zip(data)) == 3
    assert lightning.glm_boxes() == {"G19": [(-48.6, -25.3, -44.6, -21.7)]}
    lightning.EXTRA_BBOXES[:] = []


def test_calibration_mapping_monotonic():
    from worker.probability import BINS, calibrate
    rel = [[0.0, 0.01, 0.03, 0.05, 0.1, 0.12, 0.2, 0.18, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.85, 0.9]]
    cal = {"reliability": {"HEAVY_RAIN": rel}}
    p = np.linspace(0, 1, 50).astype("float32")
    c = calibrate(p, "HEAVY_RAIN", 0, cal)
    assert np.all(np.diff(c) >= -1e-6) and c[0] == 0.0 and c[-1] <= 1.0
    assert abs(float(calibrate(np.array([0.5], dtype="float32"), "HEAVY_RAIN", 0, cal)[0]) - 0.4) < 0.08
    assert len(BINS) == 21


def test_radar_payload_uses_point_intensity_and_probability():
    from worker.notifier import build_radar_payload
    row = {"lead_time_minutes": 20, "event_type": "HEAVY_RAIN", "severity_level": "MODERATE", "prob": 0.78,
           "point_dbz": 30.0, "timezone": "Europe/Rome", "flashes_near": 0}
    p = build_radar_payload(row, datetime(2026, 11, 17, 15, 50, tzinfo=timezone.utc))
    assert "Probabilità 80%" in p["body"] and "≥ 6 mm/h" in p["body"] and "17:10" in p["body"]


def test_schema_splitter_keeps_dollar_blocks():
    from app.database import split_sql
    stmts = split_sql("CREATE TABLE a (x int);\n-- c;\nDO $$ BEGIN IF 1=1 THEN SELECT 1; SELECT 2; END IF; END $$;\nSELECT 3;")
    assert len(stmts) == 3 and stmts[1].startswith("DO $$") and stmts[1].endswith("END $$")


def test_regional_domain_geometry():
    from worker.ingest_regional import cell_of, make_domain
    d = make_domain(*cell_of(11.6, 48.1))
    assert d.id == "R+012+048"
    lon0, lat0, lon1, lat1 = d.bbox
    assert lon0 < 11.6 < lon1 and lat0 < 48.1 < lat1
    assert 3.5 < lat1 - lat0 < 3.7 and 5.0 < lon1 - lon0 < 5.6   # ~400 km square at 48 N


def test_meteoalarm_and_nws_parsing():
    from datetime import datetime, timedelta, timezone
    from worker.cap import parse_meteoalarm, parse_nws
    exp = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    ma = {"warnings": [{"alert": {"identifier": "w1", "status": "Actual", "msgType": "Alert", "info": [
        {"language": "de-DE", "event": "STURMBÖEN", "expires": exp, "parameter": [
            {"valueName": "awareness_level", "value": "3; orange; Severe"},
            {"valueName": "awareness_type", "value": "1; Wind"}],
         "area": [{"areaDesc": "Kreis und Stadt München", "geocode": [{"valueName": "EMMA_ID", "value": "DE413"}]}]},
        {"language": "en", "event": "gusts", "headline": "Official WARNING of GUSTS", "expires": exp, "parameter": [
            {"valueName": "awareness_level", "value": "3; orange; Severe"},
            {"valueName": "awareness_type", "value": "1; Wind"}],
         "area": [{"areaDesc": "Kreis und Stadt München", "geocode": [{"valueName": "EMMA_ID", "value": "DE413"}]}]}]}},
        {"alert": {"identifier": "w2", "status": "Actual", "info": [{"language": "en", "parameter": [
            {"valueName": "awareness_level", "value": "1; green; Minor"}], "area": []}]}}]}
    a = parse_meteoalarm(ma, "DE")
    assert len(a) == 1 and a[0]["severity"] == "Severe" and a[0]["event_type"] == "CAP_WIND"
    assert a[0]["codes"] == ["DE413"] and a[0]["geom"] is None and a[0]["headline"].startswith("Official")
    nws = {"features": [{"id": "x", "geometry": None, "properties": {
        "id": "urn:1", "status": "Actual", "messageType": "Alert", "event": "Severe Thunderstorm Warning",
        "severity": "Severe", "expires": exp, "affectedZones": ["https://api.weather.gov/zones/forecast/TXZ119"]}}]}
    n = parse_nws(nws)
    assert n[0]["event_type"] == "CAP_THUNDERSTORM" and n[0]["codes"] == ["NWS:TXZ119"] and n[0]["country"] == "US"


def test_online_threshold_fit():
    from worker.online_calibration import NBINS, fit_threshold
    n = np.zeros(NBINS, int); h = np.zeros(NBINS, int)
    # p 0.70-0.80: 300 alerts, 60% confirmed; 0.80-0.90: 300, 95%; >= 0.90: 300, 99%
    for b, prec in ((14, .6), (15, .6), (16, .95), (17, .95), (18, .99), (19, .99)):
        n[b] = 150; h[b] = int(150 * prec)
    f = fit_threshold(n, h)
    assert f["status"] == "ok" and f["p"] == 0.8 and f["precision"] >= 0.9   # from 0.7: 84%, 0.75: 89%
    assert fit_threshold(n // 10, h // 10)["status"] == "insufficient_data"
    bad = fit_threshold(n, (n * 0.5).astype(int))
    assert bad["status"] == "disabled" and bad["p"] > 1


def test_online_verification_counts_onsets_only():
    from datetime import timedelta
    from worker import online_calibration as oc
    from worker.nowcast_engine import NowcastResult
    oc._HISTORY.clear(); oc._PENDING.clear()
    t0 = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)
    shape = (30, 30)
    leads = [5 * (i + 1) for i in range(12)]

    def res(t, obs):
        return NowcastResult(t, [], np.zeros((12,) + shape, "float32"), leads, None, None, "DPC", 0.0,
                             observed_dbz=obs)
    dry = np.full(shape, -np.inf, "float32")
    p = np.zeros((12,) + shape, "float32")
    p[:, :, :15] = 0.9                        # left half: rain forecast with P 0.9
    oc.update("IT", res(t0, dry), {"RAIN_START": p})
    wet = dry.copy(); wet[:, :10] = 30.0      # 10 minutes later: rain over 2/3 of the forecast area
    oc.update("IT", res(t0 + timedelta(minutes=10), wet), {"RAIN_START": np.zeros_like(p)})
    acc = oc._PENDING[(t0.date(), "DPC", "RAIN_START", 10)]
    n, hits = acc[0][18], acc[1][18]          # bin 0.90-0.95
    assert n == 50 and 0.6 < hits / n < 0.9   # 10x5 subsampled points forecast at 0.9; ~2/3 (+3 km halo) observed
    oc._HISTORY.clear(); oc._PENDING.clear()

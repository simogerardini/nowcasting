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
    data = _fake_lfl_zip([(44.52, 11.43, t), (51.0, 0.0, t)])  # Granarolo + London (outside bbox)
    fl = _flashes_from_zip(data)
    assert len(fl) == 1
    lat, lon, secs, rad = fl[0]
    assert abs(lat - 44.52) < 0.01 and abs(lon - 11.43) < 0.01
    assert abs(secs - datetime(2026, 11, 17, 15, 0, tzinfo=timezone.utc).timestamp()) < 1

"""Build worker/data/emma_areas.geojson.gz: MeteoAlarm EMMA_ID warning areas (simplified ~500 m).

Source: geocodes.json bundled with the MIT-licensed `meteoalarm` Python package
(https://github.com/NiklasJordan/meteoalarm, src/meteoalarm/assets/geocodes.json), which republishes
the MeteoAlarm geocode boundaries.  Usage: python scripts/build_cap_areas.py geocodes.json
"""
import gzip
import json
import re
import sys
from pathlib import Path

import shapely
from shapely.geometry import mapping, shape

src = json.loads(Path(sys.argv[1]).read_text())
out = []
for f in src["features"]:
    p = f["properties"]
    g = shapely.make_valid(shapely.make_valid(shape(f["geometry"])).simplify(0.005, preserve_topology=True))
    if g.is_empty:
        continue
    out.append({"type": "Feature", "geometry": mapping(g),
                "properties": {"code": p["code"], "country": p.get("country"), "name": p.get("name")}})
s = json.dumps({"type": "FeatureCollection", "features": out}, separators=(",", ":"), ensure_ascii=False)
s = re.sub(r"(\d+\.\d{4})\d+", r"\1", s)
dst = Path(__file__).resolve().parents[1] / "worker" / "data" / "emma_areas.geojson.gz"
dst.write_bytes(gzip.compress(s.encode(), 9))
print(len(out), "areas ->", dst, dst.stat().st_size, "bytes")

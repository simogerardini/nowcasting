"use client";

import maplibregl, { GeoJSONSource, ImageSource, Map as MlMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_URL, getRuntimeConfig } from "@/lib/config";
import { CAP_EVENTS, EVENT_BY_ID, RADAR_EVENTS } from "@/lib/events";
import type { Coords } from "@/lib/pushManager";

interface Frame {
  time: string;
  offset_min: number;
  kind: "observed" | "forecast";
  url: string;
}

interface Manifest {
  run_timestamp: string;
  source: string;
  coordinates: [[number, number], [number, number], [number, number], [number, number]];
  frames: Frame[];
  forecast_geojson: string;
  cap_geojson: string;
  legend: { dbz: number; color: string }[];
}

// OpenFreeMap "dark" vector style (free, no API key, no usage limits), tuned for OLED screens:
// pure-black land, very dark blue sea for coastlines, slightly brighter labels.
const BASEMAP_URL = "https://tiles.openfreemap.org/styles/dark";
const BLACK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#000000" } }],
};
const OLED_PAINT: Record<string, Record<string, unknown>> = {
  background: { "background-color": "#000000" },
  water: { "fill-color": "#061226" },
  waterway: { "line-color": "#061226" },
  landcover_ice_shelf: { "fill-color": "#000000" },
  landcover_glacier: { "fill-color": "#050505" },
  landuse_residential: { "fill-color": "#080808" },
  landcover_wood: { "fill-color": "#0b0b0b" },
  landuse_park: { "fill-color": "#0b0b0b" },
  building: { "fill-color": "#0d0d0d" },
  road_area_pier: { "fill-color": "#000000" },
};

async function loadBasemap(): Promise<maplibregl.StyleSpecification> {
  try {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), 6000);
    const r = await fetch(BASEMAP_URL, { signal: ctl.signal });
    clearTimeout(t);
    if (!r.ok) return BLACK_STYLE;
    const style = (await r.json()) as maplibregl.StyleSpecification;
    for (const layer of style.layers) {
      const patch = OLED_PAINT[layer.id];
      if (patch) Object.assign(((layer as { paint?: object }).paint ??= {}), patch);
      if (layer.type === "symbol" && layer.id.startsWith("place_")) {
        Object.assign(((layer as { paint?: object }).paint ??= {}), { "text-color": "#9ca3af", "text-halo-color": "#000000" });
      }
    }
    return style;
  } catch {
    return BLACK_STYLE;
  }
}

interface RegionDomain {
  id: string;
  source: string;
  run_timestamp: string;
  coordinates: Manifest["coordinates"];
  frames: Frame[];
}

interface Regions {
  domains: RegionDomain[];
  europe: { coordinates: Manifest["coordinates"]; frames: { time: string; url: string }[] } | null;
}

interface RvFrame {
  time: number; // ms
  tiles: string;
}

// Where the OPERA (EUMETNET) composite is dense: outside, observed radar comes from RainViewer tiles
const EUROPE_BOX = { w: -11, s: 34.5, e: 33, n: 71 };
const RAINVIEWER_MAPS = "https://api.rainviewer.com/public/weather-maps.json";
const STEP_TOL = 5.5 * 60_000;

function nearest<T extends { time: string }>(frames: T[], t: number, tol = STEP_TOL): T | undefined {
  let best: T | undefined;
  let bd = Infinity;
  for (const f of frames) {
    const d = Math.abs(Date.parse(f.time) - t);
    if (d < bd) {
      bd = d;
      best = f;
    }
  }
  return bd <= tol ? best : undefined;
}

const EMPTY_FC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };
const REFRESH_MS = 60_000;

function fmtTime(iso: string) {
  return new Date(iso).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
}

export default function RadarMap({ position, focus }: { position: Coords | null; focus: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<MlMap | null>(null);
  const marker = useRef<maplibregl.Marker | null>(null);
  const [ready, setReady] = useState(false);
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [regions, setRegions] = useState<Regions | null>(null);
  const [rv, setRv] = useState<RvFrame[]>([]);
  const [outsideEurope, setOutsideEurope] = useState(false);
  const [idx, setIdx] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const centred = useRef(false);

  // ---------------------------------------------------------------- map init
  useEffect(() => {
    if (!container.current || map.current) return;
    let m: MlMap | null = null;
    let cancelled = false;
    void loadBasemap().then((style) => {
      if (!cancelled && container.current) m = initMap(container.current, style);
    });
    return () => {
      cancelled = true;
      m?.remove();
      map.current = null;
    };
  }, []);

  function initMap(el: HTMLDivElement, style: maplibregl.StyleSpecification): MlMap {
    const m = new maplibregl.Map({
      container: el,
      style,
      center: [12.5, 42.0],
      zoom: 5.2,
      maxZoom: 12,
      attributionControl: false, // credits: minimal OSM line on the map + full list in the settings panel
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      // z-order anchors for the radar layers (bottom -> top): RainViewer world, OPERA Europe, DPC Italy, regions
      for (const id of ["z-rv", "z-eu", "z-it", "z-dom"]) {
        m.addLayer({ id, type: "background", layout: { visibility: "none" }, paint: { "background-opacity": 0 } });
      }
      const updateArea = () => {
        const c = m.getCenter();
        setOutsideEurope(!(c.lng >= EUROPE_BOX.w && c.lng <= EUROPE_BOX.e && c.lat >= EUROPE_BOX.s && c.lat <= EUROPE_BOX.n));
      };
      m.on("moveend", updateArea);
      updateArea();
      m.addSource("risk", { type: "geojson", data: EMPTY_FC });
      m.addSource("lightning", { type: "geojson", data: EMPTY_FC });
      m.addSource("cap", { type: "geojson", data: EMPTY_FC });
      m.addLayer({
        id: "cap-fill",
        type: "fill",
        source: "cap",
        paint: { "fill-color": "#a855f7", "fill-opacity": 0.05 },
      });
      m.addLayer({
        id: "cap-line",
        type: "line",
        source: "cap",
        paint: { "line-color": "#a855f7", "line-opacity": 0.6, "line-width": 1.2, "line-dasharray": [2, 2] },
      });
      const sevColor: maplibregl.ExpressionSpecification = [
        "match", ["get", "event_type"],
        ...RADAR_EVENTS.flatMap((e) => [e.id, e.color]), "#facc15",
      ] as unknown as maplibregl.ExpressionSpecification;
      m.addLayer({ id: "risk-fill", type: "fill", source: "risk", paint: { "fill-color": sevColor, "fill-opacity": 0.12 } });
      m.addLayer({ id: "risk-line", type: "line", source: "risk", paint: { "line-color": sevColor, "line-width": 1.8 } });
      // lightning: bright when fresh, fading out over 60 min
      m.addLayer({
        id: "lightning",
        type: "circle",
        source: "lightning",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 2, 9, 4, 12, 6],
          "circle-color": ["interpolate", ["linear"], ["get", "age_min"], 0, "#fef08a", 15, "#facc15", 40, "#f97316"],
          "circle-opacity": ["interpolate", ["linear"], ["get", "age_min"], 0, 1, 30, 0.6, 60, 0.15],
          "circle-stroke-color": "#000000",
          "circle-stroke-width": 0.5,
        },
      });
      m.on("click", "risk-fill", (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as Record<string, string | number>;
        new maplibregl.Popup({ closeButton: false })
          .setLngLat(e.lngLat)
          .setHTML(
            `<b>${EVENT_BY_ID[p.event_type as string]?.label ?? p.event_type}</b><br/>` +
              `+${p.lead_time_minutes} min (${fmtTime(p.valid_time as string)})<br/>` +
              (p.prob != null && p.prob !== "" ? `probabilità fino a ${Math.round(Number(p.prob) * 100)}%` : `max ${p.max_dbz} dBZ`),
          )
          .addTo(m);
      });
      m.on("click", "cap-fill", (e) => {
        const p = e.features?.[0]?.properties as Record<string, string> | undefined;
        if (!p) return;
        new maplibregl.Popup({ closeButton: false })
          .setLngLat(e.lngLat)
          .setHTML(`<b>📢 ${CAP_EVENTS.find((c) => c.id === p.event_type)?.label ?? p.event}</b><br/>${p.event}<br/>${p.headline ?? p.area_desc ?? ""}<br/><small>${p.severity}</small>`)
          .addTo(m);
      });
      setReady(true);
    });
    map.current = m;
    return m;
  }

  // ---------------------------------------------------------------- data refresh
  const load = useCallback(async () => {
    try {
      const { assetsUrl } = await getRuntimeConfig();
      const r = await fetch(`${assetsUrl}/latest.json?t=${Math.floor(Date.now() / 60000)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const mf: Manifest = await r.json();
      // Local-storage fallback publishes API-relative URLs (/assets/...)
      const abs = (u: string) => (u.startsWith("/") ? `${API_URL}${u}` : u);
      mf.frames.forEach((f) => (f.url = abs(f.url)));
      mf.forecast_geojson = abs(mf.forecast_geojson);
      mf.cap_geojson = abs(mf.cap_geojson);
      mf.frames.sort((a, b) => a.offset_min - b.offset_min);
      setManifest((prev) => {
        if (prev?.run_timestamp !== mf.run_timestamp) {
          setIdx(Math.max(0, mf.frames.findIndex((f) => f.offset_min === 0)));
          mf.frames.forEach((f) => {
            const img = new Image();
            img.crossOrigin = "anonymous";
            img.src = f.url; // preload for smooth animation
          });
        }
        return mf;
      });
      setError(null);
      const rg = await fetch(`${assetsUrl}/regions.json?t=${Math.floor(Date.now() / 60000)}`)
        .then((x) => (x.ok ? x.json() : null))
        .catch(() => null);
      if (rg) {
        rg.domains?.forEach((d: RegionDomain) => d.frames.forEach((f) => (f.url = abs(f.url))));
        rg.europe?.frames.forEach((f: { url: string }) => (f.url = abs(f.url)));
        setRegions((prev) => {
          const seen = new Set([...(prev?.europe?.frames ?? []), ...(prev?.domains ?? []).flatMap((d) => d.frames)].map((f) => f.url));
          [...(rg.europe?.frames ?? []), ...(rg.domains ?? []).flatMap((d: RegionDomain) => d.frames)]
            .filter((f: { url: string }) => !seen.has(f.url))
            .forEach((f: { url: string }) => {
              const img = new Image();
              img.crossOrigin = "anonymous";
              img.src = f.url; // preload
            });
          return rg;
        });
      }
      const rvm = await fetch(RAINVIEWER_MAPS)
        .then((x) => (x.ok ? x.json() : null))
        .catch(() => null);
      if (rvm?.radar?.past?.length) {
        setRv(
          (rvm.radar.past as { time: number; path: string }[]).slice(-6).map((p) => ({
            time: p.time * 1000,
            tiles: `${rvm.host}${p.path}/256/{z}/{x}/{y}/2/1_1.png`,
          })),
        );
      }
      const m = map.current;
      if (!m) return;
      const [risk, cap] = await Promise.all([
        fetch(mf.forecast_geojson).then((x) => x.json()).catch(() => EMPTY_FC),
        fetch(mf.cap_geojson).then((x) => x.json()).catch(() => EMPTY_FC),
      ]);
      (m.getSource("risk") as GeoJSONSource | undefined)?.setData(risk);
      (m.getSource("cap") as GeoJSONSource | undefined)?.setData(cap);
      const { assetsUrl: au } = await getRuntimeConfig();
      const base = au.startsWith("/") ? `${API_URL}${au}` : au;
      const lt = await fetch(`${base}/lightning.geojson?t=${Math.floor(Date.now() / 60000)}`)
        .then((x) => (x.ok ? x.json() : EMPTY_FC))
        .catch(() => EMPTY_FC);
      (m.getSource("lightning") as GeoJSONSource | undefined)?.setData(lt);
    } catch (e) {
      const msg = (e as Error).message;
      setError(msg === "HTTP 404" ? "Primo ciclo radar in elaborazione, riprova tra qualche minuto" : `Dati radar non disponibili (${msg})`);
    }
  }, []);

  useEffect(() => {
    if (!ready) return;
    void load();
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [ready, load]);

  // ---------------------------------------------------------------- frame display
  const frame = manifest?.frames[idx];
  useEffect(() => {
    const m = map.current;
    if (!ready || !m || !manifest || !frame) return;
    const src = m.getSource("radar") as ImageSource | undefined;
    if (src) src.updateImage({ url: frame.url, coordinates: manifest.coordinates });
    else {
      m.addSource("radar", { type: "image", url: frame.url, coordinates: manifest.coordinates });
      m.addLayer(
        { id: "radar", type: "raster", source: "radar", paint: { "raster-opacity": 0.75, "raster-fade-duration": 0, "raster-resampling": "linear" } },
        "z-it",
      );
    }
    const lead = frame.kind === "forecast" ? frame.offset_min : -1;
    // RAIN_START areas (any rain >= 20 dBZ) are what the radar image already shows: don't outline them
    const filter: maplibregl.FilterSpecification = [
      "all", ["==", ["get", "lead_time_minutes"], lead], ["!=", ["get", "event_type"], "RAIN_START"],
    ];
    m.setFilter("risk-fill", filter);
    m.setFilter("risk-line", filter);
  }, [ready, manifest, frame]);

  // ---------------------------------------------------------------- Europe / world / regional layers
  const RASTER_PAINT = { "raster-fade-duration": 0, "raster-resampling": "linear" } as const;
  function showImage(m: MlMap, id: string, url: string | undefined, coords: Manifest["coordinates"] | undefined, before: string) {
    const layer = m.getLayer(id);
    if (!url || !coords) {
      if (layer) m.setPaintProperty(id, "raster-opacity", 0);
      return;
    }
    const src = m.getSource(id) as ImageSource | undefined;
    if (src) src.updateImage({ url, coordinates: coords });
    else m.addSource(id, { type: "image", url, coordinates: coords });
    if (!layer) m.addLayer({ id, type: "raster", source: id, paint: { ...RASTER_PAINT, "raster-opacity": 0.75 } }, before);
    else m.setPaintProperty(id, "raster-opacity", 0.75);
  }

  // RainViewer tile layers: one per past frame, toggled by opacity (no re-download while animating)
  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    const keep = new Set(rv.map((f) => `rv-${f.time}`));
    for (const l of m.getStyle().layers ?? []) {
      if (l.id.startsWith("rv-") && !keep.has(l.id)) {
        m.removeLayer(l.id);
        m.removeSource(l.id);
      }
    }
    for (const f of rv) {
      const id = `rv-${f.time}`;
      if (m.getSource(id)) continue;
      m.addSource(id, { type: "raster", tiles: [f.tiles], tileSize: 256, maxzoom: 7, attribution: "RainViewer" });
      m.addLayer(
        { id, type: "raster", source: id, layout: { visibility: "none" }, paint: { ...RASTER_PAINT, "raster-opacity": 0 } },
        "z-rv",
      );
    }
  }, [ready, rv]);

  useEffect(() => {
    const m = map.current;
    if (!ready || !m || !frame) return;
    const t = Date.parse(frame.time);
    const observed = frame.kind === "observed";
    // OPERA Europe: observed steps only
    const eu = regions?.europe;
    const ef = observed && eu ? nearest(eu.frames, t) : undefined;
    showImage(m, "europe", ef?.url, eu?.coordinates, "z-eu");
    // RainViewer (outside Europe): newest frame not after this step, within 10 min
    const cand = rv.filter((f) => f.time <= t + 60_000 && t - f.time <= 10 * 60_000).pop();
    for (const f of rv) {
      const id = `rv-${f.time}`;
      if (!m.getLayer(id)) continue;
      // hidden layers download no tiles: RainViewer is only used away from Europe
      m.setLayoutProperty(id, "visibility", outsideEurope ? "visible" : "none");
      m.setPaintProperty(id, "raster-opacity", observed && cand === f ? 0.7 : 0);
    }
    // regional nowcasts: forecast steps
    const ids = new Set<string>();
    for (const d of regions?.domains ?? []) {
      const id = `dom-${d.id}`;
      ids.add(id);
      const f = observed ? undefined : nearest(d.frames, t);
      showImage(m, id, f?.url, d.coordinates, "z-dom");
    }
    for (const l of m.getStyle().layers ?? []) {
      if (l.id.startsWith("dom-") && !ids.has(l.id)) {
        m.removeLayer(l.id);
        m.removeSource(l.id);
      }
    }
  }, [ready, frame, regions, rv, outsideEurope]);

  // ---------------------------------------------------------------- animation
  useEffect(() => {
    // Continuous loop; pause 1.5 s on the last forecast frame before restarting
    if (!manifest) return;
    const n = manifest.frames.length;
    const t = setTimeout(() => setIdx((i) => (i + 1) % n), idx === n - 1 ? 1500 : 700);
    return () => clearTimeout(t);
  }, [manifest, idx]);

  // ---------------------------------------------------------------- user position
  useEffect(() => {
    const m = map.current;
    if (!ready || !m || !position) return;
    if (!marker.current) {
      const el = document.createElement("div");
      el.className = "h-4 w-4 rounded-full border-2 border-white bg-sky-500 shadow-[0_0_0_6px_rgba(14,165,233,0.3)]";
      marker.current = new maplibregl.Marker({ element: el });
    }
    marker.current.setLngLat([position.lon, position.lat]).addTo(m);
    if (!centred.current) {
      centred.current = true;
      m.flyTo({ center: [position.lon, position.lat], zoom: focus ? 9 : 7.5, speed: 1.6 });
    }
  }, [ready, position, focus]);

  const label = useMemo(() => {
    if (!frame) return "—";
    const sign = frame.offset_min > 0 ? "+" : "";
    return `${fmtTime(frame.time)} (${frame.offset_min === 0 ? "ora" : `${sign}${frame.offset_min} min`})`;
  }, [frame]);

  return (
    <div className="relative h-full w-full">
      <div ref={container} className="h-full w-full" />
      {error && (
        <div className="absolute inset-x-2 bottom-20 z-10 mx-auto max-w-md rounded-lg bg-red-900/90 px-3 py-2 text-center text-sm">
          {error}
        </div>
      )}
      <a
        href="https://www.openstreetmap.org/copyright"
        target="_blank"
        rel="noreferrer"
        className="absolute bottom-[max(0.25rem,env(safe-area-inset-bottom))] right-2 z-10 text-[9px] text-white/35"
      >
        © OpenStreetMap
      </a>
      {frame && (
        <div className="pointer-events-none absolute bottom-8 left-1/2 z-10 -translate-x-1/2 rounded-full bg-night-900/85 px-4 py-1.5 text-sm shadow-lg backdrop-blur">
          <span className={`font-mono ${frame.kind === "forecast" ? "text-amber-300" : "text-sky-200"}`}>
            {frame.kind === "forecast" ? "Previsione " : "Radar "}
            {label}
          </span>
          <span className="ml-2 inline-flex h-1.5 w-16 overflow-hidden rounded-full bg-slate-700 align-middle">
            <span
              className="h-full bg-sky-400 transition-all"
              style={{ width: `${((idx + 1) / (manifest?.frames.length ?? 1)) * 100}%` }}
            />
          </span>
        </div>
      )}
    </div>
  );
}

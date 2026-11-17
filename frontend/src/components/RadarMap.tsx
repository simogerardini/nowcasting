"use client";

import maplibregl, { GeoJSONSource, ImageSource, Map as MlMap } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_URL, getRuntimeConfig, SEVERITY_COLORS, SEVERITY_LABEL } from "@/lib/config";
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

// CARTO Voyager raster tiles (lightweight, no style/glyph downloads) - same basemap as tracciatore-gpx
const BASEMAP_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    carto: {
      type: "raster",
      tiles: ["a", "b", "c", "d"].map((sub) => `https://${sub}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png`),
      tileSize: 256,
      maxzoom: 19,
      attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> © <a href="https://carto.com/attributions">CARTO</a>',
    },
  },
  layers: [
    { id: "bg", type: "background", paint: { "background-color": "#e5e7eb" } },
    { id: "carto", type: "raster", source: "carto" },
  ],
};
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
  const [idx, setIdx] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const centred = useRef(false);

  // ---------------------------------------------------------------- map init
  useEffect(() => {
    if (!container.current || map.current) return;
    const m = initMap(container.current, BASEMAP_STYLE);
    return () => {
      m.remove();
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
      attributionControl: { compact: true, customAttribution: "Radar © DPC / RainViewer · Nowcast Noctalix" },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    m.on("load", () => {
      m.addSource("risk", { type: "geojson", data: EMPTY_FC });
      m.addSource("cap", { type: "geojson", data: EMPTY_FC });
      m.addLayer({
        id: "cap-fill",
        type: "fill",
        source: "cap",
        paint: { "fill-color": "#a855f7", "fill-opacity": 0.12 },
      });
      m.addLayer({
        id: "cap-line",
        type: "line",
        source: "cap",
        paint: { "line-color": "#a855f7", "line-width": 1.5, "line-dasharray": [2, 2] },
      });
      const sevColor: maplibregl.ExpressionSpecification = [
        "match", ["get", "severity_level"],
        "EXTREME", SEVERITY_COLORS.EXTREME, "SEVERE", SEVERITY_COLORS.SEVERE, SEVERITY_COLORS.MODERATE,
      ];
      m.addLayer({ id: "risk-fill", type: "fill", source: "risk", paint: { "fill-color": sevColor, "fill-opacity": 0.12 } });
      m.addLayer({ id: "risk-line", type: "line", source: "risk", paint: { "line-color": sevColor, "line-width": 1.8 } });
      m.on("click", "risk-fill", (e) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties as Record<string, string | number>;
        new maplibregl.Popup({ closeButton: false })
          .setLngLat(e.lngLat)
          .setHTML(
            `<b>${SEVERITY_LABEL[p.severity_level as string] ?? p.severity_level}</b><br/>` +
              `+${p.lead_time_minutes} min (${fmtTime(p.valid_time as string)})<br/>` +
              `max ${p.max_dbz} dBZ · ~${p.estimated_rain_mmh} mm/h`,
          )
          .addTo(m);
      });
      m.on("click", "cap-fill", (e) => {
        const p = e.features?.[0]?.properties as Record<string, string> | undefined;
        if (!p) return;
        new maplibregl.Popup({ closeButton: false })
          .setLngLat(e.lngLat)
          .setHTML(`<b>📢 ${p.event}</b><br/>${p.headline ?? p.area_desc ?? ""}<br/><small>${p.severity}</small>`)
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
      const m = map.current;
      if (!m) return;
      const [risk, cap] = await Promise.all([
        fetch(mf.forecast_geojson).then((x) => x.json()).catch(() => EMPTY_FC),
        fetch(mf.cap_geojson).then((x) => x.json()).catch(() => EMPTY_FC),
      ]);
      (m.getSource("risk") as GeoJSONSource | undefined)?.setData(risk);
      (m.getSource("cap") as GeoJSONSource | undefined)?.setData(cap);
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
        { id: "radar", type: "raster", source: "radar", paint: { "raster-opacity": 0.75, "raster-fade-duration": 0, "raster-resampling": "nearest" } },
        "cap-fill",
      );
    }
    const lead = frame.kind === "forecast" ? frame.offset_min : -1;
    const filter: maplibregl.FilterSpecification = ["==", ["get", "lead_time_minutes"], lead];
    m.setFilter("risk-fill", filter);
    m.setFilter("risk-line", filter);
  }, [ready, manifest, frame]);

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

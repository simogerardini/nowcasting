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

const BASEMAP = "https://tiles.openfreemap.org/styles/dark";
// Used if the vector basemap is unreachable: radar + polygons stay usable.
const FALLBACK_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {},
  layers: [{ id: "bg", type: "background", paint: { "background-color": "#0b1020" } }],
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
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showCap, setShowCap] = useState(true);
  const centred = useRef(false);

  // ---------------------------------------------------------------- map init
  useEffect(() => {
    if (!container.current || map.current) return;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 6000);
    let m: MlMap | null = null;
    let cancelled = false;
    fetch(BASEMAP, { signal: ctl.signal })
      .then((r) => (r.ok ? (r.json() as Promise<maplibregl.StyleSpecification>) : FALLBACK_STYLE))
      .catch(() => FALLBACK_STYLE)
      .then((style) => {
        clearTimeout(timer);
        if (cancelled || !container.current) return;
        m = initMap(container.current, style);
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

  useEffect(() => {
    const m = map.current;
    if (!ready || !m) return;
    for (const id of ["cap-fill", "cap-line"]) m.setLayoutProperty(id, "visibility", showCap ? "visible" : "none");
  }, [ready, showCap]);

  // ---------------------------------------------------------------- animation
  useEffect(() => {
    if (!playing || !manifest) return;
    const t = setInterval(() => setIdx((i) => (i + 1) % manifest.frames.length), 600);
    return () => clearInterval(t);
  }, [playing, manifest]);

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

  const ticks = useMemo(() => {
    const fr = manifest?.frames ?? [];
    if (fr.length < 2) return [];
    const pos = (off: number) => {
      const i = fr.findIndex((f) => f.offset_min >= off);
      return i < 0 ? null : (i / (fr.length - 1)) * 100;
    };
    return [
      { label: `${fr[0].offset_min}m`, pct: 0 },
      { label: "ora", pct: pos(0) },
      { label: "+30m", pct: pos(30) },
      { label: "+60m", pct: 100 },
    ].filter((t): t is { label: string; pct: number } => t.pct !== null);
  }, [manifest]);

  const label = useMemo(() => {
    if (!frame) return "—";
    const sign = frame.offset_min > 0 ? "+" : "";
    return `${fmtTime(frame.time)} (${frame.offset_min === 0 ? "ora" : `${sign}${frame.offset_min} min`})`;
  }, [frame]);

  return (
    <div className="relative h-full w-full">
      <div ref={container} className="h-full w-full" />
      {error && (
        <div className="absolute inset-x-2 bottom-40 z-10 mx-auto max-w-md rounded-lg bg-red-900/90 px-3 py-2 text-center text-sm sm:bottom-44">
          {error}
        </div>
      )}
      <div className="absolute inset-x-2 bottom-2 rounded-2xl bg-night-900/85 p-3 shadow-xl backdrop-blur sm:inset-x-auto sm:left-1/2 sm:w-[560px] sm:-translate-x-1/2">
        <div className="mb-2 flex items-center justify-between text-sm">
          <button
            onClick={() => setPlaying((p) => !p)}
            className="rounded-full bg-sky-600 px-3 py-1 font-semibold hover:bg-sky-500"
            aria-label={playing ? "Pausa" : "Riproduci"}
          >
            {playing ? "❚❚" : "▶"}
          </button>
          <span className={`font-mono ${frame?.kind === "forecast" ? "text-amber-300" : "text-sky-200"}`}>
            {frame?.kind === "forecast" ? "Previsione " : "Osservato "}
            {label}
          </span>
          <label className="flex items-center gap-1 text-xs text-slate-300">
            <input type="checkbox" checked={showCap} onChange={(e) => setShowCap(e.target.checked)} />
            Allerte
          </label>
        </div>
        <input
          type="range"
          min={0}
          max={Math.max(0, (manifest?.frames.length ?? 1) - 1)}
          value={idx}
          onChange={(e) => {
            setPlaying(false);
            setIdx(Number(e.target.value));
          }}
          className="w-full accent-sky-500"
          aria-label="Timeline radar"
        />
        <div className="relative mt-1 h-3 text-[10px] text-slate-400">
          {ticks.map((t) => (
            <span key={t.label} className="absolute -translate-x-1/2" style={{ left: `${t.pct}%` }}>
              {t.label}
            </span>
          ))}
        </div>
        {manifest && (
          <div className="mt-2 flex items-center gap-[2px]">
            {manifest.legend.map((l) => (
              <div key={l.dbz} className="flex-1 text-center text-[9px] text-slate-300">
                <div className="h-2 rounded-sm" style={{ background: l.color }} />
                {l.dbz}
              </div>
            ))}
            <span className="ml-1 text-[9px] text-slate-400">dBZ</span>
          </div>
        )}
      </div>
    </div>
  );
}

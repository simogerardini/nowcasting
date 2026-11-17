"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import AlertSettings from "@/components/AlertSettings";
import IosInstallBanner from "@/components/IosInstallBanner";
import { API_URL } from "@/lib/config";
import { CAP_EVENTS, EVENT_BY_ID } from "@/lib/events";
import { Coords, registerServiceWorker, storedEndpoint, watchLocation } from "@/lib/pushManager";

const RadarMap = dynamic(() => import("@/components/RadarMap"), { ssr: false });

interface PointAlerts {
  radar: { lead_time_minutes: number; severity_level: string; event_type: string; max_dbz: number; estimated_rain_mmh: number }[];
  cap: { identifier: string; event: string; event_type: string | null; severity: string; headline: string | null }[];
}

export default function Home() {
  const [position, setPosition] = useState<Coords | null>(null);
  const [focus, setFocus] = useState(false);
  const [alerts, setAlerts] = useState<PointAlerts | null>(null);

  useEffect(() => {
    setFocus(new URLSearchParams(window.location.search).has("focus"));
    void registerServiceWorker();
    let stop = () => undefined as void;
    const start = () => {
      stop = watchLocation(setPosition);
    };
    if (storedEndpoint()) start();
    else
      navigator.permissions
        ?.query({ name: "geolocation" as PermissionName })
        .then((p) => p.state === "granted" && start())
        .catch(() => undefined);
    return () => stop();
  }, []);

  useEffect(() => {
    if (!position) return;
    const load = () =>
      fetch(`${API_URL}/api/v1/alerts/point?lat=${position.lat}&lon=${position.lon}`)
        .then((r) => (r.ok ? r.json() : null))
        .then(setAlerts)
        .catch(() => undefined);
    load();
    const t = setInterval(load, 120_000);
    return () => clearInterval(t);
  }, [position]);

  const RANK: Record<string, number> = { EXTREME: 3, SEVERE: 2, MODERATE: 1, INFO: 0 };
  const nowWet = alerts?.radar.find((r) => r.lead_time_minutes === 0);
  const next = alerts?.radar
    .filter((r) => r.lead_time_minutes > 0 && !(nowWet && r.event_type === nowWet.event_type))
    .sort((a, b) => (RANK[b.severity_level] ?? 0) - (RANK[a.severity_level] ?? 0) || a.lead_time_minutes - b.lead_time_minutes)[0];

  return (
    <main className="fixed inset-0">
      <RadarMap position={position} focus={focus} />
      <IosInstallBanner />
      <header className="pointer-events-none absolute left-3 top-3 z-10 rounded-xl bg-night-900/80 px-3 py-2 backdrop-blur">
        <h1 className="text-sm font-bold tracking-wide">⛈️ Noctalix Nowcast</h1>
        <p className="text-[11px] text-slate-400">Radar Italia · previsione +60 min</p>
      </header>
      {position && alerts && (
        <section className="absolute left-3 top-20 z-10 max-w-[70vw] rounded-xl bg-night-900/85 px-3 py-2 text-xs backdrop-blur sm:max-w-xs">
          {nowWet && (
            <p className="mb-1 text-sky-300">{nowWet.event_type === "SNOW" ? "❄️ Sta nevicando" : "🌧️ Sta piovendo"} sulla tua posizione</p>
          )}
          {next ? (
            <p>
              <span className="mr-1 inline-block h-2 w-2 rounded-full" style={{ background: EVENT_BY_ID[next.event_type]?.color }} />
              <b>{EVENT_BY_ID[next.event_type]?.label ?? next.event_type}</b> sulla tua posizione tra ~{next.lead_time_minutes} min
              <br />~{Math.round(next.estimated_rain_mmh)} mm/h · {Math.round(next.max_dbz)} dBZ
            </p>
          ) : (
            !nowWet && <p className="text-emerald-300">✓ Nessuna precipitazione prevista nei prossimi 60 min</p>
          )}
          {alerts.cap.map((c) => (
            <p key={c.identifier} className="mt-1 text-purple-300">
              📢 {CAP_EVENTS.find((e) => e.id === c.event_type)?.label ?? c.event}: {c.event}
            </p>
          ))}
        </section>
      )}
      <AlertSettings onPosition={setPosition} />
    </main>
  );
}

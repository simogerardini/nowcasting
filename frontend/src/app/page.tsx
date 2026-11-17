"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";
import AlertSettings from "@/components/AlertSettings";
import DynamicIsland, { PointAlerts } from "@/components/DynamicIsland";
import IosInstallBanner from "@/components/IosInstallBanner";
import { API_URL } from "@/lib/config";
import { Coords, registerServiceWorker, storedEndpoint, watchLocation } from "@/lib/pushManager";

const RadarMap = dynamic(() => import("@/components/RadarMap"), { ssr: false });


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

  return (
    <main className="fixed inset-0">
      <RadarMap position={position} focus={focus} />
      <IosInstallBanner />
      <DynamicIsland alerts={alerts} hasPosition={!!position} />
      <AlertSettings onPosition={setPosition} />
    </main>
  );
}

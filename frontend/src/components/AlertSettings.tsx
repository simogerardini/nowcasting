"use client";

import { useEffect, useState } from "react";
import {
  AlertPrefs,
  Coords,
  disableAlerts,
  enableAlerts,
  isIos,
  isPushSupported,
  isStandalone,
  loadPrefs,
  storedEndpoint,
  updatePrefs,
} from "@/lib/pushManager";

const LEVELS = [
  { dbz: 35, label: "Pioggia forte (≥35 dBZ)" },
  { dbz: 40, label: "Rovescio intenso (≥40 dBZ)" },
  { dbz: 45, label: "Temporale forte (≥45 dBZ)" },
  { dbz: 55, label: "Solo grandine / estremi (≥55 dBZ)" },
];

export default function AlertSettings({ onPosition }: { onPosition: (c: Coords) => void }) {
  const [open, setOpen] = useState(false);
  const [prefs, setPrefs] = useState<AlertPrefs>({ min_severity_dbz: 40, quiet_hours_start: null, quiet_hours_end: null });
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [supported, setSupported] = useState(true);

  useEffect(() => {
    setPrefs(loadPrefs());
    setEnabled(!!storedEndpoint() && typeof Notification !== "undefined" && Notification.permission === "granted");
    setSupported(isPushSupported() && (!isIos() || isStandalone()));
  }, []);

  const quiet = !!(prefs.quiet_hours_start && prefs.quiet_hours_end);

  async function toggle() {
    setBusy(true);
    setMsg(null);
    try {
      if (enabled) {
        await disableAlerts();
        setEnabled(false);
        setMsg("Avvisi disattivati");
      } else {
        const c = await enableAlerts(prefs);
        onPosition(c);
        setEnabled(true);
        setMsg("Avvisi attivi per la tua posizione ✅");
      }
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function save(next: AlertPrefs) {
    setPrefs(next);
    try {
      await updatePrefs(next);
      if (enabled) setMsg("Impostazioni salvate");
    } catch (e) {
      setMsg((e as Error).message);
    }
  }

  return (
    <>
      <button
        onClick={() => setOpen((o) => !o)}
        className={`absolute right-3 top-28 z-10 rounded-full px-4 py-2 text-sm font-semibold shadow-lg ${
          enabled ? "bg-emerald-600" : "bg-sky-600"
        }`}
      >
        {enabled ? "🔔 Avvisi attivi" : "🔕 Attiva avvisi"}
      </button>
      {open && (
        <div className="absolute right-3 top-40 z-20 w-[min(92vw,340px)] space-y-4 rounded-2xl bg-night-800/95 p-4 text-sm shadow-2xl backdrop-blur">
          <div className="flex items-center justify-between">
            <h2 className="text-base font-bold">Avvisi meteo</h2>
            <button onClick={() => setOpen(false)} aria-label="Chiudi" className="text-slate-400">✕</button>
          </div>

          {!supported && (
            <p className="rounded bg-amber-900/60 p-2 text-amber-100">
              Notifiche push non disponibili qui. {isIos() && "Su iPhone/iPad installa prima l'app nella schermata Home."}
            </p>
          )}

          <button
            disabled={busy || !supported}
            onClick={toggle}
            className={`w-full rounded-xl py-2 font-semibold disabled:opacity-50 ${enabled ? "bg-slate-600" : "bg-sky-600"}`}
          >
            {busy ? "…" : enabled ? "Disattiva avvisi" : "Attiva avvisi push + GPS"}
          </button>

          <fieldset className="space-y-1">
            <legend className="mb-1 font-semibold">Soglia di intensità</legend>
            {LEVELS.map((l) => (
              <label key={l.dbz} className="flex items-center gap-2">
                <input
                  type="radio"
                  name="sev"
                  checked={prefs.min_severity_dbz === l.dbz}
                  onChange={() => save({ ...prefs, min_severity_dbz: l.dbz })}
                />
                {l.label}
              </label>
            ))}
          </fieldset>

          <fieldset className="space-y-2">
            <label className="flex items-center gap-2 font-semibold">
              <input
                type="checkbox"
                checked={quiet}
                onChange={(e) =>
                  save(
                    e.target.checked
                      ? { ...prefs, quiet_hours_start: "23:00", quiet_hours_end: "07:00" }
                      : { ...prefs, quiet_hours_start: null, quiet_hours_end: null },
                  )
                }
              />
              Ore di silenzio
            </label>
            {quiet && (
              <div className="flex items-center gap-2">
                <input
                  type="time"
                  value={prefs.quiet_hours_start ?? ""}
                  onChange={(e) => save({ ...prefs, quiet_hours_start: e.target.value })}
                  className="rounded bg-night-700 px-2 py-1"
                />
                <span>→</span>
                <input
                  type="time"
                  value={prefs.quiet_hours_end ?? ""}
                  onChange={(e) => save({ ...prefs, quiet_hours_end: e.target.value })}
                  className="rounded bg-night-700 px-2 py-1"
                />
              </div>
            )}
            <p className="text-xs text-slate-400">Di notte vengono silenziati solo gli avvisi gialli; arancio e rosso arrivano sempre.</p>
          </fieldset>

          {msg && <p className="text-xs text-sky-200">{msg}</p>}
        </div>
      )}
    </>
  );
}

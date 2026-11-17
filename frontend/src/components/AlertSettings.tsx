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
  DEFAULT_PREFS,
} from "@/lib/pushManager";
import { CAP_EVENTS, EventSpec, LIGHTNING_EVENTS, RADAR_EVENTS } from "@/lib/events";

function Checks({
  items, selected, onToggle, cols = 1,
}: { items: EventSpec[]; selected: string[]; onToggle: (id: string, on: boolean) => void; cols?: 1 | 2 }) {
  return (
    <div className={cols === 2 ? "grid grid-cols-2 gap-x-2 gap-y-1.5" : "space-y-1.5"}>
      {items.map((e) => (
        <label key={e.id} className="flex items-start gap-2">
          <input
            type="checkbox"
            className="mt-1 accent-sky-500"
            checked={selected.includes(e.id)}
            onChange={(ev) => onToggle(e.id, ev.target.checked)}
          />
          <span>
            <span className="mr-1 inline-block h-2 w-2 rounded-full align-middle" style={{ background: e.color }} />
            {e.label}
            {e.hint && <span className="block text-xs text-slate-400">{e.hint}</span>}
          </span>
        </label>
      ))}
    </div>
  );
}

export default function AlertSettings({ onPosition }: { onPosition: (c: Coords) => void }) {
  const [open, setOpen] = useState(false);
  const [prefs, setPrefs] = useState<AlertPrefs>(DEFAULT_PREFS);
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

  function toggleType(id: string, on: boolean) {
    const set = new Set(prefs.alert_types);
    if (on) set.add(id);
    else set.delete(id);
    void save({ ...prefs, alert_types: [...set] });
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
        <div className="absolute right-3 top-40 z-20 max-h-[calc(100dvh-11rem)] w-[min(92vw,360px)] space-y-4 overflow-y-auto rounded-2xl bg-night-800/95 p-4 text-sm shadow-2xl backdrop-blur">
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

          <fieldset>
            <legend className="mb-1 font-semibold">Radar · prossimi 60 minuti</legend>
            <p className="mb-2 text-xs text-slate-400">Previsione sulla tua posizione, aggiornata ogni 10 min. Più affidabile entro 30 min.</p>
            <Checks items={RADAR_EVENTS} selected={prefs.alert_types} onToggle={toggleType} />
          </fieldset>

          <fieldset>
            <legend className="mb-1 font-semibold">Fulmini · satellite</legend>
            <p className="mb-2 text-xs text-slate-400">Fulmini osservati dal satellite europeo MTG (EUMETSAT), ritardo 2–12 min.</p>
            <Checks items={LIGHTNING_EVENTS} selected={prefs.alert_types} onToggle={toggleType} />
          </fieldset>

          <fieldset>
            <legend className="mb-1 font-semibold">Allerte ufficiali · fino a 1–2 giorni</legend>
            <p className="mb-2 text-xs text-slate-400">MeteoAlarm / Protezione Civile per la tua regione (gialla, arancione, rossa).</p>
            <Checks items={CAP_EVENTS} selected={prefs.alert_types} onToggle={toggleType} cols={2} />
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
            <p className="text-xs text-slate-400">Di notte vengono silenziati inizio pioggia, pioggia forte, neve e allerte gialle; temporali forti, grandine e allerte arancioni/rosse arrivano sempre.</p>
          </fieldset>

          {msg && <p className="text-xs text-sky-200">{msg}</p>}
        </div>
      )}
    </>
  );
}

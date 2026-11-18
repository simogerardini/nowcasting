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
import { API_URL } from "@/lib/config";
import { EVENT_BY_ID, EventSpec, LIGHTNING_EVENTS, RADAR_EVENTS } from "@/lib/events";

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

interface VerifRow { event_type: string; alerts: number; hits: number; false_alarms: number; precision: number | null }

function Reliability() {
  const [rows, setRows] = useState<VerifRow[] | null>(null);
  useEffect(() => {
    fetch(`${API_URL}/api/v1/verification?days=30`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => setRows(d?.alerts ?? []))
      .catch(() => setRows([]));
  }, []);
  if (!rows) return null;
  return (
    <fieldset>
      <legend className="mb-1 font-semibold">Affidabilità reale · ultimi 30 giorni</legend>
      {rows.length === 0 ? (
        <p className="text-xs text-slate-400">Ancora nessun avviso radar verificato.</p>
      ) : (
        <table className="w-full text-xs">
          <tbody>
            {rows.map((r) => (
              <tr key={r.event_type}>
                <td className="py-0.5">{EVENT_BY_ID[r.event_type]?.label ?? r.event_type}</td>
                <td className="text-right text-slate-300">
                  {r.precision != null ? `${Math.round(r.precision * 100)}% confermati` : "in verifica"}
                </td>
                <td className="pl-2 text-right text-slate-500">({r.hits}/{r.hits + r.false_alarms})</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="mt-1 text-xs text-slate-500">Ogni avviso viene confrontato con il radar osservato sulla tua posizione.</p>
    </fieldset>
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
        aria-label={enabled ? "Avvisi attivi: impostazioni" : "Attiva avvisi"}
        className={`absolute bottom-[max(0.6rem,env(safe-area-inset-bottom))] left-2.5 z-20 grid h-11 w-11 place-items-center rounded-full shadow-lg ring-1 ring-white/10 backdrop-blur ${
          enabled ? "bg-emerald-600/90" : "bg-slate-800/90"
        }`}
      >
        <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 0 1-3.46 0" />
          {!enabled && <path d="M3 3l18 18" />}
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-[calc(max(0.6rem,env(safe-area-inset-bottom))+3.5rem)] left-2.5 z-30 max-h-[calc(100dvh-6rem)] w-[min(92vw,360px)] space-y-4 overflow-y-auto rounded-2xl bg-night-800/95 p-4 text-sm shadow-2xl backdrop-blur">
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
            <p className="mb-2 text-xs text-slate-400">Previsione sulla tua posizione, aggiornata ogni 5–10 min. Più affidabile entro 30 min. Italia: radar Protezione Civile · Europa: radar OPERA · resto del mondo: RainViewer.</p>
            <Checks items={RADAR_EVENTS} selected={prefs.alert_types} onToggle={toggleType} />
          </fieldset>

          <fieldset>
            <legend className="mb-1 font-semibold">Fulmini · satellite</legend>
            <p className="mb-2 text-xs text-slate-400">Europa, Africa e Atlantico: satellite MTG (EUMETSAT), ritardo 2–12 min. Americhe: satelliti GOES (NOAA), ritardo ~1 min. Non disponibile in Asia e Oceania.</p>
            <Checks items={LIGHTNING_EVENTS} selected={prefs.alert_types} onToggle={toggleType} />
          </fieldset>

          <fieldset>
            <legend className="mb-1 font-semibold">Allerte ufficiali</legend>
            <p className="text-xs text-slate-400">
              Nessuna notifica dalle sole allerte ufficiali (MeteoAlarm, Protezione Civile, NWS): non sono verificabili dal
              nostro modello sul tuo punto. Le vedi nell&apos;indicatore in alto e vengono citate negli avvisi radar quando il
              nostro modello conferma l&apos;evento (temporale, pioggia forte, neve).
            </p>
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

          <Reliability />

          {msg && <p className="text-xs text-sky-200">{msg}</p>}

          <p className="border-t border-white/10 pt-3 text-[11px] leading-relaxed text-slate-500">
            Dati: radar Italia © <a className="underline" href="https://radar.protezionecivile.it" target="_blank" rel="noreferrer">Dipartimento Protezione Civile</a>
            {" "}· radar Europa ©{" "}
            <a className="underline" href="https://www.eumetnet.eu/observations/weather-radar-network/" target="_blank" rel="noreferrer">EUMETNET OPERA</a> (CC BY 4.0)
            {" "}· radar mondo RainViewer · fulmini © EUMETSAT (MTG) e NOAA (GOES GLM) · allerte MeteoAlarm (EUMETNET) e NWS (NOAA) · mappa ©{" "}
            <a className="underline" href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>, OpenMapTiles,{" "}
            <a className="underline" href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> · confini ISTAT, MeteoAlarm.
          </p>
        </div>
      )}
    </>
  );
}

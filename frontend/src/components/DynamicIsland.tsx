"use client";

import { useEffect, useMemo, useState } from "react";
import { CAP_EVENTS, EVENT_BY_ID } from "@/lib/events";

export interface PointAlerts {
  radar: {
    lead_time_minutes: number;
    severity_level: string;
    event_type: string;
    max_dbz: number;
    estimated_rain_mmh: number;
    prob: number | null;
  }[];
  cap: { identifier: string; event: string; event_type: string | null; severity: string; headline: string | null; expires?: string | null }[];
}

interface Item {
  key: string;
  icon: string;
  short: string; // compact pill text
  long: string;  // expanded text
  color: string;
  rank: number;
}

const RANK: Record<string, number> = { EXTREME: 3, SEVERE: 2, MODERATE: 1, INFO: 0 };
const CAP_LEVEL: Record<string, { name: string; icon: string; color: string; rank: number }> = {
  Minor: { name: "gialla", icon: "🟡", color: "#facc15", rank: 1 },
  Moderate: { name: "gialla", icon: "🟡", color: "#facc15", rank: 1 },
  Severe: { name: "arancione", icon: "🟠", color: "#f97316", rank: 2 },
  Extreme: { name: "rossa", icon: "🔴", color: "#dc2626", rank: 3 },
};

function buildItems(a: PointAlerts | null): Item[] {
  if (!a) return [];
  const items: Item[] = [];
  const nowWet = a.radar.find((r) => r.lead_time_minutes === 0);
  if (nowWet) {
    const snow = nowWet.event_type === "SNOW";
    items.push({
      key: "now", icon: snow ? "❄️" : "🌧️", short: snow ? "Nevica" : "Piove",
      long: `${snow ? "Sta nevicando" : "Sta piovendo"} sulla tua posizione`, color: "#38bdf8", rank: 0,
    });
  }
  const next = a.radar
    .filter((r) => r.lead_time_minutes > 0 && !(nowWet && r.event_type === nowWet.event_type))
    .sort((x, y) => (RANK[y.severity_level] ?? 0) - (RANK[x.severity_level] ?? 0) || x.lead_time_minutes - y.lead_time_minutes)[0];
  if (next) {
    const ev = EVENT_BY_ID[next.event_type];
    const pct = next.prob != null ? ` · ${Math.round(next.prob * 100)}%` : "";
    items.push({
      key: "next", icon: next.severity_level === "EXTREME" ? "🚨" : next.severity_level === "SEVERE" ? "⚠️" : "🌧️",
      short: `${ev?.label.split(" /")[0] ?? next.event_type} ~${next.lead_time_minutes}′`,
      long: `${ev?.label ?? next.event_type} sulla tua posizione tra ~${next.lead_time_minutes} min${pct}`,
      color: ev?.color ?? "#facc15", rank: RANK[next.severity_level] ?? 0,
    });
  }
  for (const c of a.cap) {
    const lvl = CAP_LEVEL[c.severity] ?? CAP_LEVEL.Moderate;
    const what = (CAP_EVENTS.find((e) => e.id === c.event_type)?.label ?? "meteo").toLowerCase();
    const until = c.expires ? ` fino al ${new Date(c.expires).toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}` : "";
    items.push({
      key: c.identifier, icon: lvl.icon, short: `Allerta ${what}`,
      long: `Allerta ${lvl.name} ${what}${until} (Protezione Civile / MeteoAlarm)`, color: lvl.color, rank: lvl.rank,
    });
  }
  return items.sort((x, y) => y.rank - x.rank);
}

/** Apple-style "Dynamic Island": a dot when all is quiet, a compact pill when there is something,
 *  tap to expand the details. */
export default function DynamicIsland({ alerts, hasPosition }: { alerts: PointAlerts | null; hasPosition: boolean }) {
  const items = useMemo(() => buildItems(alerts), [alerts]);
  const [open, setOpen] = useState(false);
  const active = items.length > 0;

  // a quiet "all clear" peek collapses by itself
  useEffect(() => {
    if (!open || active) return;
    const t = setTimeout(() => setOpen(false), 4000);
    return () => clearTimeout(t);
  }, [open, active]);

  const top = items[0];
  const state = open ? "open" : active ? "pill" : "dot";

  return (
    <div className="pointer-events-none absolute inset-x-0 top-[max(0.5rem,env(safe-area-inset-top))] z-20 flex justify-center">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-label={active ? "Avvisi meteo sulla tua posizione" : "Nessun avviso"}
        className={`pointer-events-auto overflow-hidden bg-black text-left text-white shadow-[0_4px_24px_rgba(0,0,0,0.6)] ring-1 ring-white/10 transition-all duration-500 ease-[cubic-bezier(0.2,0.9,0.3,1.2)] ${
          state === "dot" ? "h-3.5 w-3.5 rounded-full" : state === "pill" ? "h-9 max-w-[78vw] rounded-full px-3" : "w-[min(92vw,380px)] rounded-[1.75rem] px-4 py-3"
        }`}
      >
        {state === "dot" && (
          <span className={`mx-auto block h-1.5 w-1.5 rounded-full ${hasPosition ? "bg-emerald-400/70" : "bg-slate-500"}`} />
        )}
        {state === "pill" && top && (
          <span className="flex h-full items-center gap-2 whitespace-nowrap text-xs font-medium">
            <span className="h-2 w-2 shrink-0 animate-pulse rounded-full" style={{ background: top.color }} />
            <span>{top.icon}</span>
            <span className="truncate">{top.short}</span>
            {items.length > 1 && <span className="rounded-full bg-white/15 px-1.5 text-[10px]">+{items.length - 1}</span>}
          </span>
        )}
        {state === "open" && (
          <div className="space-y-2 text-sm">
            {active ? (
              items.map((it) => (
                <p key={it.key} className="flex items-start gap-2 leading-snug">
                  <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: it.color }} />
                  <span>
                    {it.icon} {it.long}
                  </span>
                </p>
              ))
            ) : (
              <p className="text-emerald-300">
                {hasPosition ? "✓ Nessuna precipitazione né allerta sulla tua posizione nei prossimi 60 min" : "Attiva la posizione per gli avvisi sul tuo punto"}
              </p>
            )}
          </div>
        )}
      </button>
    </div>
  );
}

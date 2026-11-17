// Alertable events (mirrors backend/app/events.py)
export interface EventSpec {
  id: string;
  label: string;
  hint: string;
  color: string;
}

export const RADAR_EVENTS: EventSpec[] = [
  { id: "RAIN_START", label: "Inizio pioggia", hint: "Avviso quando sta per iniziare a piovere dove sei", color: "#38bdf8" },
  { id: "HEAVY_RAIN", label: "Pioggia forte / rovescio", hint: "≥ 35 dBZ, circa 5–20 mm/h", color: "#facc15" },
  { id: "CLOUDBURST", label: "Temporale forte / nubifragio", hint: "≥ 45 dBZ, oltre 20 mm/h", color: "#f97316" },
  { id: "HAIL", label: "Grandine / downburst probabile", hint: "≥ 55 dBZ, celle molto intense", color: "#dc2626" },
  { id: "SNOW", label: "Neve", hint: "Inizio nevicata (dove il radar distingue la neve)", color: "#e0f2fe" },
];

export const CAP_EVENTS: EventSpec[] = [
  { id: "CAP_THUNDERSTORM", label: "Temporali", hint: "", color: "#a855f7" },
  { id: "CAP_RAIN", label: "Pioggia / alluvione", hint: "", color: "#a855f7" },
  { id: "CAP_WIND", label: "Vento", hint: "", color: "#a855f7" },
  { id: "CAP_SNOW_ICE", label: "Neve / ghiaccio", hint: "", color: "#a855f7" },
  { id: "CAP_FOG", label: "Nebbia", hint: "", color: "#a855f7" },
  { id: "CAP_HEAT", label: "Caldo", hint: "", color: "#a855f7" },
  { id: "CAP_COLD", label: "Freddo", hint: "", color: "#a855f7" },
  { id: "CAP_COASTAL", label: "Mareggiate", hint: "", color: "#a855f7" },
  { id: "CAP_FOREST_FIRE", label: "Incendi boschivi", hint: "", color: "#a855f7" },
  { id: "CAP_AVALANCHE", label: "Valanghe", hint: "", color: "#a855f7" },
];

export const DEFAULT_ALERT_TYPES = ["HEAVY_RAIN", "CLOUDBURST", "HAIL", "CAP_THUNDERSTORM", "CAP_RAIN", "CAP_WIND"];

export const EVENT_BY_ID: Record<string, EventSpec> = Object.fromEntries(
  [...RADAR_EVENTS, ...CAP_EVENTS].map((e) => [e.id, e]),
);

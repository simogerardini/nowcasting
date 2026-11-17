import { API_URL, getRuntimeConfig } from "./config";

export interface AlertPrefs {
  min_severity_dbz: number;
  quiet_hours_start: string | null; // "HH:MM"
  quiet_hours_end: string | null;
}

export interface Coords {
  lat: number;
  lon: number;
}

const LS_ENDPOINT = "nowcast.endpoint";
const LS_LAST = "nowcast.lastCoords";
const LS_PREFS = "nowcast.prefs";
const MOVE_THRESHOLD_M = 500;

export const DEFAULT_PREFS: AlertPrefs = { min_severity_dbz: 40, quiet_hours_start: null, quiet_hours_end: null };

export function isPushSupported(): boolean {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export function isIos(): boolean {
  if (typeof navigator === "undefined") return false;
  const ua = navigator.userAgent;
  return /iPad|iPhone|iPod/.test(ua) || (ua.includes("Macintosh") && navigator.maxTouchPoints > 1);
}

export function isStandalone(): boolean {
  if (typeof window === "undefined") return false;
  return (
    (window.navigator as Navigator & { standalone?: boolean }).standalone === true ||
    window.matchMedia("(display-mode: standalone)").matches
  );
}

export function loadPrefs(): AlertPrefs {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(LS_PREFS) || "{}") };
  } catch {
    return DEFAULT_PREFS;
  }
}

export function savePrefs(p: AlertPrefs) {
  localStorage.setItem(LS_PREFS, JSON.stringify(p));
}

export function storedEndpoint(): string | null {
  return typeof localStorage === "undefined" ? null : localStorage.getItem(LS_ENDPOINT);
}

function urlBase64ToUint8Array(base64: string): Uint8Array {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

export function haversineM(a: Coords, b: Coords): number {
  const R = 6371000;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLon = toRad(b.lon - a.lon);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

export function getPosition(): Promise<Coords> {
  return new Promise((resolve, reject) => {
    if (!("geolocation" in navigator)) return reject(new Error("Geolocalizzazione non supportata"));
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lon: p.coords.longitude }),
      (e) => reject(new Error(e.message)),
      { enableHighAccuracy: true, timeout: 20000, maximumAge: 60000 },
    );
  });
}

export async function registerServiceWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!("serviceWorker" in navigator)) return null;
  return navigator.serviceWorker.register(`/sw.js?api=${encodeURIComponent(API_URL)}`, { scope: "/" });
}

async function api(path: string, method: string, body: unknown) {
  const r = await fetch(`${API_URL}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok && r.status !== 204) throw new Error(`API ${method} ${path}: ${r.status}`);
  return r.status === 204 ? null : r.json();
}

/** Ask permission, subscribe via VAPID and register subscription + position on the backend. */
export async function enableAlerts(prefs: AlertPrefs): Promise<Coords> {
  if (!isPushSupported()) throw new Error("Notifiche push non supportate da questo browser");
  const perm = await Notification.requestPermission();
  if (perm !== "granted") throw new Error("Permesso notifiche negato");
  const [coords, cfg, reg] = await Promise.all([getPosition(), getRuntimeConfig(), registerServiceWorker()]);
  if (!reg) throw new Error("Service worker non disponibile");
  if (!cfg.publicKey) throw new Error("Chiave VAPID non disponibile");
  await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(cfg.publicKey) as BufferSource,
    });
  }
  const json = sub.toJSON();
  await api("/api/v1/subscribe", "POST", {
    subscription: { endpoint: json.endpoint, keys: json.keys },
    lat: coords.lat,
    lon: coords.lon,
    min_severity_dbz: prefs.min_severity_dbz,
    quiet_hours_start: prefs.quiet_hours_start,
    quiet_hours_end: prefs.quiet_hours_end,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });
  localStorage.setItem(LS_ENDPOINT, json.endpoint!);
  localStorage.setItem(LS_LAST, JSON.stringify(coords));
  savePrefs(prefs);
  return coords;
}

export async function updatePrefs(prefs: AlertPrefs) {
  savePrefs(prefs);
  const endpoint = storedEndpoint();
  if (!endpoint) return;
  await api("/api/v1/settings", "PATCH", {
    endpoint,
    min_severity_dbz: prefs.min_severity_dbz,
    quiet_hours_start: prefs.quiet_hours_start,
    quiet_hours_end: prefs.quiet_hours_end,
    clear_quiet_hours: !prefs.quiet_hours_start || !prefs.quiet_hours_end,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  });
}

export async function disableAlerts() {
  const endpoint = storedEndpoint();
  const reg = await navigator.serviceWorker?.getRegistration();
  const sub = await reg?.pushManager.getSubscription();
  await sub?.unsubscribe().catch(() => undefined);
  if (endpoint) await api("/api/v1/unsubscribe", "DELETE", { endpoint }).catch(() => undefined);
  localStorage.removeItem(LS_ENDPOINT);
}

/** Sync position to backend when moved more than 500 m since the last sync. */
export async function syncLocation(c: Coords): Promise<boolean> {
  const endpoint = storedEndpoint();
  if (!endpoint) return false;
  let last: Coords | null = null;
  try {
    last = JSON.parse(localStorage.getItem(LS_LAST) || "null");
  } catch {
    /* ignore */
  }
  if (last && haversineM(last, c) < MOVE_THRESHOLD_M) return false;
  try {
    await api("/api/v1/location", "PATCH", { endpoint, lat: c.lat, lon: c.lon });
  } catch (e) {
    if (String(e).includes("404")) localStorage.removeItem(LS_ENDPOINT); // subscription expired server-side
    return false;
  }
  localStorage.setItem(LS_LAST, JSON.stringify(c));
  return true;
}

/** Continuous tracking while the app is open; returns a stop function. */
export function watchLocation(onPos: (c: Coords) => void): () => void {
  if (!("geolocation" in navigator)) return () => undefined;
  const id = navigator.geolocation.watchPosition(
    (p) => {
      const c = { lat: p.coords.latitude, lon: p.coords.longitude };
      onPos(c);
      void syncLocation(c);
    },
    () => undefined,
    { enableHighAccuracy: false, maximumAge: 120000, timeout: 60000 },
  );
  return () => navigator.geolocation.clearWatch(id);
}

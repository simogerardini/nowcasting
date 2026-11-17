export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "https://api-nowcast.noctalix.com").replace(/\/$/, "");
const ENV_ASSETS = (process.env.NEXT_PUBLIC_ASSETS_URL || "https://assets-nowcast.noctalix.com").replace(/\/$/, "");
const ENV_VAPID = process.env.NEXT_PUBLIC_VAPID_PUBLIC_KEY || "";

export interface RuntimeConfig {
  publicKey: string;
  assetsUrl: string;
}

let cached: Promise<RuntimeConfig> | null = null;

/** Runtime config from the API (VAPID key + asset base), falling back to build-time env. */
export function getRuntimeConfig(): Promise<RuntimeConfig> {
  if (!cached) {
    cached = fetch(`${API_URL}/api/v1/config`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((c: RuntimeConfig) => ({
        publicKey: c.publicKey || ENV_VAPID,
        assetsUrl: c.assetsUrl?.startsWith("/") ? `${API_URL}${c.assetsUrl}` : c.assetsUrl || ENV_ASSETS,
      }))
      .catch(() => {
        cached = null;
        return { publicKey: ENV_VAPID, assetsUrl: ENV_ASSETS };
      });
  }
  return cached;
}

export const SEVERITY_COLORS: Record<string, string> = {
  MODERATE: "#facc15",
  SEVERE: "#f97316",
  EXTREME: "#dc2626",
};

export const SEVERITY_LABEL: Record<string, string> = {
  MODERATE: "Pioggia forte",
  SEVERE: "Temporale forte",
  EXTREME: "Grandine / downburst",
};

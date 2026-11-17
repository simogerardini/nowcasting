/**
 * Cloudflare Worker "nowcast-api" (Free plan) on api-nowcast.noctalix.com
 *  - reverse proxy to the Hugging Face Space (env.ORIGIN), adding the HF token for private Spaces
 *  - edge cache for /assets/* (honours origin Cache-Control) -> Space serves each asset ~once per PoP
 *  - cron keep-alive so the free Space does not go to sleep (48 h without traffic)
 * Free-plan limits (100k requests/day): beyond them requests fail, nothing is billed.
 */
const ALLOWED_METHODS = new Set(["GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS"]);

function originRequest(req, env, url) {
  const target = new URL(url.pathname + url.search, env.ORIGIN);
  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.delete("cookie");
  headers.set("X-Forwarded-For", req.headers.get("CF-Connecting-IP") || "");
  if (env.HF_TOKEN) headers.set("Authorization", `Bearer ${env.HF_TOKEN}`);
  return new Request(target, {
    method: req.method,
    headers,
    body: ["GET", "HEAD"].includes(req.method) ? undefined : req.body,
    redirect: "manual",
  });
}

export default {
  async fetch(req, env, ctx) {
    if (!ALLOWED_METHODS.has(req.method)) return new Response("Method not allowed", { status: 405 });
    const url = new URL(req.url);
    const cacheable = req.method === "GET" && url.pathname.startsWith("/assets/");
    const cache = caches.default;
    const key = new Request(url.toString(), { method: "GET" });

    if (cacheable) {
      const hit = await cache.match(key);
      if (hit) {
        const r = new Response(hit.body, hit);
        const origin = req.headers.get("Origin");
        if (origin && (env.CORS_ORIGINS || "").split(",").includes(origin)) {
          r.headers.set("Access-Control-Allow-Origin", origin);
          r.headers.set("Vary", "Origin");
        }
        r.headers.set("X-Edge-Cache", "HIT");
        return r;
      }
    }

    let resp;
    try {
      resp = await fetch(originRequest(req, env, url));
    } catch (e) {
      return new Response(JSON.stringify({ status: "origin_unreachable" }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }
    const out = new Response(resp.body, resp);
    out.headers.delete("set-cookie"); // HF auth cookies must not leak to clients
    if (cacheable && resp.status === 200 && /public/.test(resp.headers.get("Cache-Control") || "")) {
      const stored = out.clone();
      stored.headers.delete("Access-Control-Allow-Origin");
      stored.headers.delete("Vary");
      ctx.waitUntil(cache.put(key, stored));
    }
    return out;
  },

  async scheduled(_event, env, ctx) {
    const req = new Request(new URL("/api/v1/health", env.ORIGIN), {
      headers: env.HF_TOKEN ? { Authorization: `Bearer ${env.HF_TOKEN}` } : {},
    });
    ctx.waitUntil(fetch(req).then((r) => console.log("keep-alive", r.status)));
  },
};

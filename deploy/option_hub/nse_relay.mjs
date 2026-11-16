/**
 * Preloaded into Option Hub's data proxy (node --import) by deploy/option_hub/serve.mjs.
 *
 * 1. www.nseindia.com/api/* → the dashboard (GET /hub/feed/nse, hub_feed.nse()).
 *    From the droplet, NSE's bot wall (Akamai) refuses Node's own HTTP client — it fingerprints the
 *    TLS handshake, and a datacenter IP gets the strict treatment. The install smoke test on
 *    16 Nov 2026 got 502 for the NIFTY chain while, from the same server, the dashboard's python
 *    `requests` session read NSE's surveillance lists and filings. So his reads go through that
 *    session. NSE's status, content type and body come back unchanged, so his error handling
 *    (403 → 60 s back-off, last-good fallback) still works as written.
 *    His cookie warm-up (GET "/" and "/option-chain") is answered here with an empty page and no
 *    cookies: the dashboard keeps its own handshake, so nothing of his reaches NSE's homepage.
 *
 * 2. Every other *.nseindia.com host (nsearchives: lot sizes, ban list, participant files, bhavcopy)
 *    goes to NSE directly — it is not behind the bot wall (the smoke test's ban list was 200) — but
 *    WITHOUT the Cookie header. Measured 2026-11-16 (Node 22): requests carrying the cookies NSE's
 *    own first response sets (ak_bmsc / bm_mi / AKA_A2) are stalled until the client times out;
 *    the same calls without cookies answer in ~0.1 s. His code reuses a warm cookie there.
 *
 * Without HUB_FEED_TOKEN (hub run by hand, no dashboard) www.nseindia.com is also called directly,
 * minus the cookie. Nothing in Option Hub's own files is edited, so updating it is a plain checkout.
 */
const FEED = (process.env.HUB_FEED_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const TOKEN = process.env.HUB_FEED_TOKEN || "";
const realFetch = globalThis.fetch;
const WWW = new Set(["www.nseindia.com", "nseindia.com"]);
const WARMUP_PAGE = "<!doctype html><title>NSE via the dashboard</title>";

function urlOf(input) {
  try {
    return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input?.url);
  } catch {
    return null;
  }
}

function isNse(host) {
  return host === "nseindia.com" || host.endsWith(".nseindia.com");
}

/** Where a GET to NSE goes: a dashboard URL, "page" (answer the warm-up locally) or null (direct).
 *  Exported for tests. */
export function routeNse(url, token = TOKEN) {
  if (!token || !WWW.has(url.hostname)) return null;
  if (url.pathname.startsWith("/api/")) return `${FEED}/hub/feed/nse?path=${encodeURIComponent(url.pathname + url.search)}`;
  return "page";
}

function withoutCookie(init) {
  if (!init || !init.headers) return init;
  const headers = new Headers(init.headers);
  headers.delete("cookie");
  return { ...init, headers };
}

globalThis.fetch = async function nseRelay(input, init) {
  const url = urlOf(input);
  if (!url || !isNse(url.hostname)) return realFetch.call(this, input, init);
  const method = String(init?.method || input?.method || "GET").toUpperCase();
  const route = method === "GET" ? routeNse(url) : null;
  if (route === "page") {
    return new Response(WARMUP_PAGE, { status: 200, headers: { "content-type": "text/html; charset=utf-8" } });
  }
  if (route) {
    try {
      return await realFetch(route, { headers: { "X-Hub-Feed": TOKEN }, signal: init?.signal });
    } catch (e) {
      if (e?.name === "TimeoutError" || e?.name === "AbortError") throw e;
      return new Response(JSON.stringify({ error: `Dashboard NSE relay unreachable: ${String(e?.message || e).slice(0, 120)}` }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  }
  return realFetch.call(this, input, withoutCookie(init));
};

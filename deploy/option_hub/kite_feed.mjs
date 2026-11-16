/**
 * Preloaded into Option Hub's data proxy (node --import) by deploy/option_hub/serve.mjs.
 *
 * Every request his code makes to api.kite.trade is answered by the dashboard (hub_feed.py) and
 * NEVER reaches Kite:
 *   GET /instruments/NFO          → /hub/feed/instruments/NFO  (the recorder's daily NFO dump)
 *   GET /quote, /quote/ltp|ohlc   → /hub/feed/quote            (recorder minute + 30 s top-up)
 *   GET /user/profile             → /hub/feed/profile          (Broker Settings "Test connection")
 *   anything else (orders, positions, funds, margins, history, session) → 403 read-only.
 *
 * So the hub runs on the dashboard's Kite login without ever holding its token: the browser keeps
 * placeholder Zerodha values (seeded by serve.mjs), and the Authorization header they produce is
 * dropped here. HUB_FEED_TOKEN (the hub cookie token, passed in by the dashboard app that runs the
 * hub — option_hub.py serve_env) authenticates these calls to the dashboard.
 */
const FEED = (process.env.HUB_FEED_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const TOKEN = process.env.HUB_FEED_TOKEN || "";
const realFetch = globalThis.fetch;

const READ_ONLY =
  "Read-only: Option Hub here takes quotes and the contract list from the dashboard's Kite login. " +
  "Orders, positions, funds and history are not available here — use Kite itself.";

function kiteError(status, errorType, message) {
  return new Response(JSON.stringify({ status: "error", error_type: errorType, message, data: null }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function urlOf(input) {
  try {
    return new URL(typeof input === "string" ? input : input instanceof URL ? input.href : input?.url);
  } catch {
    return null;
  }
}

/** The dashboard URL that answers this Kite request, or null (= refuse). Exported for tests. */
export function routeKite(method, url) {
  if (method !== "GET") return null;
  const p = url.pathname;
  const inst = /^\/instruments\/([A-Z]{3,4})$/.exec(p);
  if (inst) return `${FEED}/hub/feed/instruments/${inst[1]}`;
  if (/^\/quote(\/(ltp|ohlc))?$/.test(p)) return `${FEED}/hub/feed/quote${url.search}`;
  if (p === "/user/profile") return `${FEED}/hub/feed/profile`;
  return null;
}

globalThis.fetch = async function kiteViaDashboard(input, init) {
  const url = urlOf(input);
  if (!url || url.hostname !== "api.kite.trade") return realFetch.call(this, input, init);
  const method = String(init?.method || input?.method || "GET").toUpperCase();
  if (!TOKEN) return kiteError(503, "GeneralException", "The hub's Kite feed is not configured (HUB_FEED_TOKEN) — re-run deploy/option_hub.sh.");
  const target = routeKite(method, url);
  if (!target) return kiteError(403, "PermissionException", READ_ONLY);
  try {
    return await realFetch(target, { headers: { "X-Hub-Feed": TOKEN }, signal: init?.signal });
  } catch (e) {
    if (e?.name === "TimeoutError" || e?.name === "AbortError") throw e;
    return kiteError(502, "NetworkException", `Dashboard feed unreachable: ${String(e?.message || e).slice(0, 120)}`);
  }
};

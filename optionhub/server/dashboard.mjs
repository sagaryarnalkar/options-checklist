/**
 * options-checklist integration — NOT part of upstream Option Hub (see OPTIONS-CHECKLIST-CHANGES.md).
 *
 * In our deployment this hub is built and run by the dashboard app (option_hub.py), which passes
 * HUB_FEED_URL + HUB_FEED_TOKEN. With them set ("dashboard mode") the hub's market data comes from
 * the dashboard instead of from the network directly:
 *
 *   Kite        every Kite REST call (zerodhaClient.kiteFetch) is answered by the dashboard's own Kite
 *               login (hub_feed.py): GET /instruments/:exchange, /quote, /quote/ltp|ohlc, /user/profile.
 *               The hub never holds a Kite token. Everything else (orders, portfolio, funds, history)
 *               is refused read-only for now.
 *   NSE www     www.nseindia.com/api/* reads (proxy-server nseJson) go through the dashboard's python
 *               `requests` session: from the droplet NSE's bot wall refuses Node's TLS client (#113).
 *               No cookie warm-up is made at all.
 *   Prev OI     Kite quotes carry no previous-session OI, so the Zerodha chain gets previous_oi /
 *               previous_close_price from NSE's official F&O bhavcopy via the dashboard (zerodha.mjs).
 *
 * Without the two variables nothing here is active and the hub behaves exactly like upstream.
 */
export const FEED_URL = (process.env.HUB_FEED_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
export const FEED_TOKEN = process.env.HUB_FEED_TOKEN || "";
export const DASHBOARD = !!FEED_TOKEN;

/** Placeholder Zerodha credentials the frontend keeps for the built-in dashboard entry. Never sent to Kite. */
export const DASHBOARD_KITE_PLACEHOLDER = "dashboard-feed";

export function feedHeaders() {
  return { "X-Hub-Feed": FEED_TOKEN };
}

// ── Kite ──

export const KITE_READ_ONLY =
  "Read-only: this hub takes quotes and the contract list from the dashboard's Kite login. " +
  "Orders, positions, funds and history are not available here yet — use Kite itself";

/** Dashboard URL answering a Kite REST call, or null (= refused read-only). */
export function kiteRoute(method, path, query = []) {
  if (String(method || "GET").toUpperCase() !== "GET") return null;
  const qs = new URLSearchParams(query).toString();
  const inst = /^\/instruments\/([A-Z]{3,4})$/.exec(path);
  if (inst) return `${FEED_URL}/hub/feed/instruments/${inst[1]}`;
  if (/^\/quote(\/(ltp|ohlc))?$/.test(path)) return `${FEED_URL}/hub/feed/quote${qs ? `?${qs}` : ""}`;
  if (path === "/user/profile") return `${FEED_URL}/hub/feed/profile`;
  return null;
}

function kiteError(status, errorType, message) {
  return new Response(JSON.stringify({ status: "error", error_type: errorType, message, data: null }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

/** A Response shaped like Kite's own for kiteFetch to parse unchanged (success envelope or Kite error). */
export async function dashboardKiteResponse(method, path, query, doFetch) {
  const url = kiteRoute(method, path, query);
  if (!url) return kiteError(403, "PermissionException", KITE_READ_ONLY);
  try {
    return await doFetch(url, { method: "GET", headers: feedHeaders() });
  } catch (e) {
    if (e?.code === "UPSTREAM_TIMEOUT") throw e;
    return kiteError(502, "NetworkException", `Dashboard feed unreachable: ${String(e?.message || e).slice(0, 120)}`);
  }
}

// ── NSE website ──

export function nseRelayUrl(path) {
  return `${FEED_URL}/hub/feed/nse?path=${encodeURIComponent(path)}`;
}

// ── Previous session (bhavcopy) for the Zerodha chain ──

const prevCache = new Map();

/** { "<strike>": { CE: {previous_oi, previous_close_price}, PE: {...} } } for one expiry, or null. */
export async function previousSession(symbol, expiry) {
  const key = `${symbol}|${expiry || ""}`;
  const hit = prevCache.get(key);
  if (hit && Date.now() - hit.at < 60_000) return hit.v;
  let v = null;
  try {
    const q = new URLSearchParams({ symbol: symbol || "" });
    if (expiry) q.set("expiry", expiry);
    const r = await fetch(`${FEED_URL}/hub/feed/prev?${q}`, { headers: feedHeaders(), signal: AbortSignal.timeout(30_000) });
    const j = await r.json();
    if (r.ok && j?.status === "success") v = j.data?.legs || null;
  } catch {
    /* the chain is still served, just without OI change */
  }
  prevCache.set(key, { at: Date.now(), v });
  if (prevCache.size > 50) prevCache.delete(prevCache.keys().next().value);
  return v;
}

/** Adds previous_oi / previous_close_price to every leg of a chain's `oc` in place; returns legs touched.
 *  A strike missing from a loaded bhavcopy was listed today → previous OI 0. */
export function addPrevious(oc, prevLegs) {
  if (!oc || typeof oc !== "object" || !prevLegs) return 0;
  let n = 0;
  for (const [strike, row] of Object.entries(oc)) {
    const p = prevLegs[String(Number(strike))];
    for (const side of ["ce", "pe"]) {
      const leg = row?.[side];
      if (!leg || typeof leg !== "object") continue;
      const q = p?.[side.toUpperCase()];
      leg.previous_oi = q ? q.previous_oi : 0;
      if (q && q.previous_close_price != null) leg.previous_close_price = q.previous_close_price;
      n++;
    }
  }
  return n;
}

// ── Kite account strip (header chip) ──

let accountCache = { at: 0, v: null };

/** The dashboard's /account (margin, today's P&L, open positions), cached 20 s — every read makes three Kite
 *  calls on the dashboard. { status, body } for the proxy's send(). */
export async function dashboardAccount() {
  if (accountCache.v && Date.now() - accountCache.at < 20_000) return { status: 200, body: accountCache.v };
  try {
    const r = await fetch(`${FEED_URL}/account`, { signal: AbortSignal.timeout(15_000), headers: { Accept: "application/json" } });
    if (!r.ok) return { status: 502, body: { error: `dashboard /account answered HTTP ${r.status}` } };
    const body = await r.json();
    accountCache = { at: Date.now(), v: body };
    return { status: 200, body };
  } catch (e) {
    return { status: 502, body: { error: `dashboard unreachable: ${String(e?.message || e).slice(0, 120)}` } };
  }
}

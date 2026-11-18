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
 *  calls on the dashboard. Only a logged-IN reading is cached: a "logged out" one must clear the moment the
 *  owner logs in (it used to linger 20 s, and the chip then waited a full minute). { status, body } for the
 *  proxy's send(). */
export async function dashboardAccount() {
  if (accountCache.v && Date.now() - accountCache.at < 20_000) return { status: 200, body: accountCache.v };
  try {
    const r = await fetch(`${FEED_URL}/account`, { signal: AbortSignal.timeout(15_000), headers: { Accept: "application/json" } });
    if (!r.ok) return { status: 502, body: { error: `dashboard /account answered HTTP ${r.status}` } };
    const body = await r.json();
    accountCache = body?.logged_in ? { at: Date.now(), v: body } : { at: 0, v: null };
    return { status: 200, body };
  } catch (e) {
    return { status: 502, body: { error: `dashboard unreachable: ${String(e?.message || e).slice(0, 120)}` } };
  }
}

// ── F&O stock quotes and session history (hub_feed.fno_stocks / hub_feed.snapshots) ──

/** GET a dashboard feed path as JSON; throws on any non-200 (callers fall back to upstream). */
export async function dashboardJson(path, timeoutMs = 30_000) {
  const r = await fetch(`${FEED_URL}${path}`, { headers: feedHeaders(), signal: AbortSignal.timeout(timeoutMs) });
  const j = await r.json().catch(() => null);
  if (!r.ok || !j) throw new Error(`dashboard ${path.split("?")[0]} answered HTTP ${r.status}${j?.message ? `: ${j.message}` : ""}`);
  return j;
}

/** /api/x/snapshots from the dashboard's per-minute recorder, in the proxy's envelope. */
export async function dashboardSnapshots(params) {
  const q = new URLSearchParams();
  for (const k of ["symbol", "expiry", "from", "to", "latest"]) {
    const v = params.get(k);
    if (v) q.set(k, v);
  }
  try {
    const data = await dashboardJson(`/hub/feed/snapshots?${q}`, 45_000);
    return { status: 200, body: { data, meta: { source: "database", asOf: Date.now(), stale: false } } };
  } catch (e) {
    return { status: 502, body: { data: null, meta: { source: "database", asOf: Date.now(), stale: true, reason: String(e?.message || e).slice(0, 200) } } };
  }
}

// ── Real Zerodha orders (the dashboard's kite_orders.py, /hub/orders/*) ──
//
// The order screens (paper-trading ticket + baskets, Orders page, 1Cliq) send order INTENTS here; the
// dashboard turns them into Kite orders with its own login and enforces every rule (allowed contracts, lot
// cap, price band, daily cap, halt, idempotency). This hub never holds a Kite token and adds no authority:
// the dashboard's routes answer only this proxy over loopback with HUB_FEED_TOKEN.

const ORDER_OPS = {
  status: { method: "GET", path: "/hub/orders/status" },
  book: { method: "GET", path: "/hub/orders/book" },
  order: { method: "GET", path: "/hub/orders/order", query: ["order_id"] },
  log: { method: "GET", path: "/hub/orders/log", query: ["limit"] },
  preview: { method: "POST", path: "/hub/orders/preview", timeoutMs: 25_000 },
  place: { method: "POST", path: "/hub/orders/place", sends: true },
  modify: { method: "POST", path: "/hub/orders/modify", sends: true },
  cancel: { method: "POST", path: "/hub/orders/cancel", sends: true },
  halt: { method: "POST", path: "/hub/orders/halt" },
};

/** Ops that change something (POST, JSON body, need the x-hz-orders header). */
export const ORDER_POST_OPS = new Set(Object.keys(ORDER_OPS).filter((k) => ORDER_OPS[k].method === "POST"));
export const ORDER_GET_OPS = new Set(Object.keys(ORDER_OPS).filter((k) => ORDER_OPS[k].method === "GET"));

/** Forward one order op to the dashboard. { status, body } for the proxy's send(); body is always
 *  { ok: boolean, ... } — refusals carry { code, message }. */
export async function dashboardOrders(op, method, params, body, doFetch = fetch) {
  const spec = ORDER_OPS[op];
  if (!spec) return { status: 404, body: { ok: false, code: "UNKNOWN_OP", message: `Unknown orders op. Use: ${Object.keys(ORDER_OPS).join(", ")}` } };
  if (spec.method !== method) return { status: 405, body: { ok: false, code: "METHOD", message: `${op} takes ${spec.method}` } };
  const q = new URLSearchParams();
  for (const k of spec.query || []) {
    const v = params?.get?.(k);
    if (v) q.set(k, v);
  }
  const qs = q.toString();
  const init = { method, headers: { ...feedHeaders(), Accept: "application/json" }, signal: AbortSignal.timeout(spec.timeoutMs ?? 15_000) };
  if (method === "POST") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body ?? {});
  }
  let r;
  try {
    r = await doFetch(`${FEED_URL}${spec.path}${qs ? `?${qs}` : ""}`, init);
  } catch (e) {
    const why = String(e?.message || e).slice(0, 120);
    return {
      status: 504,
      body: {
        ok: false,
        code: "NO_ANSWER",
        // A send that timed out may still have reached Kite: never let the screen call it failed.
        message: spec.sends
          ? `No answer from the dashboard (${why}). The order MAY have gone through: check the order book before trying again.`
          : `Dashboard unreachable: ${why}`,
      },
    };
  }
  const j = await r.json().catch(() => null);
  if (!j || typeof j !== "object") return { status: 502, body: { ok: false, code: "BAD_UPSTREAM", message: `The dashboard answered HTTP ${r.status} without JSON.` } };
  if (j.ok === undefined) {
    // FastAPI's own refusals ({"detail": ...}): auth, validation.
    const detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j).slice(0, 200);
    return { status: r.status, body: { ok: false, code: `HTTP_${r.status}`, message: detail } };
  }
  return { status: r.status, body: j };
}

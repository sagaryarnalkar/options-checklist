/**
 * Runs Mr. Chartist's Option Hub on the droplet (user-systemd unit option-hub, written by
 * deploy/option_hub.sh). Our file, not his: his checkout (~/option-hub) is never edited.
 *
 *   1. Serves his built frontend (HUB_DIR/dist) on 127.0.0.1:4001 — SPA fallback, hashed assets
 *      cached for a year, index.html / service worker never cached.
 *   2. Supervises his data proxy (proxy-server.mjs, 127.0.0.1:4002) with nse_fetch_fix.mjs and
 *      kite_feed.mjs preloaded. If the proxy exits, this exits too and systemd restarts both.
 *   4. Fronts his /api/broker-proxy (Caddy routes it here) and, for Zerodha option chains, adds
 *      previous_oi / previous_close_price to every leg from the dashboard's /hub/feed/prev (NSE's
 *      official bhavcopy). His Zerodha adapter drops those fields, so without this his chain shows
 *      OI change 0 and no build-up labels; his parser already reads both. Everything else passes
 *      through byte for byte.
 *   3. Serves SEED_PATH, the page the dashboard tab opens the hub through. His app only asks a
 *      broker for the chain when one is saved in ITS Broker Settings (browser storage), so this page
 *      saves a placeholder Zerodha entry ONCE (marker key: never again, so removing it in his
 *      settings sticks; skipped if a broker or a storage mode is already chosen), then redirects
 *      to his app. The values are placeholders — kite_feed.mjs never forwards them. Why a page
 *      under /api/ and not a <script> in his index.html: his service worker serves a cached
 *      index.html for every navigation (and updates only after its "update available" prompt), but
 *      its navigateFallbackDenylist sends /api/* navigations to the network. Caddy routes
 *      /api/_options-checklist/* here, not to his proxy.
 *
 * Caddy fronts both on hub.<domain> and answers 401 without the owner cookie (see option_hub.py).
 * Both ports bind loopback only.
 *
 * Framing: our dashboard embeds the hub in an iframe, so no X-Frame-Options. Instead
 * Content-Security-Policy frame-ancestors limits WHO may frame it to HUB_FRAME_ANCESTORS
 * (the setup script sets https://<domain>).
 */
import http from "node:http";
import { spawn } from "node:child_process";
import { createReadStream, statSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HUB_DIR = resolve(process.env.HUB_DIR || join(homedir(), "option-hub"));
const ROOT = join(HUB_DIR, "dist");
const PORT = Number(process.env.PORT || 4001);
const HOST = process.env.HOST || "127.0.0.1";
const FRAME_ANCESTORS = (process.env.HUB_FRAME_ANCESTORS || "'self'").trim();
const FEED_ON = !!process.env.HUB_FEED_TOKEN;
const FEED_URL = (process.env.HUB_FEED_URL || "http://127.0.0.1:8000").replace(/\/+$/, "");
const FEED_TOKEN = process.env.HUB_FEED_TOKEN || "";
const PROXY_PORT = Number(process.env.PROXY_PORT || 4002);
export const BROKER_PROXY_PATH = "/api/broker-proxy";

export const SEED_PATH = "/api/_options-checklist/seed";

/** Saves a placeholder Zerodha entry in his "legacy" (plain localStorage) broker format, once. */
export const ZERODHA_SEED = `(function(){try{var L=window.localStorage,M="occl_kite_feed_seeded";
if(L.getItem(M))return;var m=L.getItem("mc_broker_storage_mode");
if(m==="encrypted"||m==="session")return;var k="optionsdesk_broker_keys",l=[];
try{l=JSON.parse(L.getItem(k)||"[]")}catch(e){}if(!Array.isArray(l)||l.length)return;
L.setItem(k,JSON.stringify([{brokerId:"zerodha",values:{apiKey:"dashboard-feed",apiSecret:"not-used",accessToken:"dashboard-feed"},addedAt:new Date().toISOString(),isActive:true}]));
L.setItem(M,new Date().toISOString());}catch(e){}})();`;

/** The bootstrap page: seed (when the feed is on), then go to `next` — same-origin paths only. */
export function seedPage(feedOn = FEED_ON) {
  return `<!doctype html><meta charset="utf-8"><title>Option Hub</title><meta name="robots" content="noindex">
<script>/* options-checklist: dashboard Kite feed (deploy/option_hub/serve.mjs) */
${feedOn ? ZERODHA_SEED : ""}
(function(){var n=new URLSearchParams(location.search).get("next")||"/";
if(n.charAt(0)!=="/"||n.charAt(1)==="/"||n.charCodeAt(1)===92)n="/";location.replace(n);})();</script>`;
}

const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
  ".webp": "image/webp", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
  ".xml": "application/xml", ".woff": "font/woff", ".woff2": "font/woff2", ".map": "application/json",
};
const BASE_HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Content-Security-Policy": `frame-ancestors 'self' ${FRAME_ANCESTORS}`.replace(/'self' 'self'/, "'self'"),
};

/** Resolve a URL path to a file inside dist/, or null. Never escapes ROOT. */
export function fileFor(root, urlPath) {
  let p;
  try { p = decodeURIComponent(String(urlPath || "/").split("?")[0]); } catch { return null; }
  const full = normalize(join(root, p));
  if (full !== root && !full.startsWith(root + sep)) return null;
  try {
    const s = statSync(full);
    if (s.isFile()) return full;
    if (s.isDirectory()) {
      const idx = join(full, "index.html");
      return statSync(idx).isFile() ? idx : null;
    }
  } catch { /* missing */ }
  return null;
}

/** Adds previous-session fields to a Zerodha chain body in place; returns legs touched. A strike
 *  missing from a loaded bhavcopy was listed today → previous OI 0. */
export function addPrevious(body, prevLegs) {
  const oc = body?.data?.oc;
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

/** "17-Nov-2026" → "2026-11-17"; ISO passes; anything else → null. */
export function isoExpiry(v) {
  if (!v) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return v;
  const m = /^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/.exec(v);
  if (!m) return null;
  const mon = ["jan","feb","mar","apr","may","jun","jul","aug","sep","oct","nov","dec"].indexOf(m[2].toLowerCase());
  return mon < 0 ? null : `${m[3]}-${String(mon + 1).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
}

const _prevCache = new Map();
async function fetchPrev(symbol, expiry) {
  const key = `${symbol}|${expiry || ""}`;
  const hit = _prevCache.get(key);
  if (hit && Date.now() - hit.at < 60_000) return hit.v;
  let v = null;
  try {
    const q = new URLSearchParams({ symbol: symbol || "" });
    if (expiry) q.set("expiry", expiry);
    const r = await fetch(`${FEED_URL}/hub/feed/prev?${q}`, { headers: { "X-Hub-Feed": FEED_TOKEN }, signal: AbortSignal.timeout(30_000) });
    const j = await r.json();
    if (r.ok && j?.status === "success") v = j.data;
  } catch { /* chain still served, just without OI change */ }
  _prevCache.set(key, { at: Date.now(), v });
  return v;
}

function forwardBrokerProxy(req, res) {
  const url = new URL(req.url || "/", "http://hub.local");
  const enrich = FEED_ON && req.method === "GET" && url.searchParams.get("broker") === "zerodha"
    && url.searchParams.get("endpoint") === "option-chain";
  const up = http.request({ host: "127.0.0.1", port: PROXY_PORT, method: req.method, path: req.url, headers: req.headers }, (ur) => {
    if (!enrich || ur.statusCode !== 200 || ur.headers["content-encoding"]) {
      res.writeHead(ur.statusCode || 502, ur.headers);
      ur.pipe(res);
      return;
    }
    const chunks = [];
    ur.on("data", (c) => chunks.push(c));
    ur.on("error", () => res.destroy());
    ur.on("end", async () => {
      let text = Buffer.concat(chunks).toString("utf8");
      const headers = { ...ur.headers };
      delete headers["content-length"];
      delete headers["transfer-encoding"];
      try {
        const body = JSON.parse(text);
        if (body?.status === "success" && body?.data?.oc) {
          const prev = await fetchPrev(url.searchParams.get("symbol"), isoExpiry(url.searchParams.get("expiry")));
          if (prev && addPrevious(body, prev.legs)) text = JSON.stringify(body);
        }
      } catch { /* not JSON: pass through untouched */ }
      res.writeHead(200, headers);
      res.end(text);
    });
  });
  up.on("error", () => { if (!res.headersSent) res.writeHead(502, BASE_HEADERS).end("hub proxy down"); else res.destroy(); });
  req.pipe(up);
}

function serve(req, res) {
  const path = (req.url || "/").split("?")[0];
  if (path === BROKER_PROXY_PATH) { forwardBrokerProxy(req, res); return; }
  if (req.method !== "GET" && req.method !== "HEAD") { res.writeHead(405, BASE_HEADERS).end(); return; }
  if (path === SEED_PATH) {
    res.writeHead(200, { ...BASE_HEADERS, "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }).end(req.method === "HEAD" ? undefined : seedPage());
    return;
  }
  if (path === "/healthz") {
    const ok = !!fileFor(ROOT, "/index.html");
    res.writeHead(ok ? 200 : 503, { ...BASE_HEADERS, "Content-Type": "application/json" })
      .end(JSON.stringify({ status: ok ? "ok" : "dist missing — run deploy/option_hub.sh", dist: ROOT }));
    return;
  }
  const isAsset = path.startsWith("/assets/");
  let file = fileFor(ROOT, path);
  if (!file && (isAsset || extname(path))) { res.writeHead(404, BASE_HEADERS).end("Not found"); return; }
  if (!file) file = join(ROOT, "index.html"); // client-side routes (react-router BrowserRouter)
  const noCache = /\/(index\.html|sw\.js|registerSW\.js|manifest\.webmanifest)$/.test(file) || file.endsWith(".html");
  res.writeHead(200, {
    ...BASE_HEADERS,
    "Content-Type": TYPES[extname(file).toLowerCase()] || "application/octet-stream",
    "Cache-Control": isAsset ? "public, max-age=31536000, immutable" : noCache ? "no-cache" : "public, max-age=3600",
  });
  if (req.method === "HEAD") { res.end(); return; }
  createReadStream(file).on("error", () => res.destroy()).pipe(res);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  http.createServer((req, res) => {
    try { serve(req, res); } catch (e) {
      console.error(`option-hub static: ${e?.stack || e}`);
      if (!res.headersSent) res.writeHead(500).end("internal error"); else res.destroy();
    }
  }).listen(PORT, HOST, () => console.log(`option-hub static: http://${HOST}:${PORT} (${ROOT})`));

  const pre = ["nse_fetch_fix.mjs", "kite_feed.mjs"].flatMap((f) => ["--import", pathToFileURL(join(HERE, f)).href]);
  const proxy = spawn(process.execPath, [...pre, "proxy-server.mjs"], {
    cwd: HUB_DIR, stdio: "inherit", env: { PROXY_HOST: "127.0.0.1", ...process.env },
  });
  proxy.on("exit", (code, signal) => {
    console.error(`option-hub proxy exited (${signal || code}); exiting so systemd restarts both`);
    process.exit(code || 1);
  });
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => { proxy.kill(sig); process.exit(0); });
  process.on("exit", () => { try { proxy.kill("SIGTERM"); } catch { /* already gone */ } });
}

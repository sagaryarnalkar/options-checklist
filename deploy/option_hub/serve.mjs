/**
 * Runs the hub (optionhub/ in this repo) on the server. Started and supervised by the dashboard app
 * (option_hub.py RUNTIME builds optionhub/ into DATA_DIR/option-hub/builds/<version> and runs this
 * file with HUB_DIR set to that build).
 *
 *   1. Serves the built frontend (HUB_DIR/dist) on 127.0.0.1:4001 — SPA fallback, hashed assets
 *      cached for a year, index.html / service worker never cached.
 *   2. Supervises the data proxy (proxy-server.mjs, 127.0.0.1:4002). If it exits, this exits too
 *      and the app restarts both. The dashboard integration (Kite + NSE through the dashboard,
 *      previous-session OI) lives in optionhub/server/dashboard.mjs and runs inside that proxy.
 *   3. Two pass-through routes kept because the one-time Caddy block sends them here, not to the
 *      proxy: /api/broker-proxy (forwarded to the proxy unchanged) and SEED_PATH, the URL the
 *      dashboard tab opens — now a plain redirect into the app (the built-in Zerodha entry
 *      replaced the old browser-storage seed).
 *
 * Caddy fronts both on hub.<domain> and answers 401 without the owner cookie (see option_hub.py).
 * Both ports bind loopback only.
 *
 * Framing: our dashboard embeds the hub in an iframe, so no X-Frame-Options. Instead
 * Content-Security-Policy frame-ancestors limits WHO may frame it to HUB_FRAME_ANCESTORS
 * (the app sets https://<domain>, derived from OPTION_HUB_URL = https://hub.<domain>).
 */
import http from "node:http";
import { spawn } from "node:child_process";
import { createReadStream, statSync } from "node:fs";
import { homedir } from "node:os";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const HUB_DIR = resolve(process.env.HUB_DIR || join(homedir(), "option-hub"));
const ROOT = join(HUB_DIR, "dist");
const PORT = Number(process.env.PORT || 4001);
const HOST = process.env.HOST || "127.0.0.1";
const FRAME_ANCESTORS = (process.env.HUB_FRAME_ANCESTORS || "'self'").trim();
const PROXY_PORT = Number(process.env.PROXY_PORT || 4002);
export const BROKER_PROXY_PATH = "/api/broker-proxy";

export const SEED_PATH = "/api/_options-checklist/seed";

/** The page the dashboard tab opens: redirect to `next` — same-origin paths only. */
export function seedPage() {
  return `<!doctype html><meta charset="utf-8"><title>Option Hub</title><meta name="robots" content="noindex">
<script>(function(){var n=new URLSearchParams(location.search).get("next")||"/";
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

/** /api/broker-proxy → the proxy, unchanged (Caddy routes it here). */
function forwardBrokerProxy(req, res) {
  const up = http.request({ host: "127.0.0.1", port: PROXY_PORT, method: req.method, path: req.url, headers: req.headers }, (ur) => {
    res.writeHead(ur.statusCode || 502, ur.headers);
    ur.pipe(res);
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

  const proxy = spawn(process.execPath, ["proxy-server.mjs"], {
    cwd: HUB_DIR, stdio: "inherit", env: { PROXY_HOST: "127.0.0.1", ...process.env },
  });
  proxy.on("exit", (code, signal) => {
    console.error(`option-hub proxy exited (${signal || code}); exiting so the app restarts both`);
    process.exit(code || 1);
  });
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => { proxy.kill(sig); process.exit(0); });
  process.on("exit", () => { try { proxy.kill("SIGTERM"); } catch { /* already gone */ } });
}

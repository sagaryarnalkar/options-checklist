/**
 * Runs Mr. Chartist's Option Hub on the droplet (user-systemd unit option-hub, written by
 * deploy/option_hub.sh). Our file, not his: his checkout (~/option-hub) is never edited.
 *
 *   1. Serves his built frontend (HUB_DIR/dist) on 127.0.0.1:4001 — SPA fallback, hashed assets
 *      cached for a year, index.html / service worker never cached.
 *   2. Supervises his data proxy (proxy-server.mjs, 127.0.0.1:4002) with nse_fetch_fix.mjs
 *      preloaded. If the proxy exits, this exits too and systemd restarts both.
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

function serve(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") { res.writeHead(405, BASE_HEADERS).end(); return; }
  const path = (req.url || "/").split("?")[0];
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
  http.createServer(serve).listen(PORT, HOST, () => console.log(`option-hub static: http://${HOST}:${PORT} (${ROOT})`));

  const fix = pathToFileURL(join(HERE, "nse_fetch_fix.mjs")).href;
  const proxy = spawn(process.execPath, ["--import", fix, "proxy-server.mjs"], {
    cwd: HUB_DIR, stdio: "inherit", env: { PROXY_HOST: "127.0.0.1", ...process.env },
  });
  proxy.on("exit", (code, signal) => {
    console.error(`option-hub proxy exited (${signal || code}); exiting so systemd restarts both`);
    process.exit(code || 1);
  });
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => { proxy.kill(sig); process.exit(0); });
}

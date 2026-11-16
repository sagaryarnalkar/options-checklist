/**
 * Preloaded into Option Hub's data proxy (node --import) by deploy/option_hub/serve.mjs.
 *
 * NSE's bot manager (Akamai) stalls every www.nseindia.com request that carries the cookies its own
 * first response sets (ak_bmsc / bm_mi / AKA_A2: no browser JS ran to validate them), until the
 * client times out. The SAME API calls without cookies answer in ~0.2 s. Option Hub warms a cookie
 * session first, so out of the box its indices / option chain / FII-DII calls all time out and it
 * falls back to sample data. Measured 2026-11-16 (Node 22): "/" 200 then "/option-chain" + cookie
 * TIMEOUT, "/api/allIndices" + cookie TIMEOUT, "/api/allIndices" no cookie 200,
 * "/api/option-chain-v3" no cookie 200 (306 KB).
 *
 * nsearchives.nseindia.com (lot sizes, participant files) behaves the same way: cookie-less 0.09 s,
 * with the warmed cookie (Option Hub reuses it there once warm) a 30 s timeout.
 *
 * This wraps global fetch and drops the Cookie header for *.nseindia.com only. Nothing in Option
 * Hub's own files is edited, so updating it is a plain git checkout.
 */
const realFetch = globalThis.fetch;

function hostOf(input) {
  try {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input?.url;
    return url ? new URL(url).hostname : "";
  } catch {
    return "";
  }
}

globalThis.fetch = function nseCookielessFetch(input, init) {
  const host = hostOf(input);
  if ((host === "nseindia.com" || host.endsWith(".nseindia.com")) && init && init.headers) {
    const headers = new Headers(init.headers);
    headers.delete("cookie");
    init = { ...init, headers };
  }
  return realFetch.call(this, input, init);
};

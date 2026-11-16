/**
 * options-checklist dashboard mode (server/dashboard.mjs): with HUB_FEED_TOKEN set, Kite calls go to
 * the dashboard and never to api.kite.trade, anything but market-data reads is refused read-only
 * without a network call, and the Zerodha chain carries previous-session OI. The modules read the
 * environment at import, so each test imports fresh copies after setting it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createFetchStub, loadFixtures } from "./brokers/contractHarness.mjs";

const FEED = "http://127.0.0.1:8019";
const TOKEN = "test-hub-token";
const fx = loadFixtures("zerodha");
const realFetch = globalThis.fetch;

/** His optionChain fixture, re-pointed from api.kite.trade to the dashboard feed. */
function chainResponsesViaDashboard() {
  return fx.byCapability.optionChain.responses.map((r) => {
    const out = { ...r };
    if (out.url) out.url = out.url.replace("https://api.kite.trade/instruments/", `${FEED}/hub/feed/instruments/`);
    if (out.urlPattern) out.urlPattern = out.urlPattern.replace("^https://api\\.kite\\.trade/quote", `^${FEED.replace(/\./g, "\\.")}/hub/feed/quote`);
    return out;
  });
}

async function dashboardModules() {
  vi.resetModules();
  const zerodha = await import("./brokers/zerodha.mjs");
  const client = await import("./brokers/zerodhaClient.mjs");
  const dashboard = await import("./dashboard.mjs");
  const instruments = await import("./brokers/zerodhaInstruments.mjs");
  instruments.resetZerodhaInstrumentCache?.();
  return { zerodha, client, dashboard };
}

describe("dashboard mode", () => {
  beforeEach(() => {
    process.env.HUB_FEED_URL = FEED;
    process.env.HUB_FEED_TOKEN = TOKEN;
  });
  afterEach(() => {
    delete process.env.HUB_FEED_URL;
    delete process.env.HUB_FEED_TOKEN;
    globalThis.fetch = realFetch;
    vi.resetModules();
  });

  it("routes only market-data reads to the dashboard", async () => {
    const { dashboard } = await dashboardModules();
    expect(dashboard.DASHBOARD).toBe(true);
    expect(dashboard.kiteRoute("GET", "/instruments/NFO")).toBe(`${FEED}/hub/feed/instruments/NFO`);
    expect(dashboard.kiteRoute("GET", "/quote", [["i", "NFO:X"], ["i", "NSE:NIFTY 50"]])).toBe(`${FEED}/hub/feed/quote?i=NFO%3AX&i=NSE%3ANIFTY+50`);
    expect(dashboard.kiteRoute("GET", "/quote/ltp", [["i", "NSE:NIFTY 50"]])).toMatch(/\/hub\/feed\/quote\?/);
    expect(dashboard.kiteRoute("GET", "/user/profile")).toBe(`${FEED}/hub/feed/profile`);
    for (const [m, p] of [["POST", "/orders/regular"], ["GET", "/orders"], ["GET", "/portfolio/positions"], ["GET", "/user/margins"],
      ["GET", "/instruments/historical/123/minute"], ["POST", "/session/token"], ["DELETE", "/orders/regular/1"]]) {
      expect(dashboard.kiteRoute(m, p)).toBeNull();
    }
  });

  it("kiteFetch asks the dashboard with the feed token and no Kite credentials", async () => {
    const { client } = await dashboardModules();
    const stub = createFetchStub([{ method: "GET", url: `${FEED}/hub/feed/profile`, json: { status: "success", data: { user_id: "DASHBOARD" } } }]);
    globalThis.fetch = stub.fetch;
    const data = await client.kiteFetch("/user/profile", {});
    expect(data.user_id).toBe("DASHBOARD");
    expect(stub.calls).toHaveLength(1);
    expect(stub.calls[0].headers["X-Hub-Feed"]).toBe(TOKEN);
    expect(JSON.stringify(stub.calls[0].headers)).not.toMatch(/Authorization|token /);
  });

  it("refuses orders, portfolio and funds read-only without any network call", async () => {
    const { client, zerodha } = await dashboardModules();
    const stub = createFetchStub([]);
    globalThis.fetch = stub.fetch;
    const err = await client.kiteFetch("/orders/regular", {}, { method: "POST", form: { tradingsymbol: "X" } }).catch((e) => e);
    expect(err).toBeInstanceOf(client.KiteApiError);
    expect(err.errorType).toBe("PermissionException");
    expect(err.message).toMatch(/Read-only/);
    await expect(client.kiteFetch("/portfolio/positions", {})).rejects.toThrow(/Read-only/);
    await expect(client.kiteFetch("/user/margins", {})).rejects.toThrow(/Read-only/);
    await expect(zerodha.capabilities).toBeTruthy();
    expect(stub.calls).toHaveLength(0);
  });

  it("a logged-out dashboard surfaces as a Kite TokenException pointing at the dashboard login", async () => {
    const { client } = await dashboardModules();
    globalThis.fetch = createFetchStub([{ method: "GET", url: `${FEED}/hub/feed/profile`, status: 403,
      json: { status: "error", error_type: "TokenException", message: "The dashboard is not logged in to Kite today", data: null } }]).fetch;
    const err = await client.kiteFetch("/user/profile", {}).catch((e) => e);
    expect(err.code).toBe("UPSTREAM_AUTH");
    expect(err.message).toMatch(/Log in to Kite on the dashboard/);
    expect(err.message).not.toMatch(/paste it in Broker Settings/);
  });

  it("the Zerodha chain comes through the dashboard and carries previous-session OI", async () => {
    const { zerodha } = await dashboardModules();
    const prev = { status: "success", data: { session: "2026-10-23", expiry: "2026-10-27", legs: {
      25000: { CE: { previous_oi: 111, previous_close_price: 101.5 }, PE: { previous_oi: 222, previous_close_price: 88 } },
      25050: { CE: { previous_oi: 333, previous_close_price: 70 } },
    } } };
    const stub = createFetchStub([...chainResponsesViaDashboard(),
      { method: "GET", url: `${FEED}/hub/feed/prev?symbol=NIFTY&expiry=2026-10-27`, json: prev }]);
    globalThis.fetch = stub.fetch;
    const out = await zerodha.fetchOptionChain({}, "NIFTY", "2026-10-27");
    expect(out.status).toBe("success");
    const oc = out.data.oc;
    expect(oc["25000"].ce.previous_oi).toBe(111);
    expect(oc["25000"].ce.previous_close_price).toBe(101.5);
    expect(oc["25000"].pe.previous_oi).toBe(222);
    expect(oc["25050"].ce.previous_oi).toBe(333);
    expect(oc["25050"].pe.previous_oi).toBe(0); // absent from a loaded bhavcopy = listed today
    expect(stub.calls.every((c) => c.url.startsWith(FEED))).toBe(true);
    expect(stub.calls.some((c) => c.url.includes("kite.trade"))).toBe(false);
  });

  it("a missing bhavcopy leaves the chain as it was", async () => {
    const { zerodha } = await dashboardModules();
    globalThis.fetch = createFetchStub([...chainResponsesViaDashboard(),
      { method: "GET", url: `${FEED}/hub/feed/prev?symbol=NIFTY&expiry=2026-10-27`, status: 503,
        json: { status: "error", error_type: "GeneralException", message: "bhavcopy unavailable", data: null } }]).fetch;
    const out = await zerodha.fetchOptionChain({}, "NIFTY", "2026-10-27");
    expect(out.status).toBe("success");
    expect(out.data.oc["25000"].ce).not.toHaveProperty("previous_oi");
  });
});

describe("without the dashboard (upstream behaviour)", () => {
  it("kiteFetch still calls api.kite.trade with the user's own token", async () => {
    delete process.env.HUB_FEED_TOKEN;
    vi.resetModules();
    const client = await import("./brokers/zerodhaClient.mjs");
    const stub = createFetchStub([{ method: "GET", url: "https://api.kite.trade/user/profile", json: { status: "success", data: { user_id: "AB1234" } } }]);
    globalThis.fetch = stub.fetch;
    try {
      const data = await client.kiteFetch("/user/profile", { apiKey: "k", accessToken: "t" });
      expect(data.user_id).toBe("AB1234");
      expect(stub.calls[0].headers.Authorization).toBe("token k:t");
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});

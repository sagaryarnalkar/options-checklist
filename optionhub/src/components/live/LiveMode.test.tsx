/**
 * options-checklist: Paper Trading's Live · Zerodha mode end to end in the page, with the terminal proxy's
 * /api/dashboard/orders stubbed (no network, no real order). Our build only (VITE_DASHBOARD_KITE=1).
 */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { generateSampleChain, getLotSize } from "@/lib/options";
import { installResizeObserver, renderPage } from "@/components/positions/testUtils";

const SPOT = 25_000;
const chains = new Map<string, ReturnType<typeof chainData>>();
function chainData(symbol: string, expiry: string) {
  const { chain } = generateSampleChain({ symbol, spot: SPOT, expiry, seed: 3 });
  return { chain, spotPrice: SPOT, expiries: [], expiry, lotSize: getLotSize(symbol, expiry) ?? 1, stepSize: 50, maxPain: SPOT, totalCEOI: 0, totalPEOI: 0, isLive: true, afterHours: false, source: "nse" as const, cachedAt: null, asOf: Date.now(), stale: false };
}
vi.mock("@/hooks/useMarketData", () => ({
  useLiveOptionChain: (symbol: string, expiry?: string) => {
    const k = `${symbol}|${expiry}`;
    if (!chains.has(k)) chains.set(k, chainData(symbol, expiry ?? ""));
    return { data: chains.get(k), isLoading: false, isError: false };
  },
  useAllIndices: () => ({ data: { vix: { value: 13.5, change: 0, changePercent: 0, high: 14, low: 13 } }, isLoading: false }),
}));
vi.mock("@/lib/volatility/api", () => ({
  fetchVixHistory: () => Promise.resolve({ data: [], source: "nse", asOf: Date.now(), stale: false }),
  cachedBhavPoints: () => [],
}));

const STATUS = {
  ok: true, enabled: true, halted: false, logged_in: true, sent_today: 0, static_ip_note: "static IP",
  rules: { underlyings: ["NIFTY", "BANKNIFTY"], instrument_types: ["CE", "PE", "FUT"], max_lots: 25, order_types: ["LIMIT", "SL", "MARKET", "SL-M"], products: ["NRML", "MIS"], max_orders_per_day: 300, max_basket_legs: 12, band: { option_pct: 50, option_min_rupees: 20, future_pct: 3 } },
};

interface Sent { op: string; body: Record<string, unknown> | null }
let sent: Sent[] = [];

function installOrdersApi() {
  sent = [];
  let n = 0;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const m = /\/api\/dashboard\/orders\?op=([a-z]+)/.exec(url);
    const json = (b: unknown, status = 200) => new Response(JSON.stringify(b), { status, headers: { "content-type": "application/json" } });
    if (!m) return json({ error: "not stubbed" }, 404);
    const op = m[1];
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : null;
    sent.push({ op, body });
    if (op === "status") return json(STATUS);
    if (op === "preview") {
      const legs = (body?.legs as Array<Record<string, unknown>>) ?? [];
      return json({
        ok: true, enabled: true, halted: false, margin: { initial: 150000, final: 42000 },
        legs: legs.map((l) => ({ ok: true, tradingsymbol: `NIFTY26NOV${l.strike}${l.opt_type}`, lot_size: 65, lots: l.lots, quantity: Number(l.lots) * 65, ltp: 100, band: [50, 150], tick_size: 0.05, side: l.side, order_type: l.order_type })),
      });
    }
    if (op === "place") return json({ ok: true, order_id: `25111800000${++n}`, quantity: 65 });
    if (op === "order") {
      const id = new URL(url, "http://x").searchParams.get("order_id");
      return json({ ok: true, order: { order_id: id, status: "COMPLETE", tradingsymbol: "X", transaction_type: "BUY", order_type: "MARKET", product: "NRML", quantity: 65, filled_quantity: 65, average_price: 100.5 } });
    }
    return json({ ok: false, code: "UNEXPECTED", message: op }, 500);
  }));
}

async function loadPage() {
  vi.resetModules();
  vi.stubEnv("VITE_DASHBOARD_KITE", "1");
  const { default: Page } = await import("@/pages/PaperTrading");
  return Page;
}

beforeAll(installResizeObserver);
beforeEach(() => {
  localStorage.clear();
  installOrdersApi();
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("Paper Trading · Live (Zerodha) mode", () => {
  it("is absent outside our build", async () => {
    vi.resetModules();
    const { default: Page } = await import("@/pages/PaperTrading");
    renderPage(<Page />, "/paper-trading");
    expect(screen.queryByRole("radio", { name: /Live · Zerodha/ })).toBeNull();
  });

  it("the ticket opens the confirmation; nothing is sent until Live Trading is on and the real-money box is ticked", async () => {
    const Page = await loadPage();
    renderPage(<Page />, "/paper-trading");
    fireEvent.click(await screen.findByRole("radio", { name: /Live · Zerodha/ }));
    expect(screen.getByText(/send/, { selector: "span" })).toBeInTheDocument();
    const buy = await screen.findByRole("button", { name: /Buy on Zerodha/ });
    await waitFor(() => expect(buy).toBeEnabled()); // no paper max-loss needed in Live mode
    fireEvent.click(buy);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/NIFTY26NOV\d+CE/)).toBeInTheDocument();
    expect(within(dialog).getByText(/1 lot × 65 = 65/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Live trading is off in Broker settings/)).toBeInTheDocument();
    const send = within(dialog).getByRole("button", { name: "Place real order" });
    fireEvent.click(within(dialog).getByRole("checkbox"));
    expect(send).toBeDisabled(); // still off: Live Trading not enabled
    expect(sent.filter((s) => s.op === "place")).toHaveLength(0);
    // Turning Live Trading on (Broker settings) and reopening:
    localStorage.setItem("optionsdesk_live_trading_enabled", "true");
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    fireEvent.click(buy);
    const dialog2 = await screen.findByRole("dialog");
    await within(dialog2).findByText(/NIFTY26NOV\d+CE/);
    const send2 = within(dialog2).getByRole("button", { name: "Place real order" });
    expect(send2).toBeDisabled(); // the box starts unticked on every open
    fireEvent.click(within(dialog2).getByRole("checkbox"));
    await waitFor(() => expect(send2).toBeEnabled());
    fireEvent.click(send2);
    // The leg's own line and the outcome line both report the fill.
    expect(await within(dialog2).findAllByText(/Filled 65 @ 100.50/, {}, { timeout: 4000 })).toHaveLength(2);
    const places = sent.filter((s) => s.op === "place");
    expect(places).toHaveLength(1);
    expect(places[0].body).toMatchObject({ source: "ticket", leg: { underlying: "NIFTY", side: "BUY", lots: 1, order_type: "MARKET", product: "NRML" } });
    expect(String(places[0].body?.intent)).toMatch(/^tk[a-z0-9]{4,18}$/);
    // Paper account untouched.
    expect(JSON.parse(localStorage.getItem("mc_paper_v1") ?? "{}").positions ?? []).toHaveLength(0);
  });

  it("a basket goes buys first, one leg at a time", async () => {
    const { getExpiries } = await import("@/lib/options");
    const { encodeLegs } = await import("@/components/option-chain/basket");
    const expiry = getExpiries("NIFTY", new Date(), 1)[0].date;
    const enc = encodeLegs([
      { symbol: "NIFTY", expiry, strike: 25000, type: "CE", side: "SELL", lots: 2, premium: 100, iv: null },
      { symbol: "NIFTY", expiry, strike: 25500, type: "CE", side: "BUY", lots: 2, premium: 40, iv: null },
    ]);
    localStorage.setItem("optionsdesk_live_trading_enabled", "true");
    const Page = await loadPage();
    renderPage(<Page />, `/paper-trading?legs=${enc}`);
    fireEvent.click(await screen.findByRole("radio", { name: /Live · Zerodha/ }));
    fireEvent.click(await screen.findByRole("button", { name: /Place on Zerodha/ }));
    const dialog = await screen.findByRole("dialog");
    await within(dialog).findByText("NIFTY26NOV25000CE");
    expect(within(dialog).getByText(/Kite margin/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("checkbox"));
    const send = within(dialog).getByRole("button", { name: "Place 2 real orders" });
    await waitFor(() => expect(send).toBeEnabled());
    fireEvent.click(send);
    expect(await within(dialog).findByText(/All 2 legs filled/, {}, { timeout: 6000 })).toBeInTheDocument();
    const places = sent.filter((s) => s.op === "place").map((s) => s.body?.leg as Record<string, unknown>);
    expect(places.map((l) => `${l.side} ${l.strike}`)).toEqual(["BUY 25500", "SELL 25000"]);
    const ops = sent.map((s) => s.op).filter((o) => o === "place" || o === "order");
    expect(ops).toEqual(["place", "order", "place", "order"]);
  });
});

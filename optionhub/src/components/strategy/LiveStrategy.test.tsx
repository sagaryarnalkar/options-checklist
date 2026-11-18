/**
 * options-checklist (#126): the Strategy Builder opens a live Kite position (?positions= from the dashboard's
 * Positions tab) and Ask Claude sends the screen's legs + figures. The terminal proxy is stubbed: no network.
 */
import { cloneElement, type ReactElement } from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import { generateSampleChain, getExpiries } from "@/lib/options";
import type { OptionChainHookData } from "@/hooks/useMarketData";

const SPOT = 25000;
const FRONT = getExpiries("NIFTY", new Date(), 4)[0].date;
const pageChain = generateSampleChain({ symbol: "NIFTY", spot: SPOT, expiry: FRONT, strikes: 61 });
const hookData: OptionChainHookData = {
  chain: pageChain.chain, spotPrice: SPOT, expiries: [], expiry: FRONT, lotSize: 65, stepSize: 50, maxPain: SPOT, totalCEOI: 0, totalPEOI: 0,
  isLive: true, afterHours: false, source: "nse", cachedAt: null, asOf: Date.now(), stale: false,
};
vi.mock("@/hooks/useMarketData", () => ({
  useLiveOptionChain: () => ({ data: hookData, isLoading: false, isFetching: false, isError: false, error: null, refetch: vi.fn() }),
  useExpiryList: () => ({ data: undefined, isLoading: false }),
  useLiveIndices: () => ({ data: undefined }),
  isLiveSession: () => false,
  buildSampleChainData: vi.fn(async () => hookData),
}));
vi.mock("@/lib/marketApi", () => ({ fetchLiveOptionChain: vi.fn(async () => ({ chain: hookData.chain, spotPrice: SPOT, source: "nse", stale: false })) }));
vi.mock("recharts", async (importOriginal) => {
  const mod = await importOriginal<typeof import("recharts")>();
  return { ...mod, ResponsiveContainer: ({ children }: { children: ReactElement }) => cloneElement(children, { width: 800, height: 300 }) };
});

const BOOK = {
  ok: true,
  as_of: "2026-11-18T14:32:05+05:30",
  groups: [
    {
      underlying: "NIFTY", spot: SPOT, pnl: -1234,
      legs: [
        { tradingsymbol: "NIFTYX25500CE", qty: -130, lot_size: 65, avg: 61.25, kind: "option", opt_type: "CE", strike: 25500, expiry: FRONT, iv: 12.5 },
        { tradingsymbol: "NIFTYX25700CE", qty: 130, lot_size: 65, avg: 22.4, kind: "option", opt_type: "CE", strike: 25700, expiry: FRONT, iv: 12 },
      ],
    },
  ],
};
let asked: Record<string, unknown>[] = [];

beforeAll(() => {
  if (!("ResizeObserver" in window)) {
    class RO { observe() {} unobserve() {} disconnect() {} }
    Object.defineProperty(window, "ResizeObserver", { value: RO, configurable: true, writable: true });
    Object.defineProperty(globalThis, "ResizeObserver", { value: RO, configurable: true, writable: true });
  }
  Element.prototype.scrollIntoView ??= () => {};
});
beforeEach(() => {
  localStorage.clear();
  asked = [];
  const enc = new TextEncoder();
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/api/dashboard/positions")) return new Response(JSON.stringify(BOOK), { headers: { "content-type": "application/json" } });
    if (url.endsWith("/api/dashboard/assistant")) {
      asked.push(JSON.parse(String(init?.body)));
      const sse = 'event: meta\ndata: {"type":"meta","conversation_id":"c1"}\n\nevent: status\ndata: {"type":"status","text":"Running a scenario"}\n\nevent: text\ndata: {"type":"text","text":"**Hold** the spread.\\n- delta is small"}\n\nevent: done\ndata: {"type":"done","conversation_id":"c1"}\n\n';
      return new Response(new ReadableStream({ start(c) { c.enqueue(enc.encode(sse)); c.close(); } }), { headers: { "content-type": "text/event-stream" } });
    }
    return new Response("{}", { status: 404 });
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

async function renderBuilder(url: string, ours = true) {
  vi.resetModules();
  if (ours) vi.stubEnv("VITE_DASHBOARD_KITE", "1");
  const { default: StrategyBuilder } = await import("@/pages/StrategyBuilder");
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[url]}>
          <Routes>
            <Route path="/strategy-builder" element={<StrategyBuilder />} />
          </Routes>
        </MemoryRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("Strategy Builder · live positions and Ask Claude", () => {
  it("?positions=NIFTY opens the live position with Kite's entry prices; Ask Claude gets the legs and the (empty) change list", async () => {
    await renderBuilder("/strategy-builder?positions=NIFTY");
    const strip = (await screen.findByText(/Live NIFTY position/)).closest("[role=status]") as HTMLElement;
    expect(within(strip).getByText(/unchanged/)).toBeInTheDocument();
    expect(within(strip).getByText(/14:32 IST/)).toBeInTheDocument();
    fireEvent.click(within(strip).getByRole("button", { name: /Ask Claude/ }));
    const sheet = await screen.findByRole("dialog");
    fireEvent.click(within(sheet).getByRole("button", { name: /Assess this position/ }));
    expect(await within(sheet).findByText("Hold")).toBeInTheDocument(); // **bold** rendered, not raw markdown
    expect(within(sheet).getByText("delta is small")).toBeInTheDocument();
    await waitFor(() => expect(asked).toHaveLength(1));
    const req = asked[0] as { scope: string; message: string; builder: { legs: Array<Record<string, unknown>>; live_position: { changes: string[]; kite_pnl: number } } };
    expect(req.scope).toBe("NIFTY");
    expect(req.builder.legs.map((l) => `${l.side} ${l.strike} ${l.qty} @${l.entry_price}`)).toEqual(["SELL 25500 130 @61.25", "BUY 25700 130 @22.4"]);
    expect(req.builder.live_position).toMatchObject({ changes: [], kite_pnl: -1234 });
  });

  it("has neither button outside our build", async () => {
    await renderBuilder("/strategy-builder", false);
    await screen.findByRole("heading", { name: /Strategy Builder/ });
    expect(screen.queryByRole("button", { name: /Live positions/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Ask Claude/ })).toBeNull();
  });
});

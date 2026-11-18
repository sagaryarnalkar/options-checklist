/**
 * options-checklist: the client side of real Zerodha orders (lib/kiteOrders.ts). No network: fetch and the
 * place/wait steps are stubbed. The dashboard (kite_orders.py) is the authority and has its own tests.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  KiteOrderError,
  cancelKiteOrder,
  newIntentId,
  orderBasketLegs,
  placeKiteBasket,
  placeKiteOrder,
  waitForKiteOrder,
  type KiteLeg,
  type KiteOrderRow,
} from "./kiteOrders";
import type { LiveArmToken } from "./liveArm";

const TOKEN = {} as LiveArmToken;
const leg = (side: "BUY" | "SELL", strike: number): KiteLeg => ({ underlying: "NIFTY", expiry: "2026-11-24", strike, opt_type: "CE", side, lots: 1, order_type: "MARKET" });
const row = (order_id: string, status: string, extra: Partial<KiteOrderRow> = {}): KiteOrderRow => ({
  order_id, status, tradingsymbol: "X", transaction_type: "BUY", order_type: "MARKET", product: "NRML", quantity: 65, ...extra,
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("intent ids", () => {
  it("are 6-20 letters/digits with their prefix, and differ", () => {
    const ids = new Set(Array.from({ length: 200 }, () => newIntentId("bk")));
    expect(ids.size).toBe(200);
    for (const id of ids) expect(id).toMatch(/^bk[a-z0-9]{4,18}$/);
  });
});

describe("basket order", () => {
  it("puts every BUY before any SELL, keeping their own order", () => {
    const out = orderBasketLegs([leg("SELL", 1), leg("BUY", 2), leg("SELL", 3), leg("BUY", 4)]);
    expect(out.map((l) => `${l.side}${l.strike}`)).toEqual(["BUY2", "BUY4", "SELL1", "SELL3"]);
  });
});

describe("placeKiteBasket", () => {
  it("sends one leg at a time and only after the previous one is COMPLETE", async () => {
    const events: string[] = [];
    const place = vi.fn(async (l: KiteLeg, _s, intent: string) => {
      events.push(`place ${l.strike} ${intent}`);
      return { ok: true as const, order_id: `o${l.strike}` };
    });
    const wait = vi.fn(async (id: string) => {
      events.push(`wait ${id}`);
      return row(id, "COMPLETE", { average_price: 10 });
    });
    const res = await placeKiteBasket([leg("BUY", 1), leg("SELL", 2)], ["i1aaaaaa", "i2aaaaaa"], TOKEN, { place, wait });
    expect(events).toEqual(["place 1 i1aaaaaa", "wait o1", "place 2 i2aaaaaa", "wait o2"]);
    expect(res.stoppedAt).toBeNull();
    expect(res.steps.map((s) => s.state)).toEqual(["done", "done"]);
  });

  it("stops at a Kite rejection and never sends the rest", async () => {
    const place = vi.fn(async (l: KiteLeg) => ({ ok: true as const, order_id: `o${l.strike}` }));
    const wait = vi.fn(async (id: string) => (id === "o2" ? row(id, "REJECTED", { status_message: "Insufficient funds" }) : row(id, "COMPLETE")));
    const res = await placeKiteBasket([leg("BUY", 1), leg("BUY", 2), leg("SELL", 3)], ["a1aaaaaa", "a2aaaaaa", "a3aaaaaa"], TOKEN, { place, wait });
    expect(place).toHaveBeenCalledTimes(2);
    expect(res.stoppedAt).toBe(1);
    expect(res.reason).toMatch(/Insufficient funds/);
    expect(res.steps.map((s) => s.state)).toEqual(["done", "failed", "skipped"]);
  });

  it("stops when a leg is still open at its limit (a short is never sent before its hedge fills)", async () => {
    const place = vi.fn(async (l: KiteLeg) => ({ ok: true as const, order_id: `o${l.strike}` }));
    const wait = vi.fn(async (id: string) => row(id, "OPEN"));
    const res = await placeKiteBasket([leg("BUY", 1), leg("SELL", 2)], ["b1aaaaaa", "b2aaaaaa"], TOKEN, { place, wait });
    expect(place).toHaveBeenCalledTimes(1);
    expect(res.steps.map((s) => s.state)).toEqual(["open", "skipped"]);
    expect(res.reason).toMatch(/still open/);
  });

  it("a refusal stops the basket; a no-answer is reported as 'may have gone through', not as failed", async () => {
    const refused = await placeKiteBasket([leg("BUY", 1), leg("SELL", 2)], ["c1aaaaaa", "c2aaaaaa"], TOKEN, {
      place: async () => {
        throw new KiteOrderError("TOO_MANY_LOTS", "26 lots is over the 25-lot limit per order.", 422);
      },
      wait: async () => null,
    });
    expect(refused.steps[0]).toMatchObject({ state: "failed", uncertain: false });
    expect(refused.reason).toMatch(/refused/);
    const lost = await placeKiteBasket([leg("BUY", 1)], ["d1aaaaaa"], TOKEN, {
      place: async () => {
        throw new KiteOrderError("NO_ANSWER", "No answer from the dashboard. The order MAY have gone through.", 504);
      },
      wait: async () => null,
    });
    expect(lost.steps[0]).toMatchObject({ state: "failed", uncertain: true });
    expect(lost.reason).toMatch(/may have gone through/);
  });
});

describe("waitForKiteOrder", () => {
  it("polls until a terminal status", async () => {
    const states = ["OPEN", "OPEN", "COMPLETE"];
    const fetchOrder = vi.fn(async (id: string) => row(id, states.shift() ?? "COMPLETE"));
    const out = await waitForKiteOrder("o1", { timeoutMs: 5000, intervalMs: 1, fetchOrder });
    expect(out?.status).toBe("COMPLETE");
    expect(fetchOrder).toHaveBeenCalledTimes(3);
  });
  it("returns the last state when time runs out", async () => {
    const out = await waitForKiteOrder("o1", { timeoutMs: 5, intervalMs: 10, fetchOrder: async (id) => row(id, "TRIGGER PENDING") });
    expect(out?.status).toBe("TRIGGER PENDING");
  });
});

describe("the wire", () => {
  it("POSTs the intent, source and leg as JSON with the x-hz-orders header", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ ok: true, order_id: "251118000001" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const out = await placeKiteOrder(leg("BUY", 25500), "ticket", "tkabc12345", TOKEN);
    expect(out.order_id).toBe("251118000001");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/dashboard\/orders\?op=place$/);
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["x-hz-orders"]).toBe("1");
    expect(JSON.parse(String(init.body))).toEqual({ intent: "tkabc12345", source: "ticket", leg: leg("BUY", 25500) });
  });

  it("turns refusals into KiteOrderError with the dashboard's code and words", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ok: false, code: "HALTED", message: "Live orders are HALTED" }), { status: 403 })));
    const err = await cancelKiteOrder("1", "cxabc12345").catch((e) => e);
    expect(err).toBeInstanceOf(KiteOrderError);
    expect(err).toMatchObject({ code: "HALTED", status: 403, message: "Live orders are HALTED", uncertain: false });
  });

  it("a network failure on a send says the order MAY have gone through", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }));
    const err = await placeKiteOrder(leg("SELL", 1), "oneclick", "ocabc12345", TOKEN).catch((e) => e);
    expect(err.code).toBe("NETWORK");
    expect(err.uncertain).toBe(true);
    expect(err.message).toMatch(/MAY have gone through/);
  });
});

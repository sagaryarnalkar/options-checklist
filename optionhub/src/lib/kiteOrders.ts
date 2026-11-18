/**
 * options-checklist: REAL Zerodha orders through the dashboard's own Kite login.
 *
 * Path: these functions → this terminal's proxy (/api/dashboard/orders, server/dashboard.mjs) → the dashboard
 * (kite_orders.py) → Kite. The dashboard is the authority: it alone holds the Kite login and re-checks every
 * order (NIFTY/BANKNIFTY options and futures only, ≤ 25 lots, lot size from Kite's own instrument list, price
 * band around LTP, daily cap, halt switch). The checks in these screens are conveniences on top.
 *
 * Gates on this side, mirroring liveArm.ts:
 *   - placeKiteOrder / placeKiteBasket / modifyKiteOrder take a LiveArmToken, which only armLive() can mint
 *     (Live Trading switched on in Broker settings; one-click arming for the 1Cliq keys).
 *   - every send carries an intent id minted when the confirm dialog OPENS (not at the click), which the
 *     dashboard uses as the Kite order tag and a unique key: a double-click or a retry never sends twice.
 *
 * Only built for our dashboard (VITE_DASHBOARD_KITE=1); kiteOrdersAvailable() is false anywhere else.
 */
import { PROXY_BASE } from "@/lib/api/proxyClient";
import { isDashboardKiteBuild } from "@/lib/brokerConfig";
import type { LiveArmToken } from "@/lib/liveArm";

export type KiteSide = "BUY" | "SELL";
export type KiteOrderType = "LIMIT" | "SL" | "MARKET" | "SL-M";
export type KiteProduct = "NRML" | "MIS";
export type KiteInstrumentType = "CE" | "PE" | "FUT";
export type KiteOrderSource = "ticket" | "basket" | "oneclick" | "orders";

/** One order as the dashboard takes it. The contract is named either by tradingsymbol or by its parts. */
export interface KiteLeg {
  underlying?: string;
  expiry?: string;
  strike?: number;
  opt_type?: KiteInstrumentType;
  tradingsymbol?: string;
  side: KiteSide;
  order_type: KiteOrderType;
  product?: KiteProduct;
  validity?: "DAY" | "IOC";
  /** Whole lots. Quantity is lots × Kite's lot size, worked out by the dashboard. */
  lots: number;
  price?: number;
  trigger_price?: number;
}

export interface KiteOrdersRules {
  underlyings: string[];
  instrument_types: string[];
  max_lots: number;
  order_types: KiteOrderType[];
  products: KiteProduct[];
  max_orders_per_day: number;
  max_basket_legs: number;
  band: { option_pct: number; option_min_rupees: number; future_pct: number };
}

export interface KiteOrdersStatus {
  ok: true;
  enabled: boolean;
  halted: boolean;
  logged_in: boolean | null;
  sent_today: number | null;
  rules: KiteOrdersRules;
  static_ip_note: string;
}

export interface KitePreviewLeg {
  ok: boolean;
  code?: string;
  message?: string;
  tradingsymbol?: string;
  underlying?: string;
  expiry?: string;
  strike?: number;
  opt_type?: KiteInstrumentType;
  side?: KiteSide;
  order_type?: KiteOrderType;
  product?: KiteProduct;
  lot_size?: number;
  lots?: number;
  quantity?: number;
  price?: number | null;
  trigger_price?: number | null;
  tick_size?: number;
  ltp?: number | null;
  band?: [number, number] | null;
}

export interface KitePreview {
  ok: boolean;
  legs: KitePreviewLeg[];
  margin: { initial?: number | null; final?: number | null; error?: string } | null;
  enabled: boolean;
  halted: boolean;
}

export interface KitePlaced {
  ok: true;
  order_id: string;
  tradingsymbol?: string;
  quantity?: number;
  lots?: number;
  lot_size?: number;
  duplicate?: boolean;
  message?: string;
}

/** A row of Kite's own order book (fields the screens read). */
export interface KiteOrderRow {
  order_id: string;
  status: string;
  status_message?: string | null;
  tradingsymbol: string;
  exchange?: string;
  transaction_type: KiteSide;
  order_type: string;
  product: string;
  variety?: string;
  validity?: string;
  quantity: number;
  filled_quantity?: number;
  pending_quantity?: number;
  price?: number;
  trigger_price?: number;
  average_price?: number;
  order_timestamp?: string | null;
  tag?: string | null;
}

/** A refusal: from the dashboard's rules (code + its own words), Kite's (code KITE_REFUSED), or the wire. */
export class KiteOrderError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "KiteOrderError";
    this.code = code;
    this.status = status;
  }
  /** True when the order may still have reached Kite (no answer). Never present those as "failed". */
  get uncertain(): boolean {
    return this.code === "NO_ANSWER" || this.code === "IN_FLIGHT" || this.code === "NETWORK";
  }
}

export const TERMINAL_STATUSES = new Set(["COMPLETE", "REJECTED", "CANCELLED"]);

export function kiteOrdersAvailable(): boolean {
  return isDashboardKiteBuild();
}

/** 6–20 letters/digits: the dashboard's idempotency key and the Kite order tag. */
export function newIntentId(prefix: "tk" | "bk" | "oc" | "md" | "cx"): string {
  const t = Date.now().toString(36);
  let r = "";
  const bytes = new Uint8Array(6);
  try {
    crypto.getRandomValues(bytes);
  } catch {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  }
  for (const b of bytes) r += (b % 36).toString(36);
  return `${prefix}${t}${r}`.slice(0, 20);
}

const ENDPOINT = `${PROXY_BASE}/api/dashboard/orders`;

async function call<T>(op: string, opts: { body?: unknown; query?: Record<string, string> } = {}): Promise<T> {
  const q = new URLSearchParams({ op, ...(opts.query ?? {}) });
  const init: RequestInit = { cache: "no-store" };
  if (opts.body !== undefined) {
    init.method = "POST";
    init.headers = { "Content-Type": "application/json", "x-hz-orders": "1" };
    init.body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(`${ENDPOINT}?${q}`, init);
  } catch (e) {
    const sends = op === "place" || op === "modify" || op === "cancel";
    throw new KiteOrderError(
      "NETWORK",
      sends
        ? `No answer (${(e as Error).message}). The order MAY have gone through: check the order book before trying again.`
        : `Could not reach the terminal's proxy (${(e as Error).message}).`,
      0,
    );
  }
  const j = (await res.json().catch(() => null)) as (T & { ok?: boolean; code?: string; message?: string; error?: string }) | null;
  if (!res.ok || !j || j.ok === false) {
    throw new KiteOrderError(j?.code ?? `HTTP_${res.status}`, j?.message ?? j?.error ?? `HTTP ${res.status}`, res.status);
  }
  return j;
}

export function fetchKiteOrdersStatus(): Promise<KiteOrdersStatus> {
  return call<KiteOrdersStatus>("status");
}

/** Resolve and check legs without sending anything: exact contracts, quantities, LTP, verdicts, Kite margin. */
export function previewKiteOrders(legs: KiteLeg[]): Promise<KitePreview> {
  return call<KitePreview>("preview", { body: { legs } });
}

/** Places ONE real order. Unreachable without a LiveArmToken (armLive in liveArm.ts). */
export function placeKiteOrder(leg: KiteLeg, source: KiteOrderSource, intent: string, token: LiveArmToken): Promise<KitePlaced> {
  void token;
  return call<KitePlaced>("place", { body: { intent, source, leg } });
}

export function modifyKiteOrder(
  orderId: string,
  changes: { order_type?: "LIMIT" | "SL"; price?: number; trigger_price?: number; lots?: number },
  intent: string,
  token: LiveArmToken,
): Promise<{ ok: true; order_id: string }> {
  void token;
  return call("modify", { body: { intent, order_id: orderId, ...changes } });
}

/** Cancelling only reduces risk: no arming needed, and it works while orders are halted. */
export function cancelKiteOrder(orderId: string, intent: string): Promise<{ ok: true; order_id: string }> {
  return call("cancel", { body: { intent, order_id: orderId } });
}

export async function fetchKiteOrderBook(): Promise<{ orders: KiteOrderRow[]; enabled: boolean; halted: boolean }> {
  return call("book");
}

export async function fetchKiteOrder(orderId: string): Promise<KiteOrderRow> {
  const j = await call<{ order: KiteOrderRow }>("order", { query: { order_id: orderId } });
  return j.order;
}

export function setKiteOrdersHalted(halted: boolean): Promise<{ ok: true; halted: boolean }> {
  return call("halt", { body: { halted } });
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Poll one order until Kite reports COMPLETE / REJECTED / CANCELLED, or the time runs out (then the last state). */
export async function waitForKiteOrder(
  orderId: string,
  { timeoutMs = 8000, intervalMs = 700, fetchOrder = fetchKiteOrder }: { timeoutMs?: number; intervalMs?: number; fetchOrder?: (id: string) => Promise<KiteOrderRow> } = {},
): Promise<KiteOrderRow | null> {
  const until = Date.now() + timeoutMs;
  let last: KiteOrderRow | null = null;
  for (;;) {
    try {
      last = await fetchOrder(orderId);
      if (TERMINAL_STATUSES.has(String(last.status).toUpperCase())) return last;
    } catch {
      /* keep polling: the order book can lag the place call by a moment */
    }
    if (Date.now() + intervalMs > until) return last;
    await sleep(intervalMs);
  }
}

/** Entry order for a basket: every BUY before any SELL (stable). Buying first never leaves a short without its
 *  hedge: for an entry the long wing protects the short; for a close, buying back the short goes before
 *  selling the long. */
export function orderBasketLegs<T extends { side: KiteSide }>(legs: readonly T[]): T[] {
  return [...legs.filter((l) => l.side === "BUY"), ...legs.filter((l) => l.side === "SELL")];
}

export type BasketStep =
  | { state: "waiting" }
  | { state: "sending" }
  | { state: "sent"; orderId: string }
  | { state: "done"; orderId: string; order: KiteOrderRow }
  | { state: "open"; orderId: string; order: KiteOrderRow | null }
  | { state: "failed"; message: string; orderId?: string; uncertain?: boolean }
  | { state: "skipped" };

export interface BasketOutcome {
  steps: BasketStep[];
  /** Index (in the SENT order) where the basket stopped, or null when every leg completed. */
  stoppedAt: number | null;
  reason: string | null;
}

/**
 * Places a basket ONE leg at a time, BUY legs first, and moves on only after Kite reports the previous leg
 * COMPLETE. It stops at the first refusal, rejection or still-open leg and never unwinds or retries on its
 * own: what was filled stays filled and the screen says exactly which legs went. `legs` must already be in
 * send order (orderBasketLegs); `intents` has one intent id per leg, minted when the dialog opened.
 */
export async function placeKiteBasket(
  legs: KiteLeg[],
  intents: string[],
  token: LiveArmToken,
  {
    onStep,
    waitMs = 8000,
    place = placeKiteOrder,
    wait = waitForKiteOrder,
  }: {
    onStep?: (i: number, step: BasketStep) => void;
    waitMs?: number;
    place?: typeof placeKiteOrder;
    wait?: typeof waitForKiteOrder;
  } = {},
): Promise<BasketOutcome> {
  const steps: BasketStep[] = legs.map(() => ({ state: "waiting" }));
  const set = (i: number, s: BasketStep) => {
    steps[i] = s;
    onStep?.(i, s);
  };
  const stop = (i: number, reason: string): BasketOutcome => {
    for (let k = i + 1; k < legs.length; k++) set(k, { state: "skipped" });
    return { steps, stoppedAt: i, reason };
  };
  for (let i = 0; i < legs.length; i++) {
    set(i, { state: "sending" });
    let orderId: string;
    try {
      orderId = (await place(legs[i], "basket", intents[i], token)).order_id;
    } catch (e) {
      const err = e instanceof KiteOrderError ? e : new KiteOrderError("ERROR", (e as Error).message, 0);
      set(i, { state: "failed", message: err.message, uncertain: err.uncertain });
      return stop(i, err.uncertain ? `Leg ${i + 1}: no answer, it may have gone through. Check the order book.` : `Leg ${i + 1} was refused: ${err.message}`);
    }
    set(i, { state: "sent", orderId });
    const order = await wait(orderId, { timeoutMs: waitMs });
    const status = String(order?.status ?? "").toUpperCase();
    if (status === "COMPLETE" && order) {
      set(i, { state: "done", orderId, order });
      continue;
    }
    if (status === "REJECTED" || status === "CANCELLED") {
      set(i, { state: "failed", orderId, message: order?.status_message || status });
      return stop(i, `Leg ${i + 1} was ${status.toLowerCase()} by Kite: ${order?.status_message || "no reason given"}`);
    }
    set(i, { state: "open", orderId, order });
    return stop(i, `Leg ${i + 1} is still ${order?.status ? order.status.toLowerCase() : "unconfirmed"} at your price, so the rest were not sent. Manage it on the Orders page.`);
  }
  return { steps, stoppedAt: null, reason: null };
}

/** Kite product for the terminal's product words. */
export function kiteProductFor(p: string | undefined): KiteProduct {
  return p === "INTRADAY" || p === "MIS" ? "MIS" : "NRML";
}

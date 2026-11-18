/**
 * options-checklist: the dashboard's LIVE Kite positions (positions.py, via this terminal's proxy
 * /api/dashboard/positions) as Strategy Builder legs, and how the builder's legs have drifted from them.
 *
 * Entry price = Kite's average price, so the builder's payoff and "P&L now" are measured from what you actually
 * paid or received. Legs are marked manual so "refresh to LTP" never overwrites that entry.
 */
import { PROXY_BASE } from "@/lib/api/proxyClient";
import { formatDateIST } from "@/lib/options";
import type { Leg } from "@/lib/options";

export interface LivePositionLeg {
  tradingsymbol: string;
  exchange?: string;
  product?: string;
  qty: number;
  lots?: number | null;
  lot_size?: number;
  avg?: number | null;
  ltp?: number | null;
  price?: number | null;
  pnl?: number | null;
  kind: "option" | "future" | "equity" | "unsupported";
  opt_type?: "CE" | "PE";
  strike?: number;
  expiry?: string | null;
  iv?: number | null;
}

export interface LivePositionGroup {
  underlying: string;
  spot?: number | null;
  legs: LivePositionLeg[];
  pnl?: number | null;
  nearest_expiry?: string | null;
}

export interface LiveBook {
  ok: boolean;
  as_of?: string;
  groups?: LivePositionGroup[];
  reason?: string;
  logged_in?: boolean;
}

export async function fetchLiveBook(): Promise<LiveBook> {
  const res = await fetch(`${PROXY_BASE}/api/dashboard/positions`, { cache: "no-store" });
  const j = (await res.json().catch(() => null)) as LiveBook | null;
  if (!j) return { ok: false, reason: `The terminal's proxy answered HTTP ${res.status}.` };
  return j;
}

/** Option and futures legs of one underlying, ready for the builder; anything else is listed in `skipped`. */
export function positionLegs(group: LivePositionGroup): { legs: Leg[]; skipped: string[] } {
  const legs: Leg[] = [];
  const skipped: string[] = [];
  for (const p of group.legs) {
    if (!p.qty) continue;
    const priced = (p.kind === "option" && (p.opt_type === "CE" || p.opt_type === "PE") && p.strike) || p.kind === "future";
    if (!priced || !p.expiry) {
      skipped.push(p.tradingsymbol);
      continue;
    }
    const units = Math.abs(p.qty);
    const lot = p.lot_size && p.lot_size > 0 ? p.lot_size : 1;
    // A quantity that is not a whole number of lots (a lot-size revision mid-contract) keeps its exact units.
    const whole = Number.isInteger(units / lot);
    const entry = p.avg && p.avg > 0 ? p.avg : p.price ?? p.ltp ?? 0;
    legs.push({
      symbol: group.underlying,
      type: p.kind === "future" ? "FUT" : (p.opt_type as "CE" | "PE"),
      side: p.qty > 0 ? "BUY" : "SELL",
      strike: p.kind === "future" ? 0 : Number(p.strike),
      expiry: p.expiry,
      lots: whole ? units / lot : units,
      lotSize: whole ? lot : 1,
      premium: Number(entry),
      iv: typeof p.iv === "number" && p.iv > 0 ? p.iv / 100 : undefined,
      enabled: true,
    });
  }
  return { legs, skipped };
}

function legLabel(l: Leg): string {
  const what = l.type === "FUT" ? "FUT" : `${l.strike} ${l.type}`;
  return `${l.side} ${what} ${formatDateIST(l.expiry)}`;
}

const keyOf = (l: Leg) => `${l.type}|${l.type === "FUT" ? 0 : l.strike}|${l.expiry}|${l.side}`;
const units = (l: Leg) => l.lots * l.lotSize;

interface Agg {
  leg: Leg;
  units: number;
  on: boolean;
}

/** Per contract and side: total units of the enabled legs (Kite can list one contract twice, NRML and MIS). */
function aggregate(legs: Array<Leg & { enabled?: boolean }>): Map<string, Agg> {
  const m = new Map<string, Agg>();
  for (const l of legs) {
    const on = l.enabled !== false;
    const prev = m.get(keyOf(l));
    m.set(keyOf(l), prev ? { leg: prev.leg, units: prev.units + (on ? units(l) : 0), on: prev.on || on } : { leg: l, units: on ? units(l) : 0, on });
  }
  return m;
}

/** Plain-words differences between the live position and the legs on screen (empty = unchanged). */
export function diffFromLive(live: Leg[], current: Array<Leg & { enabled?: boolean }>): string[] {
  const out: string[] = [];
  const was = aggregate(live);
  const now = aggregate(current);
  for (const [k, w] of was) {
    const c = now.get(k);
    if (!c) out.push(`removed ${legLabel(w.leg)} (${w.units} qty)`);
    else if (!c.on) out.push(`switched off ${legLabel(w.leg)} (excluded from the payoff)`);
    else if (c.units !== w.units) out.push(`${legLabel(w.leg)}: quantity ${w.units} → ${c.units}`);
    else if (Math.abs(c.leg.premium - w.leg.premium) > 0.005) out.push(`${legLabel(w.leg)}: entry price ${w.leg.premium} → ${c.leg.premium} (edited)`);
  }
  for (const [k, c] of now) {
    if (!was.has(k) && c.on) out.push(`added ${legLabel(c.leg)} × ${c.units} qty at ${c.leg.premium}`);
  }
  return out;
}

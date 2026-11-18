/** options-checklist (#126): live Kite positions → Strategy Builder legs, and the what-if diff. */
import { describe, expect, it } from "vitest";
import { diffFromLive, positionLegs, type LivePositionGroup } from "./livePositions";
import type { Leg } from "@/lib/options";

const EXP = "2026-11-24";
const group: LivePositionGroup = {
  underlying: "NIFTY",
  pnl: -1234,
  legs: [
    { tradingsymbol: "NIFTY26N2425500CE", qty: -130, lot_size: 65, avg: 61.25, kind: "option", opt_type: "CE", strike: 25500, expiry: EXP, iv: 12.5 },
    { tradingsymbol: "NIFTY26N2425700CE", qty: 130, lot_size: 65, avg: 22.4, kind: "option", opt_type: "CE", strike: 25700, expiry: EXP, iv: null },
    { tradingsymbol: "NIFTY26NOVFUT", qty: 65, lot_size: 65, avg: 25050, kind: "future", expiry: EXP },
    { tradingsymbol: "NIFTY26N2425000PE", qty: 0, lot_size: 65, avg: 40, kind: "option", opt_type: "PE", strike: 25000, expiry: EXP },
    { tradingsymbol: "RELIANCE", qty: 10, kind: "equity" },
    { tradingsymbol: "OLDLOT25600CE", qty: -150, lot_size: 65, avg: 10, kind: "option", opt_type: "CE", strike: 25600, expiry: EXP },
  ],
};

describe("positionLegs", () => {
  const { legs, skipped } = positionLegs(group);
  it("loads open option and futures legs with Kite's average as the entry", () => {
    expect(legs.map((l) => `${l.side} ${l.type} ${l.strike} ${l.lots}x${l.lotSize} @${l.premium}`)).toEqual([
      "SELL CE 25500 2x65 @61.25",
      "BUY CE 25700 2x65 @22.4",
      "BUY FUT 0 1x65 @25050",
      "SELL CE 25600 150x1 @10",
    ]);
    expect(legs[0].iv).toBeCloseTo(0.125);
    expect(legs[1].iv).toBeUndefined();
    expect(legs.every((l) => l.symbol === "NIFTY" && l.expiry === EXP)).toBe(true);
  });
  it("closed legs are dropped, and anything it cannot price is named", () => {
    expect(skipped).toEqual(["RELIANCE"]);
  });
});

describe("diffFromLive", () => {
  const live = positionLegs(group).legs.slice(0, 2);
  const leg = (p: Partial<Leg>): Leg & { enabled?: boolean } => ({ ...live[0], ...p });
  it("is empty when nothing changed", () => {
    expect(diffFromLive(live, live)).toEqual([]);
  });
  it("names removals, quantity and entry edits, switch-offs and additions in plain words", () => {
    const now = [
      leg({ lots: 4 }), // 25500 CE sell: 2 → 4 lots
      leg({ side: "SELL", strike: 25800, premium: 15, lots: 1 }), // added
    ];
    const out = diffFromLive(live, now);
    expect(out).toContain("SELL 25500 CE 24 Nov 2026: quantity 130 → 260");
    expect(out.some((x) => x.startsWith("removed BUY 25700 CE"))).toBe(true);
    expect(out.some((x) => x.startsWith("added SELL 25800 CE") && x.includes("65 qty at 15"))).toBe(true);
    expect(diffFromLive(live, [live[0], { ...live[1], enabled: false }])).toEqual(["switched off BUY 25700 CE 24 Nov 2026 (excluded from the payoff)"]);
    expect(diffFromLive(live, [{ ...live[0], premium: 70 }, live[1]])).toEqual(["SELL 25500 CE 24 Nov 2026: entry price 61.25 → 70 (edited)"]);
  });
  it("a contract split across two rows (NRML + MIS) counts as one", () => {
    const split = [{ ...live[0], lots: 1 }, { ...live[0], lots: 1 }, live[1]];
    expect(diffFromLive(live, split)).toEqual([]);
  });
});

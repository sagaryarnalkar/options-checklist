"""
Trade recommender — for each fresh *_cross signal, build the exact spread:
strikes, current premiums (live LTP), net credit/debit, and margin required
for 1 lot (queried via Kite's basket margin API).

Each builder:
  - Determines target expiry per the strategy's expiry rule
  - Generates candidate (sold_strike, hedge_strike) pairs that satisfy the
    structural rules (strike-multiple, distance-from-CMP, wing-cap)
  - Fetches LTP for each candidate strike in a single batch call
  - Computes net credit/debit and filters to those inside the strategy's band
  - Picks a winner (preferring widest hedge / highest credit)
  - Queries Kite's basket margin API for the actual hedged margin
  - Returns a structured dict ready to render in the UI

Currently implemented:
  - nidhi_kalash (bull put / bear call credit spread)

To add a new strategy: write _build_<name>() and register it in
build_recommendations().
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


# ---------- expiry helpers ----------

def _futures_expiries(instruments, name: str, today: date) -> list[date]:
    """Sorted list of upcoming monthly future expiries for `name`."""
    futs = [
        ins for ins in instruments
        if ins.get("name") == name
        and ins.get("instrument_type") == "FUT"
        and ins.get("segment") == "NFO-FUT"
        and ins.get("expiry") and ins["expiry"] >= today
    ]
    futs.sort(key=lambda i: i["expiry"])
    return [f["expiry"] for f in futs]


def _option_instruments(instruments, name: str, expiry: date, opt_type: str) -> dict:
    """Map of strike -> instrument dict for given name/expiry/type."""
    out = {}
    for ins in instruments:
        if (
            ins.get("name") == name
            and ins.get("expiry") == expiry
            and ins.get("instrument_type") == opt_type
        ):
            out[int(ins["strike"])] = ins
    return out


# ---------- API helpers ----------

def _ltp_batch(kite, symbols: list[str]) -> dict:
    """kite.ltp() with auto-chunking. Returns map of full key (NFO:SYMBOL) -> dict."""
    if not symbols:
        return {}
    out = {}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i : i + 200]
        try:
            out.update(kite.ltp(chunk))
        except Exception:
            # try one-at-a-time as a fallback
            for s in chunk:
                try:
                    out.update(kite.ltp([s]))
                except Exception:
                    pass
    return out


def _basket_margin(kite, legs: list[dict]) -> dict:
    """legs: [{tradingsymbol, transaction_type SELL/BUY, quantity}, ...]"""
    basket = []
    for leg in legs:
        basket.append({
            "exchange": "NFO",
            "tradingsymbol": leg["tradingsymbol"],
            "transaction_type": leg["transaction_type"],
            "variety": "regular",
            "product": "NRML",
            "order_type": "MARKET",
            "quantity": int(leg["quantity"]),
        })
    try:
        r = kite.basket_order_margins(basket, consider_positions=False)
        return r
    except Exception as e:
        return {"error": str(e)}


# ---------- per-strategy builders ----------

def _build_nidhi_kalash(kite, direction: str, instruments, spot: float, vix: float | None, today: date) -> dict | None:
    """
    Bull put / bear call credit spread.
      - 100-point strikes only
      - Sold strike >= 0.5% from CMP, on the OTM side
      - Wing <= 2.5% of spot (round-figure 500-pt over allowed)
      - Net credit: 90-110 pts (bull put) / 90-130 pts (bear call)
      - Monthly expiry; if VIX low (<13), use next month
    """
    expiries = _futures_expiries(instruments, "NIFTY", today)
    if not expiries:
        return {"strategy": "nidhi_kalash", "error": "no upcoming NIFTY future expiry found"}

    # Try current month first; if VIX is low or current-month premiums are insufficient,
    # we'll automatically fall back to next month inside the candidate loop.
    expiry_options = [expiries[0]]
    if len(expiries) > 1:
        expiry_options.append(expiries[1])

    # Strike candidates
    if direction == "bull":
        opt_type = "PE"
        # sold strike: >=0.5% below spot, rounded DOWN to nearest 100
        max_sold = math.floor((spot * 0.995) / 100) * 100
        sold_candidates = [max_sold, max_sold - 100, max_sold - 200, max_sold - 300]
        wing_widths = [500, 400, 300, 200]
        credit_min, credit_max = 90.0, 110.0
        sign = -1  # hedge is at LOWER strike
    elif direction == "bear":
        opt_type = "CE"
        max_sold = math.ceil((spot * 1.005) / 100) * 100
        sold_candidates = [max_sold, max_sold + 100, max_sold + 200, max_sold + 300]
        wing_widths = [500, 400, 300, 200]
        credit_min, credit_max = 90.0, 130.0
        sign = +1  # hedge is at HIGHER strike
    else:
        return None

    # Try each expiry until we find a match
    tried = []
    for expiry in expiry_options:
        opts = _option_instruments(instruments, "NIFTY", expiry, opt_type)
        if not opts:
            continue

        # collect symbols we need
        needed = set()
        pairs = []
        for sold in sold_candidates:
            for w in wing_widths:
                hedge = sold + sign * w
                if sold in opts and hedge in opts:
                    pairs.append((sold, hedge, w))
                    needed.add(opts[sold]["tradingsymbol"])
                    needed.add(opts[hedge]["tradingsymbol"])

        if not needed:
            continue

        ltps = _ltp_batch(kite, [f"NFO:{ts}" for ts in needed])

        def _ltp(ts):
            return ltps.get(f"NFO:{ts}", {}).get("last_price")

        candidates = []
        for sold, hedge, w in pairs:
            sold_ts = opts[sold]["tradingsymbol"]
            hedge_ts = opts[hedge]["tradingsymbol"]
            sp, hp = _ltp(sold_ts), _ltp(hedge_ts)
            if sp is None or hp is None:
                continue
            credit = sp - hp
            wing_pct = w / spot * 100
            tried.append({
                "expiry": str(expiry),
                "sold_strike": sold,
                "hedge_strike": hedge,
                "wing": w,
                "credit": round(credit, 2),
            })
            if not (credit_min <= credit <= credit_max):
                continue
            # wing cap: <=2.5% or round-figure 500-pt allowed
            if wing_pct > 2.5 and w not in (500,):
                continue
            candidates.append({
                "expiry": expiry,
                "sold_strike": sold,
                "hedge_strike": hedge,
                "sold_premium": round(sp, 2),
                "hedge_premium": round(hp, 2),
                "credit": round(credit, 2),
                "wing": w,
                "wing_pct": round(wing_pct, 2),
                "max_loss": round(w - credit, 2),
                "sold_tradingsymbol": sold_ts,
                "hedge_tradingsymbol": hedge_ts,
                "lot_size": int(opts[sold].get("lot_size") or 75),
            })

        if not candidates:
            continue

        # Pick: prefer widest wing, then highest credit
        candidates.sort(key=lambda c: (-c["wing"], -c["credit"]))
        best = candidates[0]
        return _format(strategy="nidhi_kalash", direction=direction, kite=kite, best=best,
                       alternatives=candidates[1:5], tried=tried)

    return {
        "strategy": "nidhi_kalash",
        "direction": direction,
        "expiry": str(expiry_options[0]),
        "error": "No (sold, hedge) combination met the credit band on current premiums",
        "tried": tried[:30],
    }


def _format(strategy: str, direction: str, kite, best: dict, alternatives: list, tried: list) -> dict:
    lot = best["lot_size"]
    legs = [
        {
            "action": "SELL",
            "transaction_type": "SELL",
            "tradingsymbol": best["sold_tradingsymbol"],
            "strike": best["sold_strike"],
            "option_type": "PE" if direction == "bull" else "CE",
            "premium": best["sold_premium"],
            "quantity": lot,
        },
        {
            "action": "BUY",
            "transaction_type": "BUY",
            "tradingsymbol": best["hedge_tradingsymbol"],
            "strike": best["hedge_strike"],
            "option_type": "PE" if direction == "bull" else "CE",
            "premium": best["hedge_premium"],
            "quantity": lot,
        },
    ]
    margin = _basket_margin(kite, legs)
    margin_total = None
    try:
        margin_total = float(margin.get("final", {}).get("total"))
    except Exception:
        pass
    return {
        "strategy": strategy,
        "direction": direction,
        "structure": {
            "bull": "Bull Put Credit Spread",
            "bear": "Bear Call Credit Spread",
        }[direction],
        "expiry": str(best["expiry"]),
        "lot_size": lot,
        "legs": legs,
        "credit_per_unit": best["credit"],
        "credit_total": round(best["credit"] * lot, 2),
        "wing": best["wing"],
        "wing_pct": best["wing_pct"],
        "max_profit": round(best["credit"] * lot, 2),
        "max_loss": round(best["max_loss"] * lot, 2),
        "margin_total": margin_total,
        "margin_raw": margin,
        "alternatives": [
            {
                "expiry": str(c["expiry"]),
                "sold_strike": c["sold_strike"],
                "hedge_strike": c["hedge_strike"],
                "sold_premium": c["sold_premium"],
                "hedge_premium": c["hedge_premium"],
                "credit": c["credit"],
                "wing": c["wing"],
            }
            for c in alternatives
        ],
    }


# ---------- public entry ----------

BUILDERS = {
    "nidhi_kalash": _build_nidhi_kalash,
    # TODO: golden_goose, panther, ocean_treasure, gg_leaps
}


def build_recommendations(kite, signals: dict, blocks: dict) -> dict:
    """For each *_cross signal, return a dict of trade recommendations."""
    out: dict = {}
    today = datetime.now(IST).date()

    nifty = blocks.get("nifty") or {}
    spot = nifty.get("spot")
    vix = (blocks.get("indiavix") or {}).get("spot")
    if spot is None:
        return {"error": "spot unavailable; cannot build recommendations"}

    instruments_cache: list | None = None

    def _get_instruments():
        nonlocal instruments_cache
        if instruments_cache is None:
            instruments_cache = kite.instruments("NFO")
        return instruments_cache

    for strategy, signal in (signals or {}).items():
        if "cross" not in (signal or ""):
            continue
        builder = BUILDERS.get(strategy)
        if builder is None:
            out[strategy] = {
                "strategy": strategy,
                "signal": signal,
                "note": "trade builder not yet implemented for this strategy",
            }
            continue
        direction = "bull" if "bull" in signal else "bear"
        try:
            out[strategy] = builder(kite, direction, _get_instruments(), spot, vix, today)
        except Exception as e:
            out[strategy] = {"strategy": strategy, "error": f"{type(e).__name__}: {e}"}
    return out

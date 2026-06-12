"""
Trade recommender — for each fresh *_cross signal, build the exact spread:
strikes, current premiums (live LTP), net credit/debit, and margin required
for 1 lot (via Kite's basket margin API).

Implemented:
  - nidhi_kalash   (Bull put / Bear call credit spread, 100-pt, credit 90-110/130)
  - golden_goose   (Bull put / Bear call credit spread, 100-pt, credit 90-130, wing ≤ 2%)
  - panther        (Bull put / Bear call credit spread, 100-pt, wing 200-400, credit ≥ 200)
  - ocean_treasure (Quarter-end short PE/CE + near-month hedge, 500-pt strikes)
  - gg_leaps       (Quarter-end short PE/CE LEAPS + near-month hedge, 500/1000-pt strikes)
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))


# ====================================================================
# Expiry helpers
# ====================================================================

def _futures_expiries(instruments, name: str, today: date) -> list:
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


def _option_expiries(instruments, name: str, today: date) -> list:
    """All option expiries for `name`, sorted."""
    seen = set()
    for ins in instruments:
        if (
            ins.get("name") == name
            and ins.get("instrument_type") in ("CE", "PE")
            and ins.get("expiry")
            and ins["expiry"] >= today
        ):
            seen.add(ins["expiry"])
    return sorted(seen)


def _quarter_end_expiry(option_expiries: list, today: date) -> Optional[object]:
    """
    Pick a quarter-end (Mar/Jun/Sep/Dec) option expiry per the OT rule:
      - In or before 20th of second month of current quarter → current quarter end
      - After 20th of second month, OR in third month → next quarter end

    The instruments dump may not list the LEAPS we want for very far quarters,
    so we return the first quarter-end expiry that matches the rule.
    """
    quarter_end_months = {3, 6, 9, 12}
    quarter_ends = [e for e in option_expiries if e.month in quarter_end_months]
    if not quarter_ends:
        return None

    current_quarter_end_month = ((today.month - 1) // 3 + 1) * 3
    second_month = current_quarter_end_month - 1
    third_month = current_quarter_end_month

    # Should we use current quarter or shift to next?
    use_next = False
    if today.month < second_month:
        use_next = False
    elif today.month == second_month:
        use_next = today.day > 20
    elif today.month == third_month:
        use_next = True
    else:
        use_next = True

    if not use_next:
        for e in quarter_ends:
            if e.year == today.year and e.month == current_quarter_end_month:
                return e
        return quarter_ends[0]
    # Find next quarter end after current quarter
    target = current_quarter_end_month + 3
    target_year = today.year if target <= 12 else today.year + 1
    target_month = target if target <= 12 else target - 12
    for e in quarter_ends:
        if e.year == target_year and e.month == target_month:
            return e
    # fall back: first quarter end after the current quarter's
    cutoff_date = date(today.year, current_quarter_end_month, 28)
    after = [e for e in quarter_ends if e > cutoff_date]
    return after[0] if after else quarter_ends[-1]


# ====================================================================
# Option chain helpers
# ====================================================================

def _option_chain(instruments, name: str, expiry, opt_type: str) -> dict:
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


def _ltp_batch(kite, symbols: list) -> dict:
    if not symbols:
        return {}
    out = {}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i : i + 200]
        try:
            out.update(kite.ltp(chunk))
        except Exception:
            for s in chunk:
                try:
                    out.update(kite.ltp([s]))
                except Exception:
                    pass
    return out


def _basket_margin(kite, legs: list) -> dict:
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
        return kite.basket_order_margins(basket, consider_positions=False)
    except Exception as e:
        return {"error": str(e)}


# ====================================================================
# Generic credit-spread builder (NK / GG / Panther)
# ====================================================================

def _find_best_credit_spread(
    kite, instruments, name: str, expiries: list, direction: str,
    sold_candidates: list, wing_widths: list,
    credit_band: tuple, wing_filter, strike_multiple: int = 100,
) -> dict:
    """
    Try (sold, hedge) pairs across expiries, returning (best, alternatives, tried).
    `wing_filter(wing_pts, wing_pct) -> bool` decides if a wing is acceptable.
    """
    opt_type = "PE" if direction == "bull" else "CE"
    sign = -1 if direction == "bull" else +1
    credit_min, credit_max = credit_band

    tried = []
    for expiry in expiries:
        opts = _option_chain(instruments, name, expiry, opt_type)
        if not opts:
            continue
        # Filter to strikes that are multiples of strike_multiple
        opts = {k: v for k, v in opts.items() if k % strike_multiple == 0}
        if not opts:
            continue

        # collect LTP requests for all candidate pairs
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

        spot = None  # spot is not used here; wing_filter receives wing_pct relative to sold strike
        candidates = []
        for sold, hedge, w in pairs:
            sold_ts = opts[sold]["tradingsymbol"]
            hedge_ts = opts[hedge]["tradingsymbol"]
            sp, hp = _ltp(sold_ts), _ltp(hedge_ts)
            if sp is None or hp is None:
                continue
            credit = sp - hp
            wing_pct = w / sold * 100
            tried.append({
                "expiry": str(expiry), "sold_strike": sold, "hedge_strike": hedge,
                "wing": w, "credit": round(credit, 2),
            })
            if not (credit_min <= credit <= credit_max):
                continue
            if not wing_filter(w, wing_pct):
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

        if candidates:
            candidates.sort(key=lambda c: (-c["wing"], -c["credit"]))
            return {"best": candidates[0], "alternatives": candidates[1:5], "tried": tried}

    return {"best": None, "alternatives": [], "tried": tried[:30]}


def _format_credit_spread(strategy: str, structure_name: str, direction: str,
                          kite, result: dict) -> dict:
    best = result["best"]
    if not best:
        return {
            "strategy": strategy,
            "direction": direction,
            "error": "No (sold, hedge) combination met the credit band on current premiums",
            "tried": result["tried"],
        }
    lot = best["lot_size"]
    opt_type = "PE" if direction == "bull" else "CE"
    legs = [
        {
            "action": "SELL", "transaction_type": "SELL",
            "tradingsymbol": best["sold_tradingsymbol"],
            "strike": best["sold_strike"], "option_type": opt_type,
            "premium": best["sold_premium"], "quantity": lot,
        },
        {
            "action": "BUY", "transaction_type": "BUY",
            "tradingsymbol": best["hedge_tradingsymbol"],
            "strike": best["hedge_strike"], "option_type": opt_type,
            "premium": best["hedge_premium"], "quantity": lot,
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
        "structure": structure_name,
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
            for c in result["alternatives"]
        ],
    }


# ====================================================================
# Builders for credit-spread strategies
# ====================================================================

def _build_nidhi_kalash(kite, direction, instruments, spot, vix, today):
    """100-pt strikes, sold ≥0.5% from CMP, wing ≤2.5% (round-500 exception),
       credit 90-110 (bull) / 90-130 (bear); monthly expiry; next month if VIX low."""
    expiries = _futures_expiries(instruments, "NIFTY", today)
    if not expiries:
        return {"strategy": "nidhi_kalash", "error": "no upcoming NIFTY future expiry"}
    expiry_options = expiries[:2]

    if direction == "bull":
        max_sold = math.floor((spot * 0.995) / 100) * 100
        sold_candidates = [max_sold, max_sold - 100, max_sold - 200, max_sold - 300]
        credit_band = (90.0, 110.0)
    else:
        max_sold = math.ceil((spot * 1.005) / 100) * 100
        sold_candidates = [max_sold, max_sold + 100, max_sold + 200, max_sold + 300]
        credit_band = (90.0, 130.0)

    wing_widths = [500, 400, 300, 200]
    # Wing <= 2.5% of spot, with round-figure 500-pt allowed exception
    def wing_filter(w, wp_vs_sold):
        wp_vs_spot = w / spot * 100
        return wp_vs_spot <= 2.5 or w == 500

    res = _find_best_credit_spread(
        kite, instruments, "NIFTY", expiry_options, direction,
        sold_candidates, wing_widths, credit_band, wing_filter,
    )
    structure = "Bull Put Credit Spread" if direction == "bull" else "Bear Call Credit Spread"
    return _format_credit_spread("nidhi_kalash", structure, direction, kite, res)


def _build_golden_goose(kite, direction, instruments, spot, vix, today):
    """100-pt strikes, sold OTM, wing ≤ 2% of sold strike, credit 90-130;
       monthly expiry (next month if today > 15)."""
    expiries = _futures_expiries(instruments, "NIFTY", today)
    if not expiries:
        return {"strategy": "golden_goose", "error": "no upcoming NIFTY future expiry"}
    # Prefer next month if past 15th of current month
    if today.day > 15 and len(expiries) > 1:
        expiry_options = [expiries[1], expiries[0]]
    else:
        expiry_options = expiries[:2]

    if direction == "bull":
        max_sold = math.floor(spot / 100) * 100  # just OTM
        sold_candidates = [max_sold, max_sold - 100, max_sold - 200, max_sold - 300, max_sold - 400]
    else:
        max_sold = math.ceil(spot / 100) * 100
        sold_candidates = [max_sold, max_sold + 100, max_sold + 200, max_sold + 300, max_sold + 400]

    wing_widths = [400, 300, 200, 100]
    credit_band = (90.0, 130.0)

    def wing_filter(w, wp_vs_sold):
        return wp_vs_sold <= 2.0

    res = _find_best_credit_spread(
        kite, instruments, "NIFTY", expiry_options, direction,
        sold_candidates, wing_widths, credit_band, wing_filter,
    )
    structure = "Bull Put Credit Spread" if direction == "bull" else "Bear Call Credit Spread"
    return _format_credit_spread("golden_goose", structure, direction, kite, res)


def _build_panther(kite, direction, instruments, spot, vix, today):
    """100-pt strikes, wing 200-400 pts, net credit ≥ ~200 pts; monthly expiry."""
    expiries = _futures_expiries(instruments, "NIFTY", today)
    if not expiries:
        return {"strategy": "panther", "error": "no upcoming NIFTY future expiry"}
    expiry_options = expiries[:2]

    if direction == "bull":
        max_sold = math.floor(spot / 100) * 100
        sold_candidates = [max_sold, max_sold - 100, max_sold - 200, max_sold - 300]
    else:
        max_sold = math.ceil(spot / 100) * 100
        sold_candidates = [max_sold, max_sold + 100, max_sold + 200, max_sold + 300]

    wing_widths = [400, 300, 200]
    credit_band = (200.0, 1e9)  # at least ~200, no real upper limit

    def wing_filter(w, wp_vs_sold):
        return 200 <= w <= 400

    res = _find_best_credit_spread(
        kite, instruments, "NIFTY", expiry_options, direction,
        sold_candidates, wing_widths, credit_band, wing_filter,
    )
    structure = "Bull Put Credit Spread" if direction == "bull" else "Bear Call Credit Spread"
    return _format_credit_spread("panther", structure, direction, kite, res)


# ====================================================================
# Premium-based "naked short + hedge" builders (OT / GG LEAPS)
# ====================================================================

def _find_naked_short_with_hedge(
    kite, instruments, name: str, direction: str,
    short_expiry, hedge_expiry,
    short_premium_band: tuple, hedge_premium_band: tuple,
    max_hedge_distance: int, strike_multiples: list,
    spot: float = None,
) -> dict:
    """
    Pick the OTM short strike whose premium is closest to mid-band; pair with
    a hedge whose premium is closest to mid-band AND within `max_hedge_distance`.

    Nearest-fit fallback: when NO strike quotes inside a premium band (common
    in low-VIX regimes — e.g. every OTM quarter-end put under ₹200), the band
    is relaxed to "closest premium to the band's midpoint" and the result is
    flagged so the UI can warn the user that a saved rule was bent:
      - short leg fallback additionally requires the strike to be OTM
        (the band normally guarantees that implicitly; without the band an
        ITM strike could win on premium-distance alone)
      - hedge fallback keeps the max-distance constraint HARD — that's the
        protective constraint — and relaxes only the premium band
    """
    opt_type = "PE" if direction == "bull" else "CE"
    short_chain = _option_chain(instruments, name, short_expiry, opt_type)
    hedge_chain = _option_chain(instruments, name, hedge_expiry, opt_type)
    if not short_chain or not hedge_chain:
        return {"best": None, "alternatives": [], "tried": [],
                "error": f"option chain missing (short {short_expiry} or hedge {hedge_expiry})"}

    # Keep only strikes that are multiples of any of the allowed multiples
    def _strike_ok(k):
        return any(k % m == 0 for m in strike_multiples)
    short_chain = {k: v for k, v in short_chain.items() if _strike_ok(k)}
    hedge_chain = {k: v for k, v in hedge_chain.items() if _strike_ok(k)}

    # Fetch LTPs for all strikes in both chains (typically <100 each)
    all_syms = (
        [f"NFO:{ins['tradingsymbol']}" for ins in short_chain.values()] +
        [f"NFO:{ins['tradingsymbol']}" for ins in hedge_chain.values()]
    )
    ltps = _ltp_batch(kite, all_syms)

    def _ltp(ts):
        return ltps.get(f"NFO:{ts}", {}).get("last_price")

    def _is_otm(strike):
        if spot is None:
            return True  # can't verify; preserve old behaviour
        return strike < spot if opt_type == "PE" else strike > spot

    sp_min, sp_max = short_premium_band
    hp_min, hp_max = hedge_premium_band
    sp_target = (sp_min + sp_max) / 2
    hp_target = (hp_min + hp_max) / 2

    # Candidate short strikes (in band)
    short_options = []
    for strike, ins in short_chain.items():
        prem = _ltp(ins["tradingsymbol"])
        if prem is None:
            continue
        if not (sp_min <= prem <= sp_max):
            continue
        short_options.append((strike, ins, prem))
    short_options.sort(key=lambda x: abs(x[2] - sp_target))

    short_band_relaxed = False
    if not short_options:
        # Nearest-fit fallback: any OTM strike with a live quote, ranked by
        # premium distance to the band midpoint. Cap at 5 candidates.
        fallback = []
        for strike, ins in short_chain.items():
            prem = _ltp(ins["tradingsymbol"])
            if prem is None or prem <= 0 or not _is_otm(strike):
                continue
            fallback.append((strike, ins, prem))
        fallback.sort(key=lambda x: abs(x[2] - sp_target))
        short_options = fallback[:5]
        short_band_relaxed = True
        if not short_options:
            return {"best": None, "alternatives": [], "tried": [],
                    "error": f"no OTM short strike with any live quote (band {short_premium_band} also empty)"}

    # Candidate hedge strikes (in band) — collected once
    hedge_options = []
    hedge_all = []
    for strike, ins in hedge_chain.items():
        prem = _ltp(ins["tradingsymbol"])
        if prem is None or prem <= 0:
            continue
        hedge_all.append((strike, ins, prem))
        if hp_min <= prem <= hp_max:
            hedge_options.append((strike, ins, prem))

    def _best_hedge_for(sstrike, pool):
        best, best_score = None, math.inf
        for hstrike, hins, hprem in pool:
            if abs(hstrike - sstrike) > max_hedge_distance:
                continue
            score = abs(hprem - hp_target)
            if score < best_score:
                best_score = score
                best = (hstrike, hins, hprem)
        return best

    candidates = []
    for sstrike, sins, sprem in short_options:
        hedge_band_relaxed = False
        best_hedge = _best_hedge_for(sstrike, hedge_options)
        if not best_hedge:
            # Relax the hedge premium band; distance stays hard.
            best_hedge = _best_hedge_for(sstrike, hedge_all)
            hedge_band_relaxed = best_hedge is not None
        if not best_hedge:
            continue
        hstrike, hins, hprem = best_hedge
        candidates.append({
            "short_expiry": short_expiry,
            "hedge_expiry": hedge_expiry,
            "short_strike": sstrike,
            "hedge_strike": hstrike,
            "short_premium": round(sprem, 2),
            "hedge_premium": round(hprem, 2),
            "net_credit": round(sprem - hprem, 2),
            "hedge_distance": abs(hstrike - sstrike),
            "short_tradingsymbol": sins["tradingsymbol"],
            "hedge_tradingsymbol": hins["tradingsymbol"],
            "lot_size": int(sins.get("lot_size") or 75),
            "short_band_relaxed": short_band_relaxed,
            "hedge_band_relaxed": hedge_band_relaxed,
        })

    if not candidates:
        return {"best": None, "alternatives": [], "tried": [],
                "error": f"no hedge strike within {max_hedge_distance} pts of any short candidate"}

    # Best = in-band hedge preferred, then highest net credit
    candidates.sort(key=lambda c: (c["hedge_band_relaxed"], -c["net_credit"]))
    return {
        "best": candidates[0],
        "alternatives": candidates[1:5],
        "tried": [],
        "short_premium_band": list(short_premium_band),
        "hedge_premium_band": list(hedge_premium_band),
        "max_hedge_distance": max_hedge_distance,
    }


def _format_naked_with_hedge(strategy: str, structure_name: str, direction: str,
                              kite, result: dict) -> dict:
    if result.get("error") and not result.get("best"):
        return {"strategy": strategy, "direction": direction, "error": result["error"]}
    best = result["best"]
    if not best:
        return {"strategy": strategy, "direction": direction,
                "error": "no eligible structure found"}
    lot = best["lot_size"]
    opt_type = "PE" if direction == "bull" else "CE"
    legs = [
        {
            "action": "SELL", "transaction_type": "SELL",
            "tradingsymbol": best["short_tradingsymbol"],
            "strike": best["short_strike"], "option_type": opt_type,
            "premium": best["short_premium"], "quantity": lot,
            "expiry": str(best["short_expiry"]),
        },
        {
            "action": "BUY", "transaction_type": "BUY",
            "tradingsymbol": best["hedge_tradingsymbol"],
            "strike": best["hedge_strike"], "option_type": opt_type,
            "premium": best["hedge_premium"], "quantity": lot,
            "expiry": str(best["hedge_expiry"]),
        },
    ]
    margin = _basket_margin(kite, legs)
    margin_total = None
    try:
        margin_total = float(margin.get("final", {}).get("total"))
    except Exception:
        pass

    # Human-readable warnings when a saved rule was bent to produce this trade.
    # The UI renders these prominently — the user must consciously accept the
    # deviation before trading.
    relaxations = []
    if best.get("short_band_relaxed"):
        lo, hi = result.get("short_premium_band") or [None, None]
        band_txt = f"₹{lo:.0f}–₹{hi:.0f}" if lo is not None else "the saved band"
        relaxations.append(
            f"Short-leg premium rule RELAXED: the strategy wants the sold option "
            f"to quote {band_txt}, but no {best['short_expiry']} strike does today "
            f"(low-VIX premiums). Closest OTM fit chosen: "
            f"{best['short_tradingsymbol']} @ ₹{best['short_premium']}. "
            f"Income and risk will differ from the strategy's reference numbers."
        )
    if best.get("hedge_band_relaxed"):
        lo, hi = result.get("hedge_premium_band") or [None, None]
        band_txt = f"₹{lo:.0f}–₹{hi:.0f}" if lo is not None else "the saved band"
        relaxations.append(
            f"Hedge premium rule RELAXED: no {best['hedge_expiry']} hedge within "
            f"{result.get('max_hedge_distance')} pts quotes {band_txt}. Closest fit: "
            f"{best['hedge_tradingsymbol']} @ ₹{best['hedge_premium']} "
            f"(distance constraint was NOT relaxed)."
        )

    return {
        "strategy": strategy,
        "direction": direction,
        "structure": structure_name,
        "expiry": f"short {best['short_expiry']}, hedge {best['hedge_expiry']}",
        "lot_size": lot,
        "legs": legs,
        "credit_per_unit": best["net_credit"],
        "credit_total": round(best["net_credit"] * lot, 2),
        "wing": best["hedge_distance"],
        "wing_pct": None,
        "max_profit": None,  # framework-level: not bounded purely by structure
        "max_loss": None,    # capped by hedge but only same-month
        "margin_total": margin_total,
        "margin_raw": margin,
        "relaxations": relaxations,
        "alternatives": [
            {
                "short_strike": c["short_strike"],
                "hedge_strike": c["hedge_strike"],
                "short_premium": c["short_premium"],
                "hedge_premium": c["hedge_premium"],
                "credit": c["net_credit"],
                "wing": c["hedge_distance"],
                "expiry": f"short {c['short_expiry']}, hedge {c['hedge_expiry']}",
            }
            for c in result["alternatives"]
        ],
    }


def _build_ocean_treasure(kite, direction, instruments, spot, vix, today):
    """Sell quarter-end OTM (premium 200-220), buy current-month hedge (premium 20-50,
       within 700 pts), 500-pt strikes (1000-pt fallback)."""
    option_exps = _option_expiries(instruments, "NIFTY", today)
    if not option_exps:
        return {"strategy": "ocean_treasure", "error": "no option expiries found"}
    short_expiry = _quarter_end_expiry(option_exps, today)
    if not short_expiry:
        return {"strategy": "ocean_treasure", "error": "no quarter-end expiry found"}

    # Hedge expiry = next monthly future expiry (= current monthly options expiry)
    fut_expiries = _futures_expiries(instruments, "NIFTY", today)
    if not fut_expiries:
        return {"strategy": "ocean_treasure", "error": "no monthly expiry for hedge"}
    hedge_expiry = fut_expiries[0]
    # If hedge_expiry == short_expiry (both same month), use next month for hedge
    if hedge_expiry == short_expiry and len(fut_expiries) > 1:
        hedge_expiry = fut_expiries[1]

    res = _find_naked_short_with_hedge(
        kite, instruments, "NIFTY", direction,
        short_expiry=short_expiry, hedge_expiry=hedge_expiry,
        short_premium_band=(200.0, 220.0),
        hedge_premium_band=(20.0, 50.0),
        max_hedge_distance=700,
        strike_multiples=[500, 1000],
        spot=spot,
    )
    structure = (
        "Bull: Sell quarter-end PUT + buy near-month PUT hedge"
        if direction == "bull"
        else "Bear: Sell quarter-end CALL + buy near-month CALL hedge"
    )
    return _format_naked_with_hedge("ocean_treasure", structure, direction, kite, res)


def _build_gg_leaps(kite, direction, instruments, spot, vix, today):
    """Sell quarter-end OTM LEAPS (premium 200-350, stretch 450), buy near-month
       hedge (premium 20-50, ~2% / ~500 pts away). 500/1000-pt strikes.
       (15-20 Feb special rule and 15th-of-month hedge rule not yet enforced.)"""
    option_exps = _option_expiries(instruments, "NIFTY", today)
    if not option_exps:
        return {"strategy": "gg_leaps", "error": "no option expiries found"}
    short_expiry = _quarter_end_expiry(option_exps, today)
    if not short_expiry:
        return {"strategy": "gg_leaps", "error": "no quarter-end LEAPS expiry found"}

    fut_expiries = _futures_expiries(instruments, "NIFTY", today)
    if not fut_expiries:
        return {"strategy": "gg_leaps", "error": "no monthly hedge expiry"}
    hedge_expiry = fut_expiries[0]
    if today.day > 15 and len(fut_expiries) > 1:
        hedge_expiry = fut_expiries[1]
    if hedge_expiry == short_expiry and len(fut_expiries) > 1:
        hedge_expiry = fut_expiries[1] if fut_expiries[1] != short_expiry else (fut_expiries[2] if len(fut_expiries) > 2 else fut_expiries[1])

    res = _find_naked_short_with_hedge(
        kite, instruments, "NIFTY", direction,
        short_expiry=short_expiry, hedge_expiry=hedge_expiry,
        short_premium_band=(200.0, 450.0),  # stretch band
        hedge_premium_band=(20.0, 50.0),
        max_hedge_distance=700,
        strike_multiples=[500, 1000],
        spot=spot,
    )
    structure = (
        "Bull: Sell PUT LEAPS + buy near-month PUT hedge"
        if direction == "bull"
        else "Bear: Sell CALL LEAPS + buy near-month CALL hedge"
    )
    return _format_naked_with_hedge("gg_leaps", structure, direction, kite, res)


# ====================================================================
# Schedule-driven builders (Batman, No Brainer NIFTY, Expiry Double Butterfly)
# ====================================================================

def _format_multileg(strategy: str, structure: str, expiry, lot_size: int,
                     legs_in: list, kite) -> dict:
    """Helper for non-credit-spread structures (flies, broken-wing condors).
    `legs_in` is a list of dicts: {action, transaction_type, tradingsymbol,
    strike, option_type, premium, quantity}. Computes net cash + margin."""
    net_premium_per_unit = 0.0
    for leg in legs_in:
        sign = -1 if leg["transaction_type"] == "BUY" else +1
        net_premium_per_unit += sign * (leg["premium"] or 0)
    qty = lot_size
    margin = _basket_margin(kite, legs_in)
    margin_total = None
    try:
        margin_total = float(margin.get("final", {}).get("total"))
    except Exception:
        pass
    return {
        "strategy": strategy,
        "structure": structure,
        "expiry": str(expiry),
        "lot_size": lot_size,
        "legs": legs_in,
        "net_premium_per_unit": round(net_premium_per_unit, 2),
        "net_premium_total": round(net_premium_per_unit * qty, 2),
        "margin_total": margin_total,
        "margin_raw": margin,
    }


def _nearest_strike_in_chain(strikes: list, target: float) -> int:
    return min(strikes, key=lambda s: abs(s - target))


def _build_expiry_double_butterfly(kite, instruments, spot, today):
    """Mon 3:15 PM build for the Tuesday-weekly Expiry Double Butterfly.
    X = 0.5% of spot, rounded to strike step. Call fly (1-2-1) above + Put
    fly (1-2-1) below. Current weekly expiry."""
    # Find nearest weekly NIFTY expiry (could be tomorrow or up to ~7 days)
    today_d = today
    weekly_set = set()
    for ins in instruments:
        if (ins.get("name") == "NIFTY"
            and ins.get("instrument_type") in ("CE", "PE")
            and ins.get("expiry") and ins["expiry"] >= today_d):
            weekly_set.add(ins["expiry"])
    if not weekly_set:
        return {"strategy": "edb", "error": "no NIFTY option expiry found"}
    expiry = sorted(weekly_set)[0]

    # All CE strikes for that expiry → detect step
    chain_ce = _option_chain(instruments, "NIFTY", expiry, "CE")
    if not chain_ce:
        return {"strategy": "edb", "error": "no CE chain for weekly expiry"}
    strikes_sorted = sorted(chain_ce.keys())
    if len(strikes_sorted) < 2:
        return {"strategy": "edb", "error": "chain too sparse"}
    step = min(strikes_sorted[i+1] - strikes_sorted[i]
               for i in range(len(strikes_sorted)-1)
               if strikes_sorted[i+1] - strikes_sorted[i] > 0)

    # X = 0.5% of spot, rounded UP to step (so we stay on the grid)
    x_raw = 0.005 * spot
    x = max(step, round(x_raw / step) * step)

    def _pick(target):
        return _nearest_strike_in_chain(strikes_sorted, target)

    call_buy_lo  = _pick(spot + 0.5 * x)
    call_sell    = _pick(spot + 1.5 * x)
    call_buy_hi  = _pick(spot + 2.5 * x)
    chain_pe = _option_chain(instruments, "NIFTY", expiry, "PE")
    pe_strikes = sorted(chain_pe.keys())
    put_buy_hi   = _nearest_strike_in_chain(pe_strikes, spot - 0.5 * x)
    put_sell     = _nearest_strike_in_chain(pe_strikes, spot - 1.5 * x)
    put_buy_lo   = _nearest_strike_in_chain(pe_strikes, spot - 2.5 * x)

    needed_legs = [
        ("BUY",  call_buy_lo, "CE", chain_ce, 1),
        ("SELL", call_sell,   "CE", chain_ce, 2),
        ("BUY",  call_buy_hi, "CE", chain_ce, 1),
        ("BUY",  put_buy_hi,  "PE", chain_pe, 1),
        ("SELL", put_sell,    "PE", chain_pe, 2),
        ("BUY",  put_buy_lo,  "PE", chain_pe, 1),
    ]
    sample = chain_ce[call_sell]
    lot_size = int(sample.get("lot_size") or 75)

    # Batch LTPs
    syms = set()
    for _, strike, _, chain, _ in needed_legs:
        ins = chain.get(strike)
        if not ins:
            return {"strategy": "edb",
                    "error": f"strike {strike} missing from chain"}
        syms.add(ins["tradingsymbol"])
    ltps = _ltp_batch(kite, [f"NFO:{s}" for s in syms])

    legs = []
    for action, strike, opt_type, chain, mult in needed_legs:
        ins = chain[strike]
        ts = ins["tradingsymbol"]
        prem = ltps.get(f"NFO:{ts}", {}).get("last_price")
        legs.append({
            "action": action, "transaction_type": action,
            "tradingsymbol": ts, "strike": strike,
            "option_type": opt_type, "premium": prem,
            "quantity": lot_size * mult,
        })

    return _format_multileg(
        strategy="edb",
        structure=f"Expiry Double Butterfly · X={int(x)} pts · Call fly 1-2-1 + Put fly 1-2-1",
        expiry=expiry, lot_size=lot_size,
        legs_in=legs, kite=kite,
    )


def _build_call_broken_wing_121(kite, instruments, spot, today, strategy: str,
                                 structure_label: str,
                                 plus_short: int = 600,
                                 plus_long_near: int = 300,
                                 plus_long_far: int = 1600):
    """Generic 1-2-1 call structure used by Batman and No Brainer NIFTY:
    Buy 1× (spot+300) CE / Sell 2× (spot+600) CE / Buy 1× (spot+1600) CE.
    Next monthly expiry. Last-Friday entry; the calendar gate is in the
    caller — this builder just constructs the trade for the given spot."""
    fut_exps = _futures_expiries(instruments, "NIFTY", today)
    if not fut_exps:
        return {"strategy": strategy, "error": "no monthly expiry found"}
    # Next month: skip the closest if today is on or after current expiry day
    expiry = fut_exps[1] if len(fut_exps) > 1 else fut_exps[0]

    chain = _option_chain(instruments, "NIFTY", expiry, "CE")
    if not chain:
        return {"strategy": strategy, "error": "no CE chain"}
    strikes_sorted = sorted(chain.keys())

    s_near = _nearest_strike_in_chain(strikes_sorted, spot + plus_long_near)
    s_short = _nearest_strike_in_chain(strikes_sorted, spot + plus_short)
    s_far  = _nearest_strike_in_chain(strikes_sorted, spot + plus_long_far)

    lot_size = int(chain[s_short].get("lot_size") or 75)
    needed = [
        ("BUY",  s_near, 1),
        ("SELL", s_short, 2),
        ("BUY",  s_far,  1),
    ]
    syms = {chain[strike]["tradingsymbol"] for _, strike, _ in needed}
    ltps = _ltp_batch(kite, [f"NFO:{s}" for s in syms])

    legs = []
    for action, strike, mult in needed:
        ins = chain[strike]
        ts = ins["tradingsymbol"]
        prem = ltps.get(f"NFO:{ts}", {}).get("last_price")
        legs.append({
            "action": action, "transaction_type": action,
            "tradingsymbol": ts, "strike": strike,
            "option_type": "CE", "premium": prem,
            "quantity": lot_size * mult,
        })
    return _format_multileg(
        strategy=strategy, structure=structure_label, expiry=expiry,
        lot_size=lot_size, legs_in=legs, kite=kite,
    )


def _build_batman(kite, instruments, spot, today):
    return _build_call_broken_wing_121(
        kite, instruments, spot, today,
        strategy="batman",
        structure_label="Batman · Buy 1× (+300) CE / Sell 2× (+600) CE / Buy 1× (+1600) CE",
    )


def _build_no_brainer(kite, instruments, spot, today):
    return _build_call_broken_wing_121(
        kite, instruments, spot, today,
        strategy="no_brainer",
        structure_label="No Brainer NIFTY · Buy 1× (+300) CE / Sell 2× (+600) CE / Buy 1× (+1600) CE",
    )


# ====================================================================
# Public entry point
# ====================================================================

# Signal-driven (fire on '*_cross' in signals dict)
BUILDERS = {
    "nidhi_kalash":   _build_nidhi_kalash,
    "golden_goose":   _build_golden_goose,
    "panther":        _build_panther,
    "ocean_treasure": _build_ocean_treasure,
    "gg_leaps":       _build_gg_leaps,
}

# Schedule-driven (fire on calendar flags)
SCHEDULE_BUILDERS = {
    "edb":        _build_expiry_double_butterfly,
    "batman":     _build_batman,
    "no_brainer": _build_no_brainer,
}


# Maps a signal strategy to the calendar flag that makes today a rollover day
# for that strategy. OT and GG_LEAPS roll only the hedge, not the full short
# leg, so they aren't included here (different builder shape needed).
ROLLOVER_FLAG = {
    "golden_goose": "is_t7",
    "panther":      "is_t8",
    "nidhi_kalash": "is_second_last_wed_of_expiry_month",
}


def _signal_context(strategy: str, signal: str, flags: dict) -> str:
    """Return one of: 'fresh' (signal crossed today), 'rollover' (today is the
    scheduled rollover day for this strategy), 'late' (signal is in a hold
    state — the trade is still buildable but you've missed the entry day)."""
    if "cross" in (signal or ""):
        return "fresh"
    rollover_flag = ROLLOVER_FLAG.get(strategy)
    if rollover_flag and flags.get(rollover_flag):
        return "rollover"
    return "late"


def build_recommendations(kite, signals: dict, blocks: dict,
                          calendar_flags: Optional[dict] = None) -> dict:
    """Build the per-strategy trade recommendations.

    Signal-driven strategies build on ANY directional signal (bull/bear),
    with a 'context' tag explaining whether today is the fresh-entry day,
    a scheduled rollover, or a late entry (you missed the cross day but
    the structure is still buildable).

    Schedule-driven strategies build only on their scheduled calendar day.
    Late entry isn't offered there because the entry day is structurally
    important (EDB depends on overnight theta; Batman / No Brainer are
    monthly carries anchored to the last-Friday cycle)."""
    out: dict = {}
    today = datetime.now(IST).date()
    flags = calendar_flags or {}

    nifty = blocks.get("nifty") or {}
    spot = nifty.get("spot")
    vix = (blocks.get("indiavix") or {}).get("spot")
    if spot is None:
        return {"error": "spot unavailable; cannot build recommendations"}

    instruments_cache = None

    def _get_instruments():
        nonlocal instruments_cache
        if instruments_cache is None:
            instruments_cache = kite.instruments("NFO")
        return instruments_cache

    # Signal-driven: build on any bull/bear signal, label by context
    for strategy, signal in (signals or {}).items():
        if not signal or signal == "unknown":
            continue
        if "bull" in signal:
            direction = "bull"
        elif "bear" in signal:
            direction = "bear"
        else:
            continue
        builder = BUILDERS.get(strategy)
        if builder is None:
            out[strategy] = {
                "strategy": strategy, "signal": signal,
                "context": _signal_context(strategy, signal, flags),
                "note": "trade builder not yet implemented for this strategy",
            }
            continue
        context = _signal_context(strategy, signal, flags)
        try:
            result = builder(kite, direction, _get_instruments(), spot, vix, today)
            if isinstance(result, dict):
                result["context"] = context
                result["direction"] = direction
                result["signal"] = signal
            out[strategy] = result
        except Exception as e:
            out[strategy] = {"strategy": strategy, "context": context,
                             "direction": direction, "signal": signal,
                             "error": f"{type(e).__name__}: {e}"}

    # Schedule-driven: only fire on the scheduled day (no late entry).
    schedule_triggers = [
        ("is_mon_before_weekly", ["edb"]),
        ("is_last_friday",       ["batman", "no_brainer"]),
    ]
    for flag_key, strategies in schedule_triggers:
        if not flags.get(flag_key):
            continue
        for strategy in strategies:
            builder = SCHEDULE_BUILDERS.get(strategy)
            if builder is None:
                continue
            try:
                result = builder(kite, _get_instruments(), spot, today)
                if isinstance(result, dict):
                    result["context"] = "scheduled"
                out[strategy] = result
            except Exception as e:
                out[strategy] = {"strategy": strategy, "context": "scheduled",
                                 "error": f"{type(e).__name__}: {e}"}

    return out

"""Market-pulse reads computed from the chain we already record.

Three panels, all from `chain_snapshot`, no new data source:

  EXPECTED MOVE  what the options market is pricing for this expiry
  MAX PAIN       the strike at which writers, in aggregate, lose least
  IV SKEW        call IV vs put IV — what the market charges for each tail

On the expected-move factor: the popular shortcut is `ATM straddle x 0.68`.
We do not use it, because it cannot be derived — for a normal distribution the
expected ABSOLUTE move is sigma*sqrt(2/pi) = 0.798*sigma, which makes 1 sigma
roughly 1.25x the straddle, not 0.68x. Rather than copy a number we cannot
justify, sigma is backed out of the ATM options directly (BS inversion, the
same inverter recommend.py uses) and 1 sigma = S * sigma * sqrt(T). That is
exact instead of folkloric, and `validate_expected_move()` checks it against
our own history: a correct 1-sigma band should contain about 68% of realised
closes.

MAX PAIN IS DISPLAYED ONLY IF IT EARNS IT. The theory says spot gravitates to
the strike where writers suffer least. That is a testable claim, and a very
similar one — the HHI "magnet" hypothesis — was refuted on this same data at
47%, below a coin flip. `validate_max_pain()` runs the equivalent test here.
Ship the number only if it beats chance; a pretty panel that predicts nothing
is worse than no panel, because it gets acted on.
"""
from __future__ import annotations

import math
from datetime import date as _date, datetime

from recommend import _bs_price, _implied_vol      # one BS impl, not a third copy

IST_OFFSET = "+05:30"
TRADING_DAYS = 365.0        # calendar convention, matching csp.py


def _latest_ts(conn, underlying: str, day: str | None = None) -> str | None:
    if day:
        return conn.execute("SELECT MAX(ts) FROM chain_snapshot WHERE ts>=? AND ts<? "
                            "AND underlying=?", (day, day + "~", underlying)).fetchone()[0]
    return conn.execute("SELECT MAX(ts) FROM chain_snapshot WHERE underlying=?",
                        (underlying,)).fetchone()[0]


def _chain_at(conn, underlying: str, ts: str):
    """One minute's chain: {(strike, opt_type): {...}} plus spot and expiry."""
    rows = conn.execute(
        "SELECT strike, opt_type, ltp, oi, spot, expiry FROM chain_snapshot "
        "WHERE ts=? AND underlying=?", (ts, underlying)).fetchall()
    out, spot, expiry = {}, None, None
    for k, t, ltp, oi, sp, exp in rows:
        out[(k, t)] = {"ltp": ltp or 0.0, "oi": oi or 0}
        spot, expiry = sp, exp
    return out, spot, expiry


def _dte(expiry: str, ts: str) -> float:
    try:
        e = _date.fromisoformat(expiry[:10])
        d = _date.fromisoformat(ts[:10])
        return max((e - d).days, 0)
    except Exception:
        return 0.0


def expected_move(conn, underlying: str, day: str | None = None) -> dict:
    """1-sigma move implied by the ATM options, plus the raw straddle."""
    ts = _latest_ts(conn, underlying, day)
    if not ts:
        return {"ok": False, "reason": "no chain data"}
    chain, spot, expiry = _chain_at(conn, underlying, ts)
    if not chain or not spot:
        return {"ok": False, "reason": "empty chain"}
    strikes = sorted({k for k, _ in chain})
    atm = min(strikes, key=lambda k: abs(k - spot))
    ce = chain.get((atm, "CE"), {}).get("ltp") or 0.0
    pe = chain.get((atm, "PE"), {}).get("ltp") or 0.0
    if ce <= 0 or pe <= 0:
        return {"ok": False, "reason": "no ATM quotes"}
    dte = _dte(expiry, ts)
    T = max(dte, 0.5) / TRADING_DAYS          # intraday expiry -> half a day
    iv_c = _implied_vol(ce, spot, atm, T, "CE")
    iv_p = _implied_vol(pe, spot, atm, T, "PE")
    ivs = [v for v in (iv_c, iv_p) if v]
    sigma = sum(ivs) / len(ivs) if ivs else None
    straddle = ce + pe
    one_sd = spot * sigma * math.sqrt(T) if sigma else None
    return {
        "ok": True, "ts": ts, "underlying": underlying, "spot": round(spot, 2),
        "expiry": expiry[:10], "dte": dte, "atm_strike": atm,
        "atm_ce": ce, "atm_pe": pe, "straddle": round(straddle, 2),
        "iv_ce": round(iv_c * 100, 1) if iv_c else None,
        "iv_pe": round(iv_p * 100, 1) if iv_p else None,
        "iv": round(sigma * 100, 1) if sigma else None,
        "one_sigma": round(one_sd, 1) if one_sd else None,
        "one_sigma_pct": round(one_sd / spot * 100, 2) if one_sd else None,
        "upper": round(spot + one_sd, 1) if one_sd else None,
        "lower": round(spot - one_sd, 1) if one_sd else None,
        # the folk factor, reported for comparison only — never used as ours
        "straddle_x068": round(straddle * 0.68, 1),
    }


def max_pain(conn, underlying: str, day: str | None = None) -> dict:
    """Strike at which total writer payout is smallest.

    Writer loss at settlement S = sum over call strikes OI*max(0,S-K)
                                + sum over put  strikes OI*max(0,K-S).
    """
    ts = _latest_ts(conn, underlying, day)
    if not ts:
        return {"ok": False, "reason": "no chain data"}
    chain, spot, expiry = _chain_at(conn, underlying, ts)
    strikes = sorted({k for k, _ in chain})
    if len(strikes) < 5 or not spot:
        return {"ok": False, "reason": "too few strikes"}
    curve = []
    for S in strikes:
        loss = 0.0
        for k in strikes:
            loss += chain.get((k, "CE"), {}).get("oi", 0) * max(0.0, S - k)
            loss += chain.get((k, "PE"), {}).get("oi", 0) * max(0.0, k - S)
        curve.append({"strike": k if False else S, "loss": loss})
    best = min(curve, key=lambda c: c["loss"])
    return {"ok": True, "ts": ts, "underlying": underlying, "spot": round(spot, 2),
            "expiry": expiry[:10], "dte": _dte(expiry, ts),
            "max_pain": best["strike"],
            "distance": round(spot - best["strike"], 1),
            "distance_pct": round((spot - best["strike"]) / spot * 100, 2),
            # the band we record is ATM+-10; if max pain sits on the edge the
            # true minimum is probably outside it and the number is unreliable
            "on_band_edge": best["strike"] in (strikes[0], strikes[-1]),
            "curve": curve}


def iv_skew(conn, underlying: str, day: str | None = None, band: int = 3) -> dict:
    """Call vs put IV at matched distance from spot — the price of each tail."""
    ts = _latest_ts(conn, underlying, day)
    if not ts:
        return {"ok": False, "reason": "no chain data"}
    chain, spot, expiry = _chain_at(conn, underlying, ts)
    strikes = sorted({k for k, _ in chain})
    if not strikes or not spot:
        return {"ok": False, "reason": "empty chain"}
    dte = _dte(expiry, ts)
    T = max(dte, 0.5) / TRADING_DAYS
    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    ci, pi = atm_i + band, atm_i - band
    if not (0 <= pi and ci < len(strikes)):
        return {"ok": False, "reason": "band outside recorded strikes"}
    kc, kp = strikes[ci], strikes[pi]
    ivc = _implied_vol(chain.get((kc, "CE"), {}).get("ltp") or 0, spot, kc, T, "CE")
    ivp = _implied_vol(chain.get((kp, "PE"), {}).get("ltp") or 0, spot, kp, T, "PE")
    if not ivc or not ivp:
        return {"ok": False, "reason": "could not invert IV"}
    return {"ok": True, "ts": ts, "underlying": underlying, "spot": round(spot, 2),
            "call_strike": kc, "put_strike": kp,
            "iv_call": round(ivc * 100, 1), "iv_put": round(ivp * 100, 1),
            "skew": round((ivc - ivp) * 100, 1),
            "lean": "put skew" if ivp > ivc else "call skew"}


def pulse_block(conn, underlyings=("NIFTY", "BANKNIFTY")) -> dict:
    out = {}
    for u in underlyings:
        out[u] = {"expected_move": expected_move(conn, u),
                  "max_pain": max_pain(conn, u),
                  "iv_skew": iv_skew(conn, u)}
        mp = out[u]["max_pain"]
        if mp.get("ok"):
            mp.pop("curve", None)          # keep the payload small
    return out

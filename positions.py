"""Live positions from Kite: legs, greeks, payoff curves, margins, what-ifs.

One engine feeds both the Positions tab and the assistant's tools, so the
numbers the chat quotes are the numbers on screen.

PRICING. Kite returns no greeks, so each option leg's IV is backed out of its
market price with the same Black-Scholes code the strategy builders use
(recommend._bs_price / _implied_vol, r=6.5%, q=1.2%), and greeks come from
that IV. The price is the bid/ask MID when both sides are quoted (a short
leg's LTP can be minutes stale) and the LTP otherwise; `price_basis` says
which. A leg whose quote can't support an IV (deep ITM at intrinsic, no bid)
borrows the median IV of its underlying's other legs, or 15%, and says so
(`iv_assumed`).

UNITS. Kite position quantities are in UNITS (shares), not lots: -65 is one
short NIFTY lot. So a position's delta in units is also its rupee P&L per
1-point move in the underlying.
  delta      units (≈ ₹ per 1 point)      theta_day  ₹ per calendar day
  vega_pt    ₹ per 1 vol point           gamma      delta change per 1 point
Across underlyings only RUPEE figures add up, so book totals use
delta_rs_1pct (₹ for a 1% move in every underlying) and gamma_rs_1pct (the
convexity term, ½·Γ·(1%·S)²).

PAYOFF. Each curve is P&L versus the underlying, anchored so it passes
through today's Kite P&L at today's spot. "today" reprices every leg at its
current IV; "at expiry" values the nearest-expiry legs at intrinsic and any
later legs by Black-Scholes with their remaining time (a calendar's back
month keeps its time value — the usual way to draw a multi-expiry payoff).

MARGIN. Three numbers, because they answer different questions:
  standalone  each leg as if placed alone today (Kite order_margins)
  combined    the underlying's legs as one basket, hedge benefit included
              (Kite basket_order_margins, consider_positions=False)
  account     what Kite has actually blocked (margins().utilised), which
              also carries MTM and premium effects the calculators don't.
"""
from __future__ import annotations

import hashlib
import math
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from recommend import _bs_price, _implied_vol, bs_greeks

IST = timezone(timedelta(hours=5, minutes=30))
YEAR_S = 365.0 * 86400
FALLBACK_IV = 0.15
CURVE_POINTS = 121
MARGIN_TTL_S = 120          # margins are 2-4 Kite calls; positions rarely change
BOOK_TTL_S = 10             # the UI polls and the assistant's tools share this
MARGIN_CHUNK = 20

# index option underlyings -> the spot instrument Kite quotes
INDEX_SPOT = {
    "NIFTY": "NSE:NIFTY 50",
    "BANKNIFTY": "NSE:NIFTY BANK",
    "FINNIFTY": "NSE:NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NSE:NIFTY MID SELECT",
    "NIFTYNXT50": "NSE:NIFTY NEXT 50",
    "SENSEX": "BSE:SENSEX",
    "BANKEX": "BSE:BANKEX",
}
FNO_EXCHANGES = ("NFO", "BFO")

_lock = threading.Lock()
_index = {}                 # exchange -> (day, {tradingsymbol: instrument row})
_bfo_rows = {"day": None, "rows": None}
_margin_cache = {"key": None, "ts": 0.0, "data": None}
_book_cache = {"ts": 0.0, "book": None}


# ------------------------------------------------------------ instruments --

def _rows_for(kite, exchange: str) -> list:
    if exchange == "NFO":
        import recorder                       # shares the recorder's daily cache
        return recorder._get_instruments(kite)
    today = datetime.now(IST).date()
    if _bfo_rows["day"] != today:
        _bfo_rows.update(day=today, rows=kite.instruments(exchange))
    return _bfo_rows["rows"] or []


def instrument(kite, exchange: str, tradingsymbol: str) -> Optional[dict]:
    today = datetime.now(IST).date()
    with _lock:
        day, idx = _index.get(exchange, (None, None))
        if day != today or idx is None:
            idx = {r["tradingsymbol"]: r for r in _rows_for(kite, exchange)}
            _index[exchange] = (today, idx)
    return idx.get(tradingsymbol)


def option_rows(kite, name: str, exchange: str = "NFO") -> list:
    """Every listed CE/PE row for one underlying (for chains)."""
    return [r for r in _rows_for(kite, exchange)
            if r.get("name") == name and r.get("instrument_type") in ("CE", "PE")]


def spot_symbol(name: str, exchange: str) -> str:
    if name in INDEX_SPOT:
        return INDEX_SPOT[name]
    return f"{'BSE' if exchange == 'BFO' else 'NSE'}:{name}"


# ----------------------------------------------------------------- helpers --

def _as_date(x) -> Optional[date]:
    if x is None or x == "":
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def years_to(expiry: date, now: datetime) -> float:
    """Calendar years to the 15:30 IST expiry bell."""
    close = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=IST)
    return max(0.0, (close - now).total_seconds() / YEAR_S)


def _mid(q: dict) -> tuple:
    """(price, basis) — the bid/ask mid when the book is two-sided."""
    depth = (q or {}).get("depth") or {}
    try:
        bid = float((depth.get("buy") or [{}])[0].get("price") or 0)
        ask = float((depth.get("sell") or [{}])[0].get("price") or 0)
    except (TypeError, ValueError, IndexError, AttributeError):
        bid = ask = 0.0
    ltp = float((q or {}).get("last_price") or 0)
    if bid > 0 and ask > 0 and ask >= bid:
        return (bid + ask) / 2, "mid", bid, ask
    return ltp, "ltp", (bid or None), (ask or None)


def _r(x, n=2):
    return None if x is None else round(float(x), n)


def _quote_all(kite, symbols: list) -> dict:
    out = {}
    syms = sorted(set(s for s in symbols if s))
    for i in range(0, len(syms), 400):
        try:
            out.update(kite.quote(syms[i:i + 400]) or {})
        except Exception:
            for s in syms[i:i + 400]:
                try:
                    out.update(kite.quote([s]) or {})
                except Exception:
                    pass
    return out


# ------------------------------------------------------------------- legs --

def _leg(p: dict, ins: Optional[dict], q: dict, spot: Optional[float], now: datetime) -> dict:
    qty = int(p.get("quantity") or 0)
    ex = p.get("exchange")
    leg = {
        "tradingsymbol": p.get("tradingsymbol"), "exchange": ex,
        "product": p.get("product"), "qty": qty,
        "avg": _r(p.get("average_price")), "ltp": _r(p.get("last_price")),
        "pnl": _r(p.get("pnl")), "m2m": _r(p.get("m2m")),
        "unrealised": _r(p.get("unrealised")), "realised": _r(p.get("realised")),
    }
    itype = (ins or {}).get("instrument_type")
    lot = int((ins or {}).get("lot_size") or 1)
    leg["lot_size"] = lot
    leg["lots"] = _r(qty / lot, 2) if lot else None
    if ex in FNO_EXCHANGES and itype in ("CE", "PE") and spot:
        exp = _as_date(ins.get("expiry"))
        K = float(ins.get("strike") or 0)
        T = years_to(exp, now) if exp else 0.0
        price, basis, bid, ask = _mid(q)
        if not price:
            price, basis = float(p.get("last_price") or 0), "ltp"
        iv = _implied_vol(price, spot, K, T, itype) if T > 0 else None
        leg.update(kind="option", opt_type=itype, strike=K,
                   expiry=exp.isoformat() if exp else None,
                   dte=_r(T * 365, 2), T=T, price=_r(price), price_basis=basis,
                   bid=_r(bid), ask=_r(ask), oi=(q or {}).get("oi"),
                   iv=_r(iv * 100, 2) if iv else None, _iv=iv)
    elif ex in FNO_EXCHANGES and itype == "FUT":
        exp = _as_date(ins.get("expiry"))
        price = float((q or {}).get("last_price") or p.get("last_price") or 0)
        leg.update(kind="future", expiry=exp.isoformat() if exp else None,
                   price=_r(price), basis=(price - spot) if (price and spot) else 0.0)
    elif ex in ("NSE", "BSE"):
        price = float((q or {}).get("last_price") or p.get("last_price") or 0)
        leg.update(kind="equity", price=_r(price), basis=0.0)
    else:
        leg.update(kind="unsupported",
                   note="not priced (no spot quote, or a segment this tab doesn't model)")
    return leg


def _apply_greeks(leg: dict, S: float) -> None:
    qty = leg["qty"]
    if leg["kind"] == "option":
        g = bs_greeks(S, leg["strike"], leg["T"], leg["_iv_used"], leg["opt_type"])
    elif leg["kind"] in ("future", "equity"):
        g = {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    else:
        g = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    leg["greeks_unit"] = {k: _r(v, 6) for k, v in g.items()}
    leg["delta"] = _r(g["delta"] * qty, 2)
    leg["gamma"] = _r(g["gamma"] * qty, 5)
    leg["theta_day"] = _r(g["theta"] * qty, 2)
    leg["vega_pt"] = _r(g["vega"] * qty, 2)


def _value(leg: dict, S: float, t_shift: float = 0.0, iv_shift: float = 0.0) -> float:
    """Model value per unit at underlying S, `t_shift` years on, IV moved."""
    if leg["kind"] == "option":
        T = leg["T"] - t_shift
        if T <= 0:
            return max(0.0, (S - leg["strike"]) if leg["opt_type"] == "CE" else (leg["strike"] - S))
        return _bs_price(S, leg["strike"], T, max(0.01, leg["_iv_used"] + iv_shift), leg["opt_type"])
    if leg["kind"] in ("future", "equity"):
        return S + leg.get("basis", 0.0)
    return 0.0


def _priced(legs: list) -> list:
    return [l for l in legs if l["qty"] and l["kind"] in ("option", "future", "equity")]


# ----------------------------------------------------------------- groups --

def _group_greeks(legs: list, S: float, lot: int) -> dict:
    d = sum(l["delta"] or 0 for l in legs)
    g = sum(l["gamma"] or 0 for l in legs)
    move = S * 0.01 if S else 0.0
    return {"delta": _r(d, 2), "delta_lots": _r(d / lot, 2) if lot else None,
            "gamma": _r(g, 5),
            "theta_day": _r(sum(l["theta_day"] or 0 for l in legs), 0),
            "vega_pt": _r(sum(l["vega_pt"] or 0 for l in legs), 0),
            "delta_rs_1pct": _r(d * move, 0),
            "gamma_rs_1pct": _r(0.5 * g * move * move, 0)}


def _crossings(xs: list, ys: list) -> list:
    out = []
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if a is None or b is None or a == b:
            continue
        if (a < 0) != (b < 0):
            out.append(round(xs[i - 1] + (xs[i] - xs[i - 1]) * a / (a - b), 1))
    return out


def _curves(legs: list, S: float, pnl_now: float) -> Optional[dict]:
    live = _priced(legs)
    if not live or not S:
        return None
    opts = [l for l in live if l["kind"] == "option"]
    ivs = [l["_iv_used"] for l in opts]
    iv_ref = statistics.median(ivs) if ivs else FALLBACK_IV
    t_near = min((l["T"] for l in opts), default=0.0)
    half = 3 * iv_ref * math.sqrt(max(t_near, 2 / 365))
    half = min(max(half, 0.03), 0.25)
    lo, hi = S * (1 - half), S * (1 + half)
    for l in opts:                            # keep every strike in view
        if l["strike"] and 0.6 * S < l["strike"] < 1.4 * S:
            lo, hi = min(lo, l["strike"] * 0.99), max(hi, l["strike"] * 1.01)
    xs = [lo + (hi - lo) * i / (CURVE_POINTS - 1) for i in range(CURVE_POINTS)]
    base = sum(l["qty"] * _value(l, S) for l in live)
    today = [pnl_now + sum(l["qty"] * _value(l, x) for l in live) - base for x in xs]
    expiry = [pnl_now + sum(l["qty"] * _value(l, x, t_near) for l in live) - base for x in xs]
    near_exp = min((l["expiry"] for l in opts if l.get("expiry")), default=None)
    sd = S * iv_ref * math.sqrt(max(t_near, 1 / 365))
    return {"spots": [round(x, 1) for x in xs],
            "today": [round(v) for v in today], "expiry": [round(v) for v in expiry],
            "expiry_date": near_exp, "spot": S, "iv_ref": round(iv_ref * 100, 2),
            "one_sigma": [round(S - sd, 1), round(S + sd, 1)] if opts else None,
            "breakevens_today": _crossings(xs, today),
            "breakevens_expiry": _crossings(xs, expiry),
            "max_profit_in_view": max(expiry), "max_loss_in_view": min(expiry),
            "multi_expiry": len({l.get("expiry") for l in opts}) > 1}


# ---------------------------------------------------------------- margins --

def _order(leg: dict) -> dict:
    return {"exchange": leg["exchange"], "tradingsymbol": leg["tradingsymbol"],
            "transaction_type": "SELL" if leg["qty"] < 0 else "BUY",
            "variety": "regular", "product": leg.get("product") or "NRML",
            "order_type": "MARKET", "quantity": abs(int(leg["qty"])),
            "price": 0, "trigger_price": 0}


def _total(m) -> Optional[float]:
    if not isinstance(m, dict):
        return None
    t = m.get("total")
    if t is None and (m.get("span") is not None or m.get("exposure") is not None):
        t = (m.get("span") or 0) + (m.get("exposure") or 0) + (m.get("option_premium") or 0)
    return None if t is None else float(t)


def margins(kite, groups: dict) -> dict:
    """{per_leg: {key: ₹}, per_group: {name: {combined, initial}}, overall}."""
    fno = [l for legs in groups.values() for l in legs
           if l["qty"] and l["exchange"] in FNO_EXCHANGES and l["kind"] in ("option", "future")]
    key = hashlib.sha1(repr(sorted((l["tradingsymbol"], l.get("product"), l["qty"])
                                   for l in fno)).encode()).hexdigest()
    if (_margin_cache["key"] == key and _margin_cache["data"] is not None
            and time.time() - _margin_cache["ts"] < MARGIN_TTL_S):
        return _margin_cache["data"]
    out = {"per_leg": {}, "per_group": {}, "overall": None, "errors": []}
    for i in range(0, len(fno), MARGIN_CHUNK):
        chunk = fno[i:i + MARGIN_CHUNK]
        try:
            res = kite.order_margins([_order(l) for l in chunk]) or []
            for l, m in zip(chunk, res):
                out["per_leg"][f"{l['tradingsymbol']}|{l.get('product')}"] = _total(m)
        except Exception as e:
            out["errors"].append(f"order_margins: {type(e).__name__}: {e}")

    def basket(legs):
        res = kite.basket_order_margins([_order(l) for l in legs], consider_positions=False)
        return {"combined": _total((res or {}).get("final")),
                "initial": _total((res or {}).get("initial"))}

    for name, legs in groups.items():
        gl = [l for l in legs if l["qty"] and l["exchange"] in FNO_EXCHANGES
              and l["kind"] in ("option", "future")]
        if not gl:
            continue
        try:
            out["per_group"][name] = basket(gl)
        except Exception as e:
            out["errors"].append(f"basket {name}: {type(e).__name__}: {e}")
    if fno:
        try:
            out["overall"] = basket(fno)
        except Exception as e:
            out["errors"].append(f"basket overall: {type(e).__name__}: {e}")
    _margin_cache.update(key=key, ts=time.time(), data=out)
    return out


# ------------------------------------------------------------------ book --

def fetch_book(kite, with_margins: bool = True, now: Optional[datetime] = None,
               use_cache: bool = True) -> dict:
    """Everything the tab and the assistant need, from live Kite calls."""
    if (use_cache and now is None and _book_cache["book"] is not None
            and time.time() - _book_cache["ts"] < BOOK_TTL_S
            and (not with_margins or _book_cache["book"].get("margins_included"))):
        return _book_cache["book"]
    cacheable = now is None                   # a pinned clock (tests) never caches
    now = now or datetime.now(IST)
    warnings = []
    net = (kite.positions() or {}).get("net") or []
    rows = [p for p in net if p.get("quantity") or p.get("realised") or p.get("pnl")]
    meta = {}
    for p in rows:
        ex = p.get("exchange")
        ins = None
        if ex in FNO_EXCHANGES:
            try:
                ins = instrument(kite, ex, p.get("tradingsymbol"))
            except Exception as e:
                warnings.append(f"instruments {ex}: {type(e).__name__}: {e}")
        if ex in FNO_EXCHANGES and ins:
            name = ins.get("name")
        elif ex in ("NSE", "BSE"):
            name = p.get("tradingsymbol")
        else:
            name = p.get("tradingsymbol")
        meta[(ex, p.get("tradingsymbol"), p.get("product"))] = (ins, name)
    spot_syms = {}
    for (ex, ts, _), (ins, name) in meta.items():
        if ex in FNO_EXCHANGES and name:
            spot_syms[name] = spot_symbol(name, ex)
        elif ex in ("NSE", "BSE"):
            spot_syms[name] = f"{ex}:{ts}"
    quotes = _quote_all(kite, [f"{p.get('exchange')}:{p.get('tradingsymbol')}"
                               for p in rows if p.get("quantity")] + list(spot_syms.values()))
    spots = {n: float((quotes.get(s) or {}).get("last_price") or 0) or None
             for n, s in spot_syms.items()}
    groups: dict = {}
    for p in rows:
        ins, name = meta[(p.get("exchange"), p.get("tradingsymbol"), p.get("product"))]
        q = quotes.get(f"{p.get('exchange')}:{p.get('tradingsymbol')}") or {}
        if p.get("exchange") in FNO_EXCHANGES and not ins:
            warnings.append(f"{p.get('tradingsymbol')}: not in today's instrument list "
                            "(expired?) — shown without greeks")
        groups.setdefault(name, []).append(_leg(p, ins, q, spots.get(name), now))

    out_groups = []
    for name, legs in groups.items():
        S = spots.get(name)
        opts = [l for l in legs if l["qty"] and l["kind"] == "option"]
        have = [l["_iv"] for l in opts if l.get("_iv")]
        fill = statistics.median(have) if have else FALLBACK_IV
        for l in opts:
            l["_iv_used"] = l["_iv"] or fill
            l["iv_assumed"] = not l["_iv"]
        open_legs = [l for l in legs if l["qty"]]
        for l in open_legs:
            _apply_greeks(l, S or 0.0)
        lot = next((l["lot_size"] for l in opts), None) or next(
            (l["lot_size"] for l in open_legs), 1)
        pnl = sum(l["pnl"] or 0 for l in legs)
        out_groups.append({
            "underlying": name, "spot": S, "spot_symbol": spot_syms.get(name),
            "lot_size": lot, "legs": open_legs,
            "closed": [l for l in legs if not l["qty"]],
            "pnl": _r(pnl, 0),
            "unrealised": _r(sum(l["unrealised"] or 0 for l in legs), 0),
            "realised": _r(sum(l["realised"] or 0 for l in legs), 0),
            "greeks": _group_greeks(open_legs, S or 0.0, lot),
            "nearest_expiry": min((l["expiry"] for l in opts if l.get("expiry")), default=None),
            "curve": _curves(open_legs, S, pnl),
        })
    out_groups.sort(key=lambda g: -abs(g["greeks"]["delta_rs_1pct"] or 0) - abs(g["pnl"] or 0))

    book = {"ok": True, "as_of": now.isoformat(timespec="seconds"),
            "groups": out_groups, "warnings": warnings, "margins_included": False}
    if with_margins:
        try:
            m = margins(kite, {g["underlying"]: g["legs"] for g in out_groups})
            for g in out_groups:
                for l in g["legs"]:
                    l["margin"] = _r(m["per_leg"].get(f"{l['tradingsymbol']}|{l.get('product')}"), 0)
                stand = [l["margin"] for l in g["legs"] if l.get("margin") is not None]
                comb = (m["per_group"].get(g["underlying"]) or {}).get("combined")
                g["margin"] = {"standalone_sum": _r(sum(stand), 0) if stand else None,
                               "combined": _r(comb, 0),
                               "hedge_benefit": _r(sum(stand) - comb, 0)
                               if (stand and comb is not None) else None}
            book["margin_errors"] = m.get("errors") or []
            book["margins_included"] = True
            overall = (m.get("overall") or {}).get("combined")
            stand_all = [l["margin"] for g in out_groups for l in g["legs"]
                         if l.get("margin") is not None]
            book["margin"] = {"standalone_sum": _r(sum(stand_all), 0) if stand_all else None,
                              "combined": _r(overall, 0),
                              "hedge_benefit": _r(sum(stand_all) - overall, 0)
                              if (stand_all and overall is not None) else None}
        except Exception as e:
            book["margin_errors"] = [f"{type(e).__name__}: {e}"]
    try:
        eq = (kite.margins() or {}).get("equity") or {}
        avail = eq.get("available") or {}
        used = eq.get("utilised") or {}
        book["account"] = {"available": _r(avail.get("live_balance", avail.get("cash")), 0),
                           "utilised": _r(used.get("debits"), 0),
                           "span": _r(used.get("span"), 0),
                           "exposure": _r(used.get("exposure"), 0),
                           "option_premium": _r(used.get("option_premium"), 0)}
    except Exception as e:
        book["account"] = {"error": f"{type(e).__name__}: {e}"}
    gs = out_groups
    book["totals"] = {
        "pnl": _r(sum(g["pnl"] or 0 for g in gs), 0),
        "unrealised": _r(sum(g["unrealised"] or 0 for g in gs), 0),
        "realised": _r(sum(g["realised"] or 0 for g in gs), 0),
        "theta_day": _r(sum(g["greeks"]["theta_day"] or 0 for g in gs), 0),
        "vega_pt": _r(sum(g["greeks"]["vega_pt"] or 0 for g in gs), 0),
        "delta_rs_1pct": _r(sum(g["greeks"]["delta_rs_1pct"] or 0 for g in gs), 0),
        "gamma_rs_1pct": _r(sum(g["greeks"]["gamma_rs_1pct"] or 0 for g in gs), 0),
        "open_legs": sum(len(g["legs"]) for g in gs),
    }
    for g in gs:                               # private working fields out
        for l in g["legs"] + g["closed"]:
            for k in ("_iv",):
                l.pop(k, None)
    if cacheable:
        _book_cache.update(ts=time.time(), book=book)
    return book


def public(book: dict) -> dict:
    """The book without model internals (T, _iv_used) — what the API serves."""
    import copy
    b = copy.deepcopy(book)
    for g in b.get("groups", []):
        for l in g["legs"]:
            l.pop("T", None)
            l.pop("_iv_used", None)
    return b


# --------------------------------------------------------------- scenario --

def scenario(book: dict, underlying: Optional[str] = None,
             spot_change_pct: Optional[float] = None, spot_level: Optional[float] = None,
             iv_change_pts: float = 0.0, days_forward: float = 0.0) -> dict:
    """Reprice the book under a move: P&L change and greeks after, per group.

    A % move applies to every underlying in scope — across a book holding both
    NIFTY and stocks that is a beta-of-1 assumption, and the output says so.
    `spot_level` needs a single underlying."""
    groups = [g for g in book.get("groups", [])
              if underlying is None or g["underlying"] == underlying]
    if underlying and not groups:
        return {"ok": False, "reason": f"no open position in {underlying}"}
    if spot_level is not None and len(groups) != 1:
        return {"ok": False, "reason": "spot_level needs exactly one underlying"}
    t_shift, iv_shift = (days_forward or 0) / 365.0, (iv_change_pts or 0) / 100.0
    res = []
    for g in groups:
        S = g["spot"]
        live = [l for l in _priced(g["legs"]) if "_iv_used" in l or l["kind"] != "option"]
        if not S or not live:
            continue
        S2 = spot_level if spot_level is not None else S * (1 + (spot_change_pct or 0) / 100)
        legs = []
        for l in live:
            v0, v1 = _value(l, S), _value(l, S2, t_shift, iv_shift)
            row = {"tradingsymbol": l["tradingsymbol"], "qty": l["qty"],
                   "value_now": _r(v0), "value_after": _r(v1),
                   "pnl_change": _r(l["qty"] * (v1 - v0), 0)}
            if l["kind"] == "option":
                T2 = l["T"] - t_shift
                gk = bs_greeks(S2, l["strike"], T2, max(0.01, l["_iv_used"] + iv_shift), l["opt_type"])
                row["delta_after"] = _r(gk["delta"] * l["qty"], 2)
                row["expired_by_then"] = T2 <= 0
            legs.append(row)
        chg = sum(x["pnl_change"] or 0 for x in legs)
        res.append({"underlying": g["underlying"], "spot_now": S, "spot_after": _r(S2),
                    "pnl_now": g["pnl"], "pnl_change": _r(chg, 0),
                    "pnl_after": _r((g["pnl"] or 0) + chg, 0), "legs": legs})
    return {"ok": True, "assumptions": {
                "spot_change_pct": spot_change_pct, "spot_level": spot_level,
                "iv_change_pts": iv_change_pts, "days_forward": days_forward,
                "model": "Black-Scholes at each leg's current IV (+ shift), r 6.5%, q 1.2%",
                "beta_note": ("same % move applied to every underlying (beta 1)"
                              if len(res) > 1 and spot_level is None else None)},
            "groups": res,
            "total_pnl_change": _r(sum(x["pnl_change"] or 0 for x in res), 0)}


def compact(book: dict, underlying: Optional[str] = None) -> dict:
    """A token-light view for the assistant's context."""
    if not book.get("ok"):
        return book
    gs = []
    for g in book["groups"]:
        if underlying and g["underlying"] != underlying:
            continue
        c = g.get("curve") or {}
        gs.append({
            "underlying": g["underlying"], "spot": g["spot"], "lot_size": g["lot_size"],
            "pnl": g["pnl"], "realised": g["realised"], "greeks": g["greeks"],
            "margin": g.get("margin"), "nearest_expiry": g["nearest_expiry"],
            "breakevens_at_expiry": c.get("breakevens_expiry"),
            "breakevens_today": c.get("breakevens_today"),
            "one_sigma_to_nearest_expiry": c.get("one_sigma"),
            "max_profit_in_view": c.get("max_profit_in_view"),
            "max_loss_in_view": c.get("max_loss_in_view"),
            "legs": [{k: l.get(k) for k in (
                "tradingsymbol", "kind", "product", "qty", "lots", "opt_type", "strike",
                "expiry", "dte", "avg", "price", "price_basis", "bid", "ask", "pnl", "iv",
                "iv_assumed", "delta", "gamma", "theta_day", "vega_pt", "margin", "oi")
                if l.get(k) is not None} for l in g["legs"]],
            "closed_today": [{"tradingsymbol": l["tradingsymbol"], "realised": l["realised"]}
                             for l in g["closed"]],
        })
    out = {"as_of": book["as_of"], "groups": gs, "warnings": book.get("warnings") or []}
    if not underlying:
        out.update(totals=book.get("totals"), margin=book.get("margin"),
                   account=book.get("account"))
    return out

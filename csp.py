"""
Cash-Secured Puts (#79) — sized PE ideas on the equity F&O universe.

WHAT A CSP IS HERE: you sell a put and hold the FULL assignment cost in cash
(strike x lot). If it expires worthless you keep the premium; if you are
assigned you buy the stock at the strike. So the trade is really "get paid to
place a limit buy" — which is why this module refuses to treat assignment as
failure and instead reports what your cost basis becomes if it happens.

FOUR DESIGN DECISIONS, all deliberate:

1. PRICE FROM THE BID, NEVER THE LTP.  You sell into the bid. LTP is the last
   trade, which may be stale or at the ask, and on cheap options the gap is
   enormous — a Rs 0.60 put with a Rs 0.05 tick can lose 8-17% of its premium
   to the spread alone. Every premium, yield and IV in here is computed from
   `depth.buy[0].price`. We also carry the LTP and the spread so the UI can
   show what the optimistic number would have been.

2. TWO PROBABILITIES, NOT ONE.  Dashboards of this kind quote "80% safe" from
   REALIZED vol while the option is priced off IMPLIED vol. Implied normally
   runs above realized (that gap is the seller's edge), so a realized-vol
   probability flatters the trade. We publish both:
       p_otm_realized — P(expires worthless) on realized sigma
       p_otm_implied  — the same on the market's own IV
   The difference is the honest measure of how much you are being paid.

3. LOGNORMAL, AND SAID SO.  sigma*sqrt(T) with a lognormal assumption
   understates fat left tails, which is the only direction that hurts a put
   seller. Both probabilities are therefore optimistic in the tail; the UI
   labels them estimates, not guarantees.

4. FUNDAMENTALS ARE BEST-EFFORT AND FAIL SAFE.  Kite provides none. NSE 403s
   non-browser traffic and Yahoo 429s aggressively, so neither source can be
   relied on. The RELIABLE distress screen is price-based (below 200-DMA, far
   off the 52-week high, sustained decline, IV spike) and always runs; any
   fundamental data is an optional overlay that may be absent or stale, and
   is labelled as such rather than silently trusted.

CADENCE: daily bars are one historical call per name (~200 calls), so they are
cached once per day. Only quotes refresh on the 30-minute market-hours job,
and those batch 500 instruments per request.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# --- tunables (all overridable per request) ---
TARGET_P_OTM = 0.80        # desired P(expires worthless), on realized vol
MIN_DROP_PCT = 5.0         # "already fallen" dip filter
HIST_DAYS = 400            # daily bars pulled per name (52w high + 200-DMA)
VOL_LOOKBACK = 60          # sessions for realized vol
MIN_PREMIUM = 0.50         # ignore near-worthless puts (spread eats them)
MAX_SPREAD_PCT = 25.0      # flag illiquid strikes
QUOTE_CHUNK = 200
MARGIN_CHUNK = 50   # orders per margin POST


# ---------------- maths ----------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S, K, T, sig, r=0.065, q=0.0):
    if T <= 0 or sig <= 0:
        return max(0.0, K - S)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def implied_vol_put(price, S, K, T):
    """Bisection IV from a PUT price. None when the quote can't support one."""
    if price is None or price <= 0.01 or T <= 0:
        return None
    intrinsic = max(0.0, K - S)
    if price <= intrinsic + 0.01:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(70):
        mid = (lo + hi) / 2
        if bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    v = (lo + hi) / 2
    return None if (v > 4.9 or v < 2e-4) else v


def p_expires_otm(S, K, T, sig):
    """P(S_T > K) under lognormal — i.e. the put expires worthless.
    Drift is deliberately set to zero: assuming a positive drift would make
    every put look safer, which is exactly the flattery we are avoiding."""
    if T <= 0 or sig <= 0 or S <= 0 or K <= 0:
        return None
    d2 = (math.log(S / K) - 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    return _ncdf(d2)


def strike_for_target(S, T, sig, target_p, step):
    """Largest strike (closest to spot => richest premium) whose estimated
    P(expires worthless) still clears the target. Inverts the lognormal."""
    if T <= 0 or sig <= 0:
        return None
    z = _inv_norm(target_p)
    k = S * math.exp(-z * sig * math.sqrt(T) - 0.5 * sig * sig * T)
    return math.floor(k / step) * step if step else k


def _inv_norm(p: float) -> float:
    """Acklam's inverse normal CDF — adequate to ~1e-9 over (0,1)."""
    if not 0 < p < 1:
        raise ValueError(p)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def realized_vol(closes, lookback=VOL_LOOKBACK):
    """Annualised close-to-close vol from the last `lookback` sessions."""
    c = [x for x in closes if x and x > 0][-(lookback + 1):]
    if len(c) < 20:
        return None
    rets = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    n = len(rets)
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    return math.sqrt(var) * math.sqrt(252)


# ---------------- universe ----------------

def fno_equity_symbols(instruments) -> dict:
    """Equity F&O names with an options chain: {symbol: lot_size}. Indices are
    excluded — this module is about owning shares, and you cannot be assigned
    an index."""
    INDEX = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
    out = {}
    for ins in instruments:
        if ins.get("segment") != "NFO-OPT" or ins.get("instrument_type") != "PE":
            continue
        name = ins.get("name")
        if not name or name in INDEX:
            continue
        out.setdefault(name, int(ins.get("lot_size") or 0))
    return {k: v for k, v in out.items() if v}


def monthly_expiry(instruments, name: str, today: date):
    """Nearest monthly (last) expiry for a name, and the cycle start = the 1st
    of that expiry's month, which is what 'cycle drop' measures from.

    (First version walked back to the PREVIOUS month's 1st, so every drop was
    computed over a ~6-week window instead of the current cycle — it made
    healthy names look dropped and dropped names look flat.)"""
    exps = sorted({i["expiry"] for i in instruments
                   if i.get("name") == name and i.get("segment") == "NFO-OPT"
                   and i.get("expiry") and i["expiry"] >= today})
    if not exps:
        return None, None
    by_month = {}
    for e in exps:
        by_month.setdefault((e.year, e.month), []).append(e)
    front = min(by_month)
    monthly = max(by_month[front])
    return monthly, monthly.replace(day=1)


# ---------------- risk screen (price-based, always available) ----------------

def risk_flags(spot, closes, cycle_drop, d5, rv, iv) -> list:
    """Distress signals derived from PRICE ONLY, so they always work. These are
    market evidence that something may be wrong — not fundamentals."""
    flags = []
    c = [x for x in closes if x]
    if len(c) >= 200:
        dma200 = sum(c[-200:]) / 200
        if spot < dma200:
            flags.append(f"below 200-DMA ({dma200:,.0f})")
    if len(c) >= 250:
        hi52 = max(c[-250:])
        off = (spot / hi52 - 1) * 100
        if off <= -30:
            flags.append(f"{off:.0f}% off 52w high")
    if cycle_drop is not None and cycle_drop <= -15:
        flags.append(f"cycle {cycle_drop:.0f}% — possible news, not noise")
    if d5 is not None and d5 <= -10:
        flags.append(f"5d {d5:.0f}% — sharp recent decline")
    if rv and iv and iv > rv * 1.6:
        flags.append(f"IV {iv*100:.0f}% >> realized {rv*100:.0f}% — event priced")
    return flags


def score_idea(p_impl, ann_yield, cycle_drop, n_flags, spread_pct) -> float:
    """0-100 blend. Deliberately penalises risk flags and wide spreads: a high
    probability on a broken name is not a good trade."""
    s = 0.0
    s += 45 * min(max((p_impl or 0) - 0.60, 0) / 0.35, 1)         # 60%->95% maps 0->45
    s += 30 * min(max(ann_yield or 0, 0) / 40.0, 1)               # 40% p.a. saturates
    if cycle_drop is not None:                                    # the dip we want
        s += 15 * min(max(-cycle_drop, 0) / 12.0, 1)
    s += 10 * (1 - min((spread_pct or 0) / MAX_SPREAD_PCT, 1))    # liquidity
    s -= 12 * n_flags                                             # distress
    return round(max(0.0, min(100.0, s)), 1)


# ---------------- data fetch ----------------

def _chunks(xs, n):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def refresh_daily_bars(kite, instruments, conn, symbols=None, days=HIST_DAYS) -> dict:
    """Cache daily equity bars (one historical call per name — the expensive
    part, so this runs at most once a day, NOT on the 30-minute cadence)."""
    uni = fno_equity_symbols(instruments)
    syms = [s for s in (symbols or uni) if s in uni]
    tokens = {}
    for ins in instruments:
        if ins.get("segment") == "NSE" and ins.get("tradingsymbol") in syms:
            tokens[ins["tradingsymbol"]] = ins["instrument_token"]
    end = datetime.now(IST)
    start = end - timedelta(days=days)
    ok = err = 0
    for sym in syms:
        tok = tokens.get(sym)
        if not tok:
            err += 1
            continue
        try:
            bars = kite.historical_data(tok, start.strftime("%Y-%m-%d"),
                                        end.strftime("%Y-%m-%d"), "day")
            conn.executemany(
                "INSERT OR REPLACE INTO csp_daily (symbol,d,open,high,low,close)"
                " VALUES (?,?,?,?,?,?)",
                [(sym, str(b["date"].date()), b["open"], b["high"], b["low"], b["close"])
                 for b in bars])
            conn.commit()
            ok += 1
        except Exception:
            err += 1
    return {"symbols": len(syms), "ok": ok, "errors": err}


def _load_closes(conn, sym) -> list:
    return [r[0] for r in conn.execute(
        "SELECT close FROM csp_daily WHERE symbol=? ORDER BY d", (sym,)) if r[0]]


def scan(kite, instruments, conn, *, target_p=TARGET_P_OTM, min_drop=MIN_DROP_PCT,
         only_dropped=True, universe=None, today=None, limit=None) -> dict:
    """Build CSP ideas for the F&O universe. Quotes are batched; premiums come
    from the BEST BID. Returns rows sorted by score."""
    today = today or datetime.now(IST).date()
    uni = fno_equity_symbols(instruments)
    syms = sorted(uni)
    if universe:
        syms = [s for s in syms if s in set(universe)]
    if limit:
        syms = syms[:limit]

    # 1) per-name context from cached daily bars
    ctx = {}
    for sym in syms:
        closes = _load_closes(conn, sym)
        if len(closes) < 40:
            continue
        rv = realized_vol(closes)
        if not rv:
            continue
        exp, cyc_start = monthly_expiry(instruments, sym, today)
        if not exp:
            continue
        dte = (exp - today).days
        if dte < 2:
            continue
        cyc = [r for r in conn.execute(
            "SELECT d, close FROM csp_daily WHERE symbol=? AND d>=? ORDER BY d",
            (sym, str(cyc_start)))]
        cyc_open = cyc[0][1] if cyc else None
        ctx[sym] = {"closes": closes, "rv": rv, "expiry": exp, "dte": dte,
                    "cycle_start": cyc_start, "cycle_open": cyc_open,
                    "lot": uni[sym]}
    if not ctx:
        return {"ok": False, "reason": "no symbols with cached daily bars — "
                                       "run refresh_daily_bars first"}

    # 2) live spot for every candidate (batched)
    spot_keys = [f"NSE:{s}" for s in ctx]
    spots = {}
    for ch in _chunks(spot_keys, QUOTE_CHUNK):
        try:
            spots.update(kite.quote(ch))
        except Exception:
            for k in ch:
                try:
                    spots.update(kite.quote([k]))
                except Exception:
                    pass

    # 3) pick the target strike per name, then batch-quote those PUTS
    want = {}
    for sym, c in ctx.items():
        q = spots.get(f"NSE:{sym}") or {}
        spot = q.get("last_price")
        if not spot:
            continue
        strikes = sorted({float(i["strike"]) for i in instruments
                          if i.get("name") == sym and i.get("expiry") == c["expiry"]
                          and i.get("instrument_type") == "PE" and i.get("strike")})
        if len(strikes) < 3:
            continue
        step = min((b - a) for a, b in zip(strikes, strikes[1:])) or 1
        T = c["dte"] / 365.0
        raw = strike_for_target(spot, T, c["rv"], target_p, step)
        if not raw:
            continue
        below = [k for k in strikes if k <= raw]
        if not below:
            continue
        K = max(below)
        sym_pe = next((i["tradingsymbol"] for i in instruments
                       if i.get("name") == sym and i.get("expiry") == c["expiry"]
                       and i.get("instrument_type") == "PE" and float(i["strike"]) == K), None)
        if not sym_pe:
            continue
        c.update(spot=spot, strike=K, step=step, T=T, pe_symbol=sym_pe)
        want[sym] = f"NFO:{sym_pe}"

    pe_quotes = {}
    for ch in _chunks(list(want.values()), QUOTE_CHUNK):
        try:
            pe_quotes.update(kite.quote(ch))
        except Exception:
            for k in ch:
                try:
                    pe_quotes.update(kite.quote([k]))
                except Exception:
                    pass

    # 4) assemble
    rows = []
    for sym, key in want.items():
        c = ctx[sym]
        q = pe_quotes.get(key) or {}
        depth = q.get("depth") or {}
        buy, sell = (depth.get("buy") or []), (depth.get("sell") or [])
        bid = (buy[0].get("price") if buy else None) or 0.0
        ask = (sell[0].get("price") if sell else None) or 0.0
        ltp = q.get("last_price")
        if bid < MIN_PREMIUM:                     # unsellable / spread-dominated
            continue
        spread_pct = ((ask - bid) / bid * 100) if (ask and bid) else None

        spot, K, T, lot = c["spot"], c["strike"], c["T"], c["lot"]
        iv = implied_vol_put(bid, spot, K, T)     # IV from the BID, not the LTP
        p_real = p_expires_otm(spot, K, T, c["rv"])
        p_impl = p_expires_otm(spot, K, T, iv) if iv else None

        closes = c["closes"]
        cyc_open = c["cycle_open"]
        cyc_drop = ((spot / cyc_open - 1) * 100) if cyc_open else None
        d1 = ((spot / closes[-1] - 1) * 100) if closes else None
        d5 = ((spot / closes[-5] - 1) * 100) if len(closes) >= 5 else None
        hi52 = max(closes[-250:]) if len(closes) >= 250 else max(closes)
        dma200 = (sum(closes[-200:]) / 200) if len(closes) >= 200 else None

        cash = K * lot
        prem = bid * lot
        y = prem / cash * 100 if cash else 0.0
        ann = ((1 + prem / cash) ** (365 / max(c["dte"], 1)) - 1) * 100 if cash else 0.0
        flags = risk_flags(spot, closes, cyc_drop, d5, c["rv"], iv)
        if only_dropped and not (cyc_drop is not None and cyc_drop <= -min_drop):
            continue
        rows.append({
            "symbol": sym, "spot": round(spot, 2),
            "expiry": str(c["expiry"]), "dte": c["dte"],
            "cycle_start": str(c["cycle_start"]),
            "cycle_open": round(cyc_open, 2) if cyc_open else None,
            "cycle_drop_pct": round(cyc_drop, 2) if cyc_drop is not None else None,
            "d1_pct": round(d1, 2) if d1 is not None else None,
            "d5_pct": round(d5, 2) if d5 is not None else None,
            "strike": K, "lot_size": lot, "pe_symbol": c["pe_symbol"],
            "otm_pct": round((spot - K) / spot * 100, 2),
            "bid": bid, "ask": ask, "ltp": ltp,
            "spread_pct": round(spread_pct, 1) if spread_pct is not None else None,
            "bid_vs_ltp_pct": round((bid / ltp - 1) * 100, 1) if ltp else None,
            "iv": round(iv * 100, 1) if iv else None,
            "realized_vol": round(c["rv"] * 100, 1),
            "p_otm_realized": round(p_real * 100, 1) if p_real else None,
            "p_otm_implied": round(p_impl * 100, 1) if p_impl else None,
            "premium_total": round(prem, 0),
            "cash_required": round(cash, 0),
            "yield_pct": round(y, 2), "ann_yield_pct": round(ann, 1),
            "from_52w_high": round((spot / hi52 - 1) * 100, 1) if hi52 else None,
            "above_200dma": bool(dma200 and spot > dma200),
            # if assigned, this is what you actually own it at
            "assigned_cost": round(K - bid, 2),
            "assigned_vs_spot_pct": round(((K - bid) / spot - 1) * 100, 2),
            "risk_flags": flags,
            "margin_total": None, "margin_span": None, "margin_exposure": None,
            "return_on_margin_pct": None, "ann_return_on_margin_pct": None,
            "score": score_idea(p_impl, ann, cyc_drop, len(flags), spread_pct),
        })
    apply_margins(rows, fetch_margins(kite, rows))
    rows.sort(key=lambda r: r["score"], reverse=True)
    return {"ok": True, "ts": datetime.now(IST).isoformat(),
            "scanned": len(ctx), "universe_size": len(uni),
            "target_p": target_p, "min_drop": min_drop,
            "only_dropped": only_dropped, "rows": rows}


def fetch_margins(kite, rows: list) -> dict:
    """Exact blocked margin per idea from Kite's own margin calculator.

    A short put is NOT actually cash-secured in an Indian F&O account: the
    broker blocks SPAN + exposure, far less than strike x lot. So the margin
    number has to come from the same engine the broker charges with — an
    approximation here would corrupt the return-on-margin column, which is
    exactly what the ideas get ranked on.

    One POST per chunk; each order is costed standalone (no netting against
    the user's real book), which is what a fresh position would block.
    """
    if not rows:
        return {}
    out = {}
    for chunk in _chunks(rows, MARGIN_CHUNK):
        orders = [{
            "exchange": "NFO",
            "tradingsymbol": r["pe_symbol"],
            "transaction_type": "SELL",
            "variety": "regular",
            "product": "NRML",
            "order_type": "LIMIT",
            "quantity": int(r["lot_size"]),
            "price": float(r["bid"]),
            "trigger_price": 0,
        } for r in chunk]
        try:
            res = kite.order_margins(orders)
        except Exception:
            res = None
        if not res or len(res) != len(chunk):
            continue
        for r, m in zip(chunk, res):
            if not isinstance(m, dict):
                continue
            total = m.get("total")
            if total is None:
                span, expo = m.get("span"), m.get("exposure")
                total = (span or 0) + (expo or 0)
            if not total:
                continue
            out[r["symbol"]] = {
                "margin_total": round(float(total), 0),
                "margin_span": round(float(m.get("span") or 0), 0),
                "margin_exposure": round(float(m.get("exposure") or 0), 0),
            }
    return out


def apply_margins(rows: list, margins: dict) -> int:
    """Attach margin + return-on-margin. Rows with no margin keep None — a
    missing number must read as missing, never as zero (which would rank the
    idea top on any return-on-margin sort)."""
    n = 0
    for r in rows:
        m = margins.get(r["symbol"])
        if not m:
            r["margin_total"] = None
            r["return_on_margin_pct"] = None
            r["ann_return_on_margin_pct"] = None
            continue
        r.update(m)
        prem, mar, dte = r["premium_total"], m["margin_total"], max(r["dte"], 1)
        rom = prem / mar * 100 if mar else None
        r["return_on_margin_pct"] = round(rom, 2) if rom is not None else None
        # SIMPLE annualisation, deliberately not compounded. Return on margin
        # starts high (10-20% for a two-week put), and compounding that to 365
        # days prints 6000%+ — arithmetically true, financially meaningless,
        # and it would make every row look like a jackpot. Simple x365/DTE
        # answers the real question: "what if I kept repeating this trade?"
        r["ann_return_on_margin_pct"] = (
            round(rom * 365 / dte, 0) if rom is not None else None)
        n += 1
    return n


def store_snapshot(conn, res: dict) -> int:
    if not res.get("ok"):
        return 0
    ts = res["ts"]
    n = 0
    for r in res["rows"]:
        conn.execute(
            "INSERT OR REPLACE INTO csp_snapshot (ts,symbol,spot,expiry,dte,strike,"
            "lot_size,bid,ask,ltp,spread_pct,iv,realized_vol,p_otm_realized,"
            "p_otm_implied,otm_pct,premium_total,cash_required,yield_pct,"
            "ann_yield_pct,cycle_open,cycle_drop_pct,d1_pct,d5_pct,from_52w_high,"
            "above_200dma,score,risk_flags,fundamentals,pe_symbol,margin_total,"
            "margin_span,margin_exposure,return_on_margin_pct,"
            "ann_return_on_margin_pct) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, r["symbol"], r["spot"], r["expiry"], r["dte"], r["strike"],
             r["lot_size"], r["bid"], r["ask"], r["ltp"], r["spread_pct"], r["iv"],
             r["realized_vol"], r["p_otm_realized"], r["p_otm_implied"], r["otm_pct"],
             r["premium_total"], r["cash_required"], r["yield_pct"], r["ann_yield_pct"],
             r["cycle_open"], r["cycle_drop_pct"], r["d1_pct"], r["d5_pct"],
             r["from_52w_high"], int(r["above_200dma"]), r["score"],
             json.dumps(r["risk_flags"]), json.dumps(r.get("fundamentals")),
             r.get("pe_symbol"), r.get("margin_total"), r.get("margin_span"),
             r.get("margin_exposure"), r.get("return_on_margin_pct"),
             r.get("ann_return_on_margin_pct")))
        n += 1
    conn.commit()
    return n


def latest_snapshot(conn) -> dict:
    row = conn.execute("SELECT MAX(ts) FROM csp_snapshot").fetchone()
    ts = row[0] if row else None
    if not ts:
        return {"ok": False, "reason": "no snapshot yet"}
    rows = []
    for r in conn.execute("SELECT * FROM csp_snapshot WHERE ts=? ORDER BY score DESC", (ts,)):
        d = dict(r)
        d["risk_flags"] = json.loads(d.get("risk_flags") or "[]")
        try:
            d["fundamentals"] = json.loads(d.get("fundamentals") or "null")
        except Exception:
            d["fundamentals"] = None
        d["above_200dma"] = bool(d.get("above_200dma"))
        rows.append(d)
    return {"ok": True, "ts": ts, "rows": rows}


# ---------------- fundamentals (BEST EFFORT — may be absent) ----------------
# Neither source is official and both block aggressively (NSE 403s non-browser
# traffic; Yahoo 429s). Everything here is wrapped so a failure degrades to
# "unavailable" rather than breaking the scan. The price-based risk_flags()
# screen above is the reliable one and never depends on this.

FUND_TTL_HOURS = 20          # refresh at most ~once a trading day
NSE_HOME = "https://www.nseindia.com"
YF = "https://query1.finance.yahoo.com"
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _nse_session():
    """NSE needs a cookie handshake from its homepage before its JSON endpoints
    answer — and it must be made with `requests`, not urllib.

    Verified #81: identical headers, urllib gets a hard 403 and requests gets
    200. NSE fingerprints the TLS handshake (Akamai), so the HTTP headers are
    not the gate — the client stack is. Returns a Session or None.
    """
    try:
        import requests
    except Exception:
        return None
    ses = requests.Session()
    ses.headers.update({
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        ses.get(NSE_HOME, timeout=15)
        return ses
    except Exception:
        return None


def _nse_json(ses, path: str):
    """GET an NSE JSON endpoint on a primed session. Raises on failure."""
    r = ses.get(f"{NSE_HOME}{path}", timeout=15,
                headers={"Accept": "*/*", "Referer": NSE_HOME + "/"})
    r.raise_for_status()
    return r.json()


def fetch_nse(symbol: str, opener=None) -> dict:
    """Promoter pledge % and latest-results date — the India-specific red
    flags. Pledged promoter shares are one of the strongest single warnings
    for an equity you might be assigned."""
    import json as _j
    op = opener or _nse_session()
    if op is None:
        return {"error": "NSE unreachable (403/handshake failed)"}
    out = {}
    try:
        d = _nse_json(op, f"/api/quote-equity?symbol={symbol}")
        info = d.get("info") or {}
        meta = d.get("metadata") or {}
        pi = d.get("priceInfo") or {}
        out["industry"] = info.get("industry") or meta.get("industry")
        out["listed_date"] = meta.get("listingDate")
        out["pe"] = (d.get("metadata") or {}).get("pdSymbolPe")
        wk = pi.get("weekHighLow") or {}
        out["w52_high"] = wk.get("max")
        out["w52_low"] = wk.get("min")
    except Exception as e:
        out["quote_error"] = f"{type(e).__name__}"
    try:
        r = op.open(f"{NSE_HOME}/api/top-corp-info?symbol={symbol}&market=equities", timeout=12)
        d = _j.loads(r.read())
        sh = ((d.get("shareholdings") or {}).get("data") or [])
        for row in sh:
            k = str(row.get("category", "")).lower()
            if "pledge" in k or "encumber" in k:
                out["promoter_pledge_pct"] = row.get("value")
        ann = (d.get("corporate") or {}).get("announcements") or []
        out["recent_announcements"] = [a.get("desc") for a in ann[:3] if a.get("desc")]
    except Exception as e:
        out["corp_error"] = f"{type(e).__name__}"
    return out


def fetch_yahoo(symbol: str) -> dict:
    """Ratios via Yahoo's quoteSummary (what yfinance wraps). Uses .NS."""
    import json as _j, urllib.request
    url = (f"{YF}/v10/finance/quoteSummary/{symbol}.NS"
           "?modules=defaultKeyStatistics,financialData,summaryDetail")
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA})
    try:
        d = _j.loads(urllib.request.urlopen(req, timeout=12).read())
    except Exception as e:
        return {"error": f"Yahoo {type(e).__name__}"}
    try:
        res = (d.get("quoteSummary") or {}).get("result") or []
        if not res:
            return {"error": "Yahoo: empty result"}
        res = res[0]
        fd, ks, sd = (res.get("financialData") or {}, res.get("defaultKeyStatistics") or {},
                      res.get("summaryDetail") or {})
        g = lambda o, k: (o.get(k) or {}).get("raw") if isinstance(o.get(k), dict) else o.get(k)
        return {k: v for k, v in {
            "trailing_pe": g(sd, "trailingPE"), "forward_pe": g(sd, "forwardPE"),
            "debt_to_equity": g(fd, "debtToEquity"),
            "roe": g(fd, "returnOnEquity"),
            "profit_margin": g(fd, "profitMargins"),
            "revenue_growth": g(fd, "revenueGrowth"),
            "earnings_growth": g(fd, "earningsGrowth"),
            "current_ratio": g(fd, "currentRatio"),
            "free_cashflow": g(fd, "freeCashflow"),
            "recommendation": fd.get("recommendationKey"),
        }.items() if v is not None}
    except Exception as e:
        return {"error": f"Yahoo parse {type(e).__name__}"}


# --- Market-wide surveillance feeds (#81) ---------------------------------
# These beat the per-symbol sources on every axis: THREE fetches cover the
# whole market (vs ~200 Yahoo calls that 429), they are the exchange's own
# published opinion that a name is troubled, and they are the single most
# direct answer to "don't put me into stocks with big problems".
NSE_ARCHIVES = "https://nsearchives.nseindia.com"
_SURV_TTL_MIN = 180


def fetch_surveillance() -> dict:
    """NSE ASM + GSM surveillance stages and today's F&O ban list.

    ASM = Additional Surveillance Measure (price/volume abnormality).
    GSM = Graded Surveillance Measure — the severe one; its codes include IBC
    (insolvency) admissions. A name under GSM is one you do not want to be
    assigned. Ban = no fresh F&O positions allowed today at all.
    """
    import json as _j
    out = {"asm": {}, "gsm": {}, "ban": [], "errors": []}
    ses = _nse_session()
    if ses is not None:
        for key, path in (("asm", "/api/reportASM"), ("gsm", "/api/reportGSM")):
            try:
                d = _nse_json(ses, path)
            except Exception as e:
                out["errors"].append(f"{key}: {type(e).__name__}")
                continue
            # ASM nests under longterm/shortterm; GSM is a bare list
            groups = []
            if isinstance(d, dict):
                for gk, gv in d.items():
                    if isinstance(gv, dict) and isinstance(gv.get("data"), list):
                        groups.append((gk, gv["data"]))
            elif isinstance(d, list):
                groups.append(("", d))
            for gname, items in groups:
                for it in items:
                    sym = (it or {}).get("symbol")
                    if not sym:
                        continue
                    stage = (it.get("asmSurvIndicator") or it.get("gsmStage")
                             or it.get("survDesc") or "listed")
                    label = f"{gname} {stage}".strip() if gname else str(stage)
                    out[key][sym] = label
    else:
        out["errors"].append("nse: handshake failed")
    try:                                    # plain CSV, no cookie needed
        import urllib.request
        req = urllib.request.Request(f"{NSE_ARCHIVES}/content/fo/fo_secban.csv",
                                     headers={"User-Agent": _BROWSER_UA})
        txt = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        for line in txt.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2 and parts[0].isdigit():
                out["ban"].append(parts[1])
    except Exception as e:
        out["errors"].append(f"ban: {type(e).__name__}")
    return out


def fetch_screener(symbol: str) -> dict:
    """screener.in top-ratio box — reachable where Yahoo 429s and NSE 403s.
    Returns {} on any failure; callers must treat empty as UNKNOWN."""
    import re as _re, urllib.request
    out = {}
    for suffix in ("consolidated/", ""):
        try:
            req = urllib.request.Request(
                f"https://www.screener.in/company/{symbol}/{suffix}",
                headers={"User-Agent": _BROWSER_UA})
            h = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
        except Exception:
            continue
        pairs = _re.findall(
            r'<li[^>]*>\s*<span class="name"[^>]*>\s*([^<]+?)\s*</span>\s*'
            r'<span class="nowrap value">(.*?)</span>', h, _re.S)
        for name, val in pairs:
            v = _re.sub(r"<[^>]+>", " ", val)
            v = v.replace("\u20b9", "").replace(",", "").replace("%", "").strip()
            v = " ".join(v.split())
            key = "scr_" + name.strip().lower().replace(" ", "_").replace("/", "_")
            num = _re.match(r"^-?\d+(\.\d+)?", v)
            if num:
                out[key] = float(num.group())        # blanks stay absent, not ""
        if out:
            out["_source"] = "screener.in"
            break
    return out


def surveillance_flags(sym: str, surv: dict) -> list:
    """Exchange-published warnings for one symbol. Empty list = not listed on
    any surveillance feed, which is meaningful ONLY if the fetch succeeded."""
    if not surv:
        return []
    out = []
    g = (surv.get("gsm") or {}).get(sym)
    if g:
        out.append(f"NSE GSM ({g})" + (" — IBC/insolvency" if "IBC" in str(g).upper() else ""))
    a = (surv.get("asm") or {}).get(sym)
    if a:
        out.append(f"NSE ASM ({a})")
    if sym in (surv.get("ban") or []):
        out.append("in F&O ban today — no fresh positions")
    return out


def fundamental_flags(f: dict) -> list:
    """Turn whatever fundamentals we actually got into 'big problem' warnings.
    Silent when a field is missing — never invents a clean bill of health."""
    if not f:
        return []
    out = []
    p = f.get("promoter_pledge_pct")
    try:
        p = float(str(p).replace("%", "")) if p is not None else None
    except Exception:
        p = None
    if p is not None and p > 0:
        out.append(f"promoter pledge {p:.0f}%" + (" — HIGH" if p >= 25 else ""))
    de = f.get("debt_to_equity")
    if isinstance(de, (int, float)) and de > 150:
        out.append(f"debt/equity {de:.0f}")
    roe = f.get("roe")
    if isinstance(roe, (int, float)) and roe < 0:
        out.append(f"negative ROE {roe*100:.0f}%")
    pm = f.get("profit_margin")
    if isinstance(pm, (int, float)) and pm < 0:
        out.append(f"negative margin {pm*100:.0f}%")
    eg = f.get("earnings_growth")
    if isinstance(eg, (int, float)) and eg < -0.25:
        out.append(f"earnings {eg*100:.0f}%")
    rg = f.get("revenue_growth")
    if isinstance(rg, (int, float)) and rg < -0.15:
        out.append(f"revenue {rg*100:.0f}%")
    # screener.in keys are `scr_*` and are PERCENTS, unlike Yahoo's fractions
    sroe = f.get("scr_roe")
    if isinstance(sroe, (int, float)) and sroe < 0:
        out.append(f"ROE {sroe:.0f}%")
    roce = f.get("scr_roce")
    if isinstance(roce, (int, float)) and roce < 0:
        out.append(f"ROCE {roce:.1f}% — burning capital")
    return out


def get_fundamentals(conn, symbol: str, force=False) -> dict:
    """Cached, best-effort. Always returns a dict; `available` says whether
    anything real came back, so the UI can be honest instead of showing blanks
    that look like a pass."""
    row = conn.execute("SELECT fetched_ts, data, error FROM csp_fundamentals "
                       "WHERE symbol=?", (symbol,)).fetchone()
    if row and not force:
        try:
            age = (datetime.now(IST) - datetime.fromisoformat(row[0])).total_seconds() / 3600
            if age < FUND_TTL_HOURS:
                d = json.loads(row[1] or "{}")
                return {"available": bool(d), "data": d, "error": row[2],
                        "fetched": row[0], "cached": True}
        except Exception:
            pass
    nse = fetch_nse(symbol)
    yh = fetch_yahoo(symbol)
    scr = fetch_screener(symbol)          # reachable where NSE 403s and YF 429s
    merged = {k: v for k, v in {**yh, **nse, **scr}.items() if k not in ("error",)}
    errs = [x.get("error") for x in (nse, yh) if x.get("error")]
    if not scr:
        errs.append("screener: no data")
    conn.execute("INSERT OR REPLACE INTO csp_fundamentals (symbol,fetched_ts,source,data,error)"
                 " VALUES (?,?,?,?,?)",
                 (symbol, datetime.now(IST).isoformat(), "nse+yahoo+screener",
                  json.dumps(merged), "; ".join(errs) if errs else None))
    conn.commit()
    return {"available": bool(merged), "data": merged,
            "error": "; ".join(errs) if errs else None,
            "fetched": datetime.now(IST).isoformat(), "cached": False}


def enrich_with_fundamentals(conn, rows: list, top_n: int = 25) -> dict:
    """Per-symbol sources are fetched only for the top-ranked ideas (they rate-
    limit, and there is no point pulling 200 names you will never trade).

    The exchange surveillance feeds are different: THREE fetches cover the
    whole market, so every row gets them. They are also the strongest signal
    of the thing the user actually cares about — a name with real problems.
    """
    surv = fetch_surveillance()
    surv_ok = not surv.get("errors")
    n_surv = 0
    for r in rows:
        sf = surveillance_flags(r["symbol"], surv)
        r["surveillance"] = sf
        r["surveillance_checked"] = surv_ok
        if sf:
            r["risk_flags"] = list(r.get("risk_flags") or []) + sf
            # GSM/ban are disqualifying, not a nudge
            hard = any(("GSM" in x or "ban" in x) for x in sf)
            r["score"] = max(0.0, round(r["score"] - (40 if hard else 12), 1))
            n_surv += 1

    ok = fail = 0
    for r in rows[:top_n]:
        f = get_fundamentals(conn, r["symbol"])
        r["fundamentals"] = f
        if f.get("available"):
            ff = fundamental_flags(f["data"])
            if ff:
                r["risk_flags"] = list(r.get("risk_flags") or []) + ff
                r["score"] = max(0.0, round(r["score"] - 8 * len(ff), 1))
            ok += 1
        else:
            fail += 1
    rows.sort(key=lambda r: r["score"], reverse=True)   # penalties change order
    return {"enriched": ok, "unavailable": fail,
            "surveillance_ok": surv_ok, "surveillance_errors": surv.get("errors"),
            "flagged_by_surveillance": n_surv,
            "asm_listed": len(surv.get("asm") or {}),
            "gsm_listed": len(surv.get("gsm") or {}),
            "ban_listed": len(surv.get("ban") or [])}

"""Backtest of the directional read (#69): Hilega Milega + SRT, on daily data.

The claims under test are the author's, implemented verbatim in compute.py:
  * RSI above its WMA-21 red line = buy side, below = sell side
  * a FRESH BUY needs RSI > 55 and a completed V-turn; trail/exit = 20-SMA
  * RSI >= 86 = book profit (resistance)
  * a buy leg that flips straight back to sell => the prior swing low breaks
    ("8 out of 10")
  * SRT (close / 124-day average) <= ~0.85 = positional buy zone, >= 1.24
    stretched; weekly SRT > 1.24 = distribution

WALK-FORWARD THROUGH THE PRODUCTION FUNCTIONS. For every day, the frame the
dashboard would have had — the trailing 1,100 calendar days ending that day,
exactly what direction_block() fetches — goes through compute.srt_read and
compute.hilega_milega unchanged, including the SRT-dependent switch of the RSI
period to 7. Nothing is re-implemented, so nothing can drift from production.
The signal is taken on the day's close; outcomes are measured from that close.

EVERY CLAIM GETS A MATCHED CONTROL (validate-against-matched-control):
  state          next-day return in buy vs sell state (non-overlapping, so a
                 plain t-test is honest), plus 5/10/20-day descriptives
  fresh buy      each trade (enter at the close, exit at the first close below
                 the 20-SMA) vs RANDOM entries held for the same number of days
  RSI >= 86      forward returns after the first close at/above 86 vs all days
  old-low break  break rate vs every day's own 20-day swing low, re-weighted to
                 the events' distance-from-level (a near level breaks more
                 often whatever the indicator says)
  SRT zones      forward 60/120/250-day returns after entering each zone; few,
                 overlapping episodes, so reported as counts and averages only

Daily index candles come from Fyers' history API into the archive (res '1D');
the archive already held them from late 2021 and this extends them back as far
as Fyers serves.
"""
from __future__ import annotations

import json
import math
import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

import db

IST = timezone(timedelta(hours=5, minutes=30))
LOOKBACK_CAL_DAYS = 1100          # direction_block's fetch window
FETCH_FROM = date(2005, 1, 1)
HORIZONS = (5, 10, 20)
SRT_HORIZONS = (60, 120, 250)
BREAK_HORIZONS = (10, 20, 40)
ERAS = (("2008-2014", "2008-01-01", "2015-01-01"), ("2015-2020", "2015-01-01", "2021-01-01"),
        ("2021-now", "2021-01-01", "2100-01-01"))


def result_path(underlying: str):
    return db.DATA_DIR / f"history_direction_{underlying}.json"


# ------------------------------------------------------------------ data --

def fetch_daily(arch, underlying: str) -> dict:
    """Extend the archive's daily candles back to FETCH_FROM (about one call
    per year; fails soft so an expired login still grades what is stored)."""
    import fyers_hist as fh
    try:
        cli = fh.Client(fh.RateLimiter(arch))
        n = fh.ensure_index_daily(cli, arch, underlying, FETCH_FROM,
                                  datetime.now(IST).date())
        return {"fetched_rows": n}
    except Exception as e:
        return {"fetch_error": f"{type(e).__name__}: {e}"}


def load_daily(arch, underlying: str) -> pd.DataFrame:
    rows = arch.execute("SELECT ts, o, h, l, c FROM index_bar WHERE underlying=? AND res='1D' "
                        "ORDER BY ts", (underlying,)).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    if df.empty:
        return df
    df["date"] = pd.to_datetime([datetime.fromtimestamp(int(t), IST).date() for t in df["ts"]])
    df = df.drop(columns="ts").drop_duplicates("date", keep="last").set_index("date")
    return df.dropna(subset=["close"])


# ---------------------------------------------------------- walk forward --

def walk(df: pd.DataFrame, since: Optional[str] = None) -> pd.DataFrame:
    """One production read per day, on only what was known at that close."""
    import compute
    recs = []
    idx = df.index
    start = idx.searchsorted(pd.Timestamp(since)) if since else 0
    for i in range(start, len(df)):
        day = idx[i]
        d = df.loc[day - pd.Timedelta(days=LOOKBACK_CAL_DAYS):day]
        if len(d) < compute.SRT_PERIOD:
            continue
        srt = compute.srt_read(d["close"])
        v = srt.get("srt")
        period = compute.HM_FAST_RSI if (v is not None and v <= 0.85) else compute.HM_RSI_PERIOD
        hm = compute.hilega_milega(d, period, series_bars=1)
        if not hm.get("ok"):
            continue
        w = d.resample("W-FRI").agg({"open": "first", "high": "max",
                                     "low": "min", "close": "last"}).dropna()
        hw = compute.hilega_milega(w, period, series_bars=1)
        sw = compute.srt_read(w["close"], weekly=True)
        olb = hm.get("old_low_break") or {}
        recs.append({
            "date": day, "close": float(df["close"].iloc[i]), "period": period,
            "state": hm["state"], "verdict": hm["verdict"], "rsi": hm["rsi"],
            "v_turn": hm["v_turn"], "book": hm["book_profit"],
            "sma20": hm.get("sma20"), "olb_level": olb.get("level"),
            "srt": v, "srt_zone": srt.get("zone"),
            "w_state": hw.get("state") if hw.get("ok") else None,
            "srt_w": sw.get("srt") if sw.get("ok") else None,
            "srt_w_zone": sw.get("zone") if sw.get("ok") else None,
        })
    return pd.DataFrame(recs).set_index("date") if recs else pd.DataFrame()


# ------------------------------------------------------------- grading --

def _t(xs: list):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return round(m / (sd / math.sqrt(n)), 2) if sd else None


def _welch(a: list, b: list):
    if len(a) < 3 or len(b) < 3:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    se = math.sqrt(va / len(a) + vb / len(b))
    return round((ma - mb) / se, 2) if se else None


def _pct(x):
    return round(x * 100, 3) if x is not None else None


def _summ(xs: list) -> dict:
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "mean_pct": _pct(sum(xs) / len(xs)),
            "up_pct": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1)}


def _verdict(z, n, min_n=20):
    if n < min_n:
        return "insufficient sample"
    if z is None:
        return "no baseline"
    return "beats baseline" if z > 1.96 else ("WORSE than baseline" if z < -1.96
                                              else "no evidence")


def _fwd(close: pd.Series, k: int) -> pd.Series:
    return close.shift(-k) / close - 1


def _firsts(mask: pd.Series) -> pd.Series:
    """True on the first day of each run where mask holds."""
    m = mask.fillna(False).astype(bool)
    return m & ~m.shift(1, fill_value=False)


def grade_state(r: pd.DataFrame, close: pd.Series) -> dict:
    out = {}
    nxt = _fwd(close, 1).reindex(r.index)
    for col, label in (("state", "daily"),):
        buy = [x for x, s in zip(nxt, r[col]) if s == "buy" and pd.notna(x)]
        sell = [x for x, s in zip(nxt, r[col]) if s == "sell" and pd.notna(x)]
        z = _welch(buy, sell)
        out[label] = {"next_day_buy_state": _summ(buy), "next_day_sell_state": _summ(sell),
                      "welch_t_buy_minus_sell": z,
                      "verdict": _verdict(z, min(len(buy), len(sell)))}
        for k in HORIZONS:
            f = _fwd(close, k).reindex(r.index)
            out[label][f"{k}d"] = {
                "buy": _summ([x for x, s in zip(f, r[col]) if s == "buy" and pd.notna(x)]),
                "sell": _summ([x for x, s in zip(f, r[col]) if s == "sell" and pd.notna(x)]),
                "note": "overlapping windows — descriptive only"}
    # weekly state -> the next week's return, non-overlapping (Fridays)
    # last session of each week (a Friday holiday must not merge two weeks)
    wk = r.groupby(r.index.to_period("W-FRI")).tail(1)
    wc = close.reindex(wk.index)
    nw = (wc.shift(-1) / wc - 1)
    b = [x for x, s in zip(nw, wk["w_state"]) if s == "buy" and pd.notna(x)]
    s_ = [x for x, s in zip(nw, wk["w_state"]) if s == "sell" and pd.notna(x)]
    z = _welch(b, s_)
    out["weekly"] = {"next_week_buy_state": _summ(b), "next_week_sell_state": _summ(s_),
                     "welch_t_buy_minus_sell": z, "verdict": _verdict(z, min(len(b), len(s_)))}
    return out


def grade_fresh_buy(r: pd.DataFrame, df: pd.DataFrame, seed: int = 7, draws: int = 2000) -> dict:
    """Enter on the close of the first 'buy + V-turn' day; exit on the first
    close below the 20-SMA (his trail). Control: random entry days held for
    the SAME number of days, drawn from the same span."""
    close = df["close"]
    sma = close.rolling(20).mean()
    sig = _firsts((r["verdict"] == "buy") & r["v_turn"])
    pos = {d: i for i, d in enumerate(close.index)}
    trades = []
    for d in sig[sig].index:
        i = pos[d]
        j = i + 1
        while j < len(close) and not (close.iloc[j] < sma.iloc[j]):
            j += 1
        if j >= len(close):
            break                                     # still open at the end
        trades.append({"entry": str(d.date()), "exit": str(close.index[j].date()),
                       "days": j - i, "ret": float(close.iloc[j] / close.iloc[i] - 1)})
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    # control entries come from the same span the signals were scanned over
    lo, hi = pos[r.index[0]], pos[r.index[-1]]
    trades = [t for t in trades if hi - t["days"] >= lo]
    if not trades:
        return {"n": 0}
    rets = [t["ret"] for t in trades]
    rnd = random.Random(seed)
    means, wins = [], []
    for _ in range(draws):
        rr = []
        for t in trades:
            i = rnd.randint(lo, hi - t["days"])
            rr.append(float(close.iloc[i + t["days"]] / close.iloc[i] - 1))
        means.append(sum(rr) / len(rr))
        wins.append(sum(1 for x in rr if x > 0) / len(rr))
    m = sum(rets) / len(rets)
    w = sum(1 for x in rets if x > 0) / len(rets)
    p_mean = sum(1 for x in means if x >= m) / draws
    return {"n": len(trades), "mean_pct": _pct(m), "win_pct": round(w * 100, 1),
            "avg_days": round(sum(t["days"] for t in trades) / len(trades), 1),
            "random_same_hold_mean_pct": _pct(sum(means) / draws),
            "random_same_hold_win_pct": round(sum(wins) / draws * 100, 1),
            "p_random_mean_ge": round(p_mean, 4),
            "verdict": ("insufficient sample" if len(trades) < 20 else
                        "beats random entry" if p_mean < 0.025 else
                        "WORSE than random entry" if p_mean > 0.975 else "no evidence"),
            "last_trades": trades[-5:]}


def grade_book(r: pd.DataFrame, close: pd.Series) -> dict:
    ev = _firsts(r["rsi"] >= 86)
    out = {"events": int(ev.sum())}
    for k in HORIZONS:
        f = _fwd(close, k).reindex(r.index)
        e = [x for x, flag in zip(f, ev) if flag and pd.notna(x)]
        a = [x for x in f if pd.notna(x)]
        out[f"{k}d"] = {"after_86": _summ(e), "all_days": _summ(a),
                        "claim": "profit-book line: expect BELOW all-day returns"}
    return out


def _bucket(dist: float) -> str:
    for edge in (1, 2, 4, 8):
        if dist < edge:
            return f"<{edge}%"
    return ">=8%"


def grade_old_low(r: pd.DataFrame, df: pd.DataFrame) -> dict:
    """'Old low breaks, and badly' — 8 out of 10, he says. Per horizon: the
    events' break rate vs every day's own 20-bar swing low, matched on how far
    the close sat above the level."""
    low, close = df["low"], df["close"]
    pos = {d: i for i, d in enumerate(close.index)}
    ev = r[_firsts(r["olb_level"].notna()) & r["olb_level"].notna()]
    out = {"events": len(ev), "claim": "8/10 break"}
    swing = low.rolling(20).min().shift(1)            # control: the 20 bars before
    for h in BREAK_HORIZONS:
        fut_min = low[::-1].rolling(h, min_periods=h).min()[::-1].shift(-1)
        hits, buckets = [], {}
        for d, row in ev.iterrows():
            i = pos[d]
            if pd.isna(fut_min.iloc[i]) or row["olb_level"] >= close.iloc[i]:
                continue
            dist = (close.iloc[i] / row["olb_level"] - 1) * 100
            hits.append(bool(fut_min.iloc[i] < row["olb_level"]))
            buckets[_bucket(dist)] = buckets.get(_bucket(dist), 0) + 1
        base = {}
        for i in range(pos[r.index[0]], len(close)):
            lv = swing.iloc[i]
            if pd.isna(lv) or pd.isna(fut_min.iloc[i]) or lv >= close.iloc[i]:
                continue
            b = _bucket((close.iloc[i] / lv - 1) * 100)
            x = base.setdefault(b, [0, 0])
            x[0] += int(fut_min.iloc[i] < lv)
            x[1] += 1
        tot = sum(buckets.values())
        matched = (float(sum(buckets[b] / tot * (base[b][0] / base[b][1])
                             for b in buckets if b in base and base[b][1])) if tot else None)
        unmatched = (sum(v[0] for v in base.values()) / sum(v[1] for v in base.values())
                     if base else None)
        n = len(hits)
        k = int(sum(hits))
        z = (round((k / n - matched) / math.sqrt(matched * (1 - matched) / n), 2)
             if n and matched and 0 < matched < 1 else None)
        out[f"{h}d"] = {"n": n, "broke": k, "pct": round(k / n * 100, 1) if n else None,
                        "baseline_matched_pct": round(matched * 100, 1) if matched else None,
                        "baseline_all_pct": round(unmatched * 100, 1) if unmatched else None,
                        "z": z, "verdict": _verdict(z, n, min_n=15)}
    return out


def grade_srt(r: pd.DataFrame, close: pd.Series) -> dict:
    out = {}
    zones = {"positional_buy (daily <=0.87)": r["srt_zone"] == "positional_buy",
             "generational_buy (daily <=0.60)": r["srt_zone"] == "generational_buy",
             "stretched (daily >=1.24)": r["srt_zone"] == "stretched",
             "distribution (weekly >1.24)": r["srt_w_zone"] == "distribution"}
    all_f = {k: [x for x in _fwd(close, k).reindex(r.index) if pd.notna(x)] for k in SRT_HORIZONS}
    for name, mask in zones.items():
        # an episode starts after >=20 sessions outside the zone
        m = mask.fillna(False).astype(bool)
        outside = (~m).astype(int).groupby(m.cumsum()).cumsum()
        starts = m & (outside.shift(1, fill_value=99) >= 20)
        days_in = int(m.sum())
        blk = {"days_in_zone": days_in, "episodes": int(starts.sum()),
               "episode_dates": [str(d.date()) for d in starts[starts].index][:12]}
        for k in SRT_HORIZONS:
            f = _fwd(close, k).reindex(r.index)
            blk[f"{k}d"] = {"after_entry": _summ([x for x, s in zip(f, starts) if s and pd.notna(x)]),
                            "any_day_in_zone": _summ([x for x, s in zip(f, m) if s and pd.notna(x)]),
                            "all_days": _summ(all_f[k])}
        out[name] = blk
    out["note"] = ("few, overlapping episodes — descriptive only; no significance is claimed "
                   "and none should be read in")
    return out


def grade(r: pd.DataFrame, df: pd.DataFrame) -> dict:
    close = df["close"]
    return {"span": [str(r.index[0].date()), str(r.index[-1].date())], "sessions": len(r),
            "state": grade_state(r, close), "fresh_buy": grade_fresh_buy(r, df),
            "book_86": grade_book(r, close), "old_low_break": grade_old_low(r, df),
            "srt": grade_srt(r, close),
            "rsi7_days_pct": round((r["period"] == 7).mean() * 100, 1)}


# ------------------------------------------------------------------- run --

def _plain(v):
    """numpy scalars / NaN -> JSON-native values."""
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def run(underlying: str = "NIFTY", arch_path: Optional[str] = None, fetch: bool = True) -> dict:
    import fyers_hist
    arch = fyers_hist.connect(arch_path) if arch_path else fyers_hist.connect()
    meta = fetch_daily(arch, underlying) if fetch else {}
    df = load_daily(arch, underlying)
    if len(df) < 400:
        res = {"ok": False, "underlying": underlying,
               "reason": f"only {len(df)} daily candles stored", **meta}
    else:
        r = walk(df)
        res = {"ok": True, "underlying": underlying, **meta,
               "candles": [str(df.index[0].date()), str(df.index[-1].date()), len(df)],
               "latest": {k: _plain(v) for k, v in r.iloc[-1].to_dict().items()},
               "pooled": grade(r, df), "by_era": {}}
        for name, a, b in ERAS:
            sl = r[(r.index >= a) & (r.index < b)]
            if len(sl) > 250:
                res["by_era"][name] = grade(sl, df)
    res["computed_at"] = datetime.now(IST).isoformat(timespec="seconds")
    result_path(underlying).write_text(json.dumps(res, default=str))
    return res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="NIFTY")
    ap.add_argument("--arch")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    out = run(a.underlying, a.arch, fetch=not a.no_fetch)
    print(json.dumps({k: out.get(k) for k in ("ok", "underlying", "candles", "fetched_rows",
                                               "fetch_error", "reason", "computed_at")}))

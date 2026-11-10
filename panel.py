"""Every indicator the app computes, for one NIFTY session, in one shape.

The nightly forecast (forecast.py) and the day-by-day replay of the past
(forecast_replay.py) both read a session through build(): the same code turns
the same kind of inputs into the same numbers, so whatever is learnt replaying
2022 is learnt on exactly the readings shown tonight. Only the sources differ:

  index daily bars    index_daily (Kite official closes; Fyers 1D fills gaps)
  futures CMF / VWMA  live: Kite's nearest future (compute.py's own fetch)
                      past: the Fyers archive's monthly FUT minute bars
  session reads       history.db when it has the day (the production
                      functions replayed, #93), else oi_chain.db, computed
                      now with those same functions
  participants        participant_oi, cut at the session (no later files)

The panel is RELATIVE: percent of spot, ranks, signs, states. That lets the
replay show it with no dates and no index levels (blinded); the absolute
numbers live under "levels" and are shown only in the live briefing.

states() turns a panel into discrete readings ("above SMA200", "tilt +10..+40")
and history_stats() counts what NIFTY did after past sessions with the same
reading, against the base rate of those same sessions. Point-in-time by
construction: the caller passes only sessions whose outcome was already known.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

IST = timezone(timedelta(hours=5, minutes=30))

# Indicator families, in the order the forecaster must read them. The last two
# are not panel sections: they come from the history stats and the record.
FAMILIES = {
    "price_trend": "Trend and structure: moves, moving-average distances, 52-week position",
    "momentum_reversal": "Stretch: streaks, ATR-sized moves, realised vol, distance from recent highs/lows",
    "strategy_signals": "The saved strategies' indicators: BB-21 midline (Golden Goose / GG LEAPS), "
                        "EMA-53 (Nidhi Kalash), CMF-21 (Panther), 2h VWMA-21 (Ocean Treasure)",
    "hm_srt": "Hilega-Milega RSI state daily and weekly, SRT daily and weekly",
    "volatility": "India VIX, ATM implied vol, expected move, implied vs realised",
    "writer_pressure": "WCash and its bull/bear tilt, premium-ladder skew and straddle",
    "options_positioning": "OI builds that held into the close, max pain",
    "oi_flow": "Option fund-flow score markers",
    "llt": "Large-lot NIFTY futures prints",
    "participants_fii": "FII index futures and options positioning (NSE participant file)",
    "participants_other": "Client / Pro / DII positioning",
    "banknifty_relative": "BANKNIFTY's moves and reads against NIFTY",
    "expiry_calendar": "Weekday, days to weekly and monthly expiry, expiry day",
    "history_stats": "What NIFTY did after past sessions with the same readings",
    "track_record": "Your own graded record and lessons",
}
SECTIONS = [k for k in FAMILIES if k not in ("history_stats", "track_record")]
SYMS = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "VIX": "NSE:INDIA VIX"}
TOKENS = {"NIFTY": 256265, "BANKNIFTY": 260105, "VIX": 264969}
CLASSES = ("FII", "DII", "Pro", "Client")
MIN_STATE_N = 30                 # history stats below this are listed, not judged


def _r(x, n=2):
    try:
        if x is None or (isinstance(x, float) and not math.isfinite(x)) or pd.isna(x):
            return None
        return round(float(x), n)
    except Exception:
        return None


def _pct(a, b, n=2):
    """% change from a to b."""
    try:
        return round((float(b) / float(a) - 1) * 100, n) if a else None
    except Exception:
        return None


# ------------------------------------------------------------ daily bars --

def refresh_index_daily(kite, conn, since: date = date(2015, 1, 1)) -> dict:
    """Kite's official daily bars for NIFTY, BANKNIFTY and INDIA VIX, from
    `since`, in <=1900-day chunks (Kite caps a daily request at 2000 days)."""
    out = {}
    end = datetime.now(IST).date()
    for sym, ks in SYMS.items():
        tok = TOKENS[sym]
        try:
            tok = ((kite.ltp([ks]) or {}).get(ks) or {}).get("instrument_token") or tok
        except Exception:
            pass
        n, frm = 0, since
        while frm <= end:
            to = min(end, frm + timedelta(days=1900))
            try:
                rows = kite.historical_data(tok, frm.strftime("%Y-%m-%d 00:00:00"),
                                            to.strftime("%Y-%m-%d 23:59:59"), "day") or []
            except Exception as e:
                out[sym + "_error"] = f"{type(e).__name__}: {e}"
                rows = []
            conn.executemany(
                "INSERT OR REPLACE INTO index_daily (sym, d, o, h, l, c, source) VALUES "
                "(?,?,?,?,?,?,'kite')",
                [(sym, str(r["date"])[:10], float(r["open"]), float(r["high"]), float(r["low"]),
                  float(r["close"])) for r in rows])
            n += len(rows)
            frm = to + timedelta(days=1)
        conn.commit()
        out[sym] = n
    return out


def fill_index_daily_from_fyers(conn, arch) -> dict:
    """Days Kite has not given us (no session when the replay was prepared),
    from the Fyers archive's daily candles. Never overwrites a Kite row."""
    out = {}
    for sym in ("NIFTY", "BANKNIFTY"):
        try:
            rows = arch.execute("SELECT ts, o, h, l, c FROM index_bar WHERE underlying=? "
                                "AND res='1D' ORDER BY ts", (sym,)).fetchall()
        except sqlite3.Error as e:
            out[sym] = f"error: {e}"
            continue
        recs = [(sym, datetime.fromtimestamp(int(r[0]), IST).date().isoformat(),
                 r[1], r[2], r[3], r[4]) for r in rows if r[4]]
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO index_daily (sym, d, o, h, l, c, source) "
                         "VALUES (?,?,?,?,?,?,'fyers')", recs)
        conn.commit()
        out[sym] = conn.total_changes - before
    return out


def nifty_closes(conn) -> list:
    """[(d, close)] oldest first — the outcome series for grading and stats."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT d, c FROM index_daily WHERE sym='NIFTY' ORDER BY d")]


# --------------------------------------------------------------- sources --

class _ArchiveFutures:
    """NIFTY monthly futures from the Fyers archive: for a session, the
    nearest contract expiring on/after it (compute.py's rule), aggregated from
    minute bars into the daily and 60-minute bars compute.py fetches live."""

    def __init__(self, arch, underlying: str = "NIFTY"):
        self.arch = arch
        self.contracts = [(r[0], r[1]) for r in arch.execute(
            "SELECT id, expiry FROM contract WHERE underlying=? AND kind='FUT' ORDER BY expiry",
            (underlying,))]
        self._cache = {}

    def _bars(self, cid):
        if cid not in self._cache:
            rows = self.arch.execute("SELECT ts, o, h, l, c, v FROM bar WHERE contract_id=? "
                                     "ORDER BY ts", (cid,)).fetchall()
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            if df.empty:
                self._cache[cid] = (df, df)
                return self._cache[cid]
            t = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
            mins = t.dt.hour * 60 + t.dt.minute
            keep = ((mins >= 555) & (mins <= 929)).to_numpy()  # 09:15-15:29, as Kite bars
            df = df.loc[keep].reset_index(drop=True)
            t = t.loc[keep].reset_index(drop=True)
            mins = mins.loc[keep].reset_index(drop=True)
            df["day"] = t.dt.tz_localize(None).dt.normalize()          # naive IST date
            df["start"] = t.dt.normalize() + pd.to_timedelta((mins - 555) // 60 * 60 + 555,
                                                             unit="m")    # 60m bar, tz-aware
            agg = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                       close=("close", "last"), volume=("volume", "sum"))
            daily = df.groupby("day").agg(**agg)
            h60 = df.groupby("start").agg(**agg)
            self._cache[cid] = (daily, h60)
            if len(self._cache) > 4:                      # keep memory flat
                self._cache.pop(next(iter(self._cache)))
        return self._cache[cid]

    def _contract(self, d: str):
        return next((cid for cid, exp in self.contracts if exp >= d), None)

    def daily(self, d: str):
        cid = self._contract(d)
        if cid is None:
            return None
        daily, _ = self._bars(cid)
        ts = pd.Timestamp(d)
        return daily.loc[ts - pd.Timedelta(days=120):ts]

    def h60(self, d: str):
        cid = self._contract(d)
        if cid is None:
            return None
        _, h60 = self._bars(cid)
        hi = pd.Timestamp(d + " 23:59").tz_localize("Asia/Kolkata")
        return h60.loc[hi - pd.Timedelta(days=45):hi]


class _KiteFutures:
    """Tonight's futures, fetched exactly as compute.py fetches them."""

    def __init__(self, kite, underlying: str = "NIFTY"):
        import compute
        tok, _ = compute._nearest_future_token(kite, underlying)
        self.d = compute._historical(kite, tok, "day", days=120)
        self.h = compute._historical(kite, tok, "60minute", days=45)

    def daily(self, d: str):
        if self.d.empty:
            return None
        idx = self.d.index.tz_localize(None) if self.d.index.tz is not None else self.d.index
        df = self.d.copy()
        df.index = idx.normalize()
        return df.loc[:pd.Timestamp(d)]

    def h60(self, d: str):
        if self.h.empty:
            return None
        hi = pd.Timestamp(d + " 23:59").tz_localize("Asia/Kolkata")
        df = self.h.copy()
        if df.index.tz is None:
            df.index = df.index.tz_localize("Asia/Kolkata")
        return df.loc[:hi]


class Sources:
    """Where a session's inputs come from. `conn` = oi_chain.db (daily bars,
    participants, live session tables); `hist` = history.db; `arch` = the
    Fyers archive (past futures); `kite` = tonight's futures."""

    def __init__(self, conn, hist=None, arch=None, kite=None, underlying="NIFTY"):
        self.conn, self.hist, self.arch = conn, hist, arch
        self._daily = {}
        self._fut = None
        self._fut_err = None
        try:
            if kite is not None:
                self._fut = _KiteFutures(kite, underlying)
            elif arch is not None:
                self._fut = _ArchiveFutures(arch, underlying)
        except Exception as e:
            self._fut_err = f"{type(e).__name__}: {e}"

    def daily(self, sym: str, until: str) -> pd.DataFrame:
        if sym not in self._daily:
            rows = self.conn.execute("SELECT d, o, h, l, c FROM index_daily WHERE sym=? "
                                     "ORDER BY d", (sym,)).fetchall()
            df = pd.DataFrame([tuple(r) for r in rows],
                              columns=["date", "open", "high", "low", "close"])
            df["date"] = pd.to_datetime(df["date"])
            self._daily[sym] = df.set_index("date")
        return self._daily[sym].loc[:pd.Timestamp(until)]

    def fut_daily(self, d):
        return self._fut.daily(d) if self._fut else None

    def fut_60m(self, d):
        return self._fut.h60(d) if self._fut else None

    # ---- session reads
    def session(self, u: str, d: str) -> dict:
        if self.hist is not None:
            try:
                r = self.hist.execute("SELECT * FROM hist_day WHERE underlying=? AND d=?",
                                      (u, d)).fetchone()
            except sqlite3.Error:
                r = None
            if r:
                return self._from_hist(dict(r), u, d)
        return self._from_live(u, d)

    def _from_hist(self, r: dict, u: str, d: str) -> dict:
        out = {"source": "history.db", "expiry": (r.get("expiry") or "")[:10] or None,
               "dte": r.get("dte"),
               "wcash_cr": r.get("wcash_cr"), "wcash_ce_cr": r.get("wcash_ce"),
               "wcash_pe_cr": r.get("wcash_pe"), "tilt_pct": r.get("wcash_tilt"),
               "ladder_skew_pct": r.get("ladder_skew"),
               "ladder_straddle_pct": r.get("ladder_straddle_pct"),
               "em_spot": r.get("em_spot"), "em_iv": r.get("em_iv"),
               "em_one_sigma": r.get("em_one_sigma"), "em_dte": r.get("em_dte"),
               "mp_strike": r.get("mp_strike"), "mp_edge": r.get("mp_edge")}
        out.update(_events(self.hist, u, d))
        return _norm(out)

    def _from_live(self, u: str, d: str) -> dict:
        import compute
        import pulse
        conn = self.conn
        if not conn.execute("SELECT 1 FROM chain_snapshot WHERE underlying=? AND ts>=? AND ts<? "
                            "LIMIT 1", (u, d, d + "~")).fetchone():
            return {"source": None, "missing": "no recorded option chain for this session"}
        out = {"source": "oi_chain.db"}
        try:
            w = compute.wcash_read(conn, u, d)
            if w.get("ok"):
                out.update(wcash_cr=w["close_cr"], wcash_ce_cr=w["ce_cr"], wcash_pe_cr=w["pe_cr"],
                           tilt_pct=w["tilt_pct"])
        except Exception as e:
            out["wcash_error"] = f"{type(e).__name__}: {e}"
        try:
            lad = compute.premium_ladder(conn, u, d)
            if lad.get("ok"):
                out.update(ladder_skew_pct=lad["skew_pct"], ladder_straddle_pct=lad["straddle_pct"])
        except Exception as e:
            out["ladder_error"] = f"{type(e).__name__}: {e}"
        try:
            em = pulse.expected_move(conn, u, d)
            if em.get("ok"):
                out.update(em_spot=em["spot"], em_iv=em["iv"], em_one_sigma=em["one_sigma"],
                           em_dte=em["dte"], expiry=em.get("expiry"), dte=em.get("dte"))
        except Exception as e:
            out["em_error"] = f"{type(e).__name__}: {e}"
        try:
            mp = pulse.max_pain(conn, u, d)
            if mp.get("ok"):
                out.update(mp_strike=mp["max_pain"], mp_edge=int(bool(mp.get("on_band_edge"))))
                out.setdefault("expiry", mp.get("expiry"))
                out.setdefault("dte", mp.get("dte"))
        except Exception as e:
            out["mp_error"] = f"{type(e).__name__}: {e}"
        out.update(_events(conn, u, d))
        return _norm(out)


_INTS = ("dte", "em_dte", "mp_edge")
_FLOATS = ("wcash_cr", "wcash_ce_cr", "wcash_pe_cr", "tilt_pct", "ladder_skew_pct",
           "ladder_straddle_pct", "em_spot", "em_iv", "em_one_sigma", "mp_strike")


def _norm(d: dict) -> dict:
    """One type per field whichever DB it came from (history.db stores REAL)."""
    for k in _INTS:
        if d.get(k) is not None:
            d[k] = int(round(float(d[k])))
    for k in _FLOATS:
        if d.get(k) is not None:
            d[k] = float(d[k])
    for b in d.get("builds") or []:
        b["strike"] = float(b["strike"])
    return d


def _events(ev, u: str, d: str) -> dict:
    """Markers, OI builds and LLT prints for the day, from whichever DB."""
    lo, hi = d, d + "~"
    out = {}
    try:
        out["markers"] = [dict(r) for r in ev.execute(
            "SELECT ts, side, score FROM score_marker_outcomes WHERE underlying=? AND ts>=? "
            "AND ts<? ORDER BY ts", (u, lo, hi))]
    except sqlite3.Error:
        out["markers"] = None
    try:
        out["builds"] = [dict(r) for r in ev.execute(
            "SELECT strike, opt_type, side, bias, held, hits, spot_at_fire FROM oi_builds "
            "WHERE underlying=? AND ts>=? AND ts<?", (u, lo, hi))]
    except sqlite3.Error:
        out["builds"] = None
    if u == "NIFTY":
        try:
            out["llt"] = [dict(r) for r in ev.execute(
                "SELECT classification, lots, confidence, closing_flag FROM llt_prints "
                "WHERE ts>=? AND ts<?", (lo, hi))]
        except sqlite3.Error:
            out["llt"] = None
    return out


# -------------------------------------------------------------- sections --

def _price_trend(df) -> tuple:
    c, o, h, l = df["close"], df["open"], df["high"], df["low"]
    last = float(c.iloc[-1])
    back = lambda k: float(c.iloc[-1 - k]) if len(c) > k else None
    sma = lambda n, end=None: float(c.iloc[:end].tail(n).mean()) if len(c.iloc[:end]) >= n else None
    s20, s50, s200 = sma(20), sma(50), sma(200)
    s20_5 = sma(20, -5)
    yr = df.tail(250)
    hi, lo = float(yr["high"].max()), float(yr["low"].min())
    dh, dl = float(h.iloc[-1]), float(l.iloc[-1])
    out = {"day_pct": _pct(back(1), last), "week_pct": _pct(back(5), last),
           "month_pct": _pct(back(21), last), "quarter_pct": _pct(back(63), last),
           "gap_open_pct": _pct(back(1), float(o.iloc[-1])),
           "open_to_close_pct": _pct(float(o.iloc[-1]), last),
           "close_in_day_range_pct": round((last - dl) / (dh - dl) * 100) if dh > dl else None,
           "vs_sma20_pct": _pct(s20, last), "vs_sma50_pct": _pct(s50, last),
           "vs_sma200_pct": _pct(s200, last),
           "sma20_slope_5d_pct": _pct(s20_5, s20), "sma50_vs_sma200_pct": _pct(s200, s50),
           "pos_in_52w_range_pct": round((last - lo) / (hi - lo) * 100) if hi > lo else None,
           "off_52w_high_pct": _pct(hi, last), "above_52w_low_pct": _pct(lo, last)}
    levels = {"close": last, "sma20": _r(s20), "sma50": _r(s50), "sma200": _r(s200),
              "high_52w": hi, "low_52w": lo}
    return out, levels


def _momentum(df) -> dict:
    c, h, l = df["close"], df["high"], df["low"]
    rets = (c.pct_change() * 100).dropna()
    streak = 0
    for v in reversed(rets.tolist()):
        if v == 0 or (streak and (v > 0) != (streak > 0)):
            break
        streak += 1 if v > 0 else -1
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).dropna()
    atr = float(tr.tail(14).mean()) if len(tr) else None
    last = float(c.iloc[-1])
    lr = (c / c.shift(1)).apply(lambda x: math.log(x) if x and x > 0 else None).dropna().tail(20)
    rv = float(lr.std() * math.sqrt(252) * 100) if len(lr) > 2 else None
    t20 = df.tail(20)
    return {"streak_sessions": streak, "up_sessions_last_10": int((rets.tail(10) > 0).sum()),
            "atr14_pct": _r(atr / last * 100) if atr else None,
            "day_move_in_atr": _r(abs(last - float(c.iloc[-2])) / atr) if atr and len(c) > 1 else None,
            "realized_vol_20d_pct": _r(rv),
            "biggest_up_day_20d_pct": _r(rets.tail(20).max()),
            "biggest_down_day_20d_pct": _r(rets.tail(20).min()),
            "off_20d_high_pct": _pct(float(t20["high"].max()), last),
            "above_20d_low_pct": _pct(float(t20["low"].min()), last)}


def _strategy(df, src, d) -> tuple:
    import compute
    d120 = df.tail(120).copy()
    bb, em = compute.bb21_mid(d120["close"]), compute.ema53(d120["close"])
    cp, cl = float(d120["close"].iloc[-2]), float(d120["close"].iloc[-1])
    f = compute._f
    out = {"golden_goose_bb21": compute._signal_from_cross(cp, cl, f(bb.iloc[-2]), f(bb.iloc[-1])),
           "nidhi_kalash_ema53": compute._signal_from_cross(cp, cl, f(em.iloc[-2]), f(em.iloc[-1])),
           "close_vs_bb21_pct": _pct(f(bb.iloc[-1]), cl), "close_vs_ema53_pct": _pct(f(em.iloc[-1]), cl)}
    levels = {"bb21_mid": _r(f(bb.iloc[-1])), "ema53": _r(f(em.iloc[-1]))}
    fd = src.fut_daily(d)
    if fd is not None and len(fd) >= 2 and fd["volume"].sum() > 0:
        cmf = compute.cmf21(fd["high"], fd["low"], fd["close"], fd["volume"])
        out["panther_cmf21"] = compute._cmf_signal(f(cmf.iloc[-2]), f(cmf.iloc[-1]))
        out["cmf21"] = _r(f(cmf.iloc[-1]), 3)
    else:
        out["panther_cmf21"] = "unknown"
        out["cmf21_missing"] = src._fut_err or "no futures bars with volume for this session"
    h60 = src.fut_60m(d)
    if h60 is not None and not h60.empty and h60["volume"].sum() > 0:
        h2 = compute._resample_to_2h(h60)
        vw = compute.vwma21(h2["close"], h2["volume"])
        if len(h2) >= 2:
            out["ocean_treasure_vwma21_2h"] = compute._signal_from_cross(
                float(h2["close"].iloc[-2]), float(h2["close"].iloc[-1]),
                f(vw.iloc[-2]), f(vw.iloc[-1]))
            out["fut_close_vs_vwma21_pct"] = _pct(f(vw.iloc[-1]), float(h2["close"].iloc[-1]))
            levels["vwma21_2h"] = _r(f(vw.iloc[-1]))
    out.setdefault("ocean_treasure_vwma21_2h", "unknown")
    return out, levels


def _hm(df, d) -> dict:
    import compute
    d11 = df.loc[pd.Timestamp(d) - pd.Timedelta(days=1100):]
    srt = compute.srt_read(d11["close"])
    v = srt.get("srt")
    period = compute.HM_FAST_RSI if (v is not None and v <= 0.85) else compute.HM_RSI_PERIOD
    hm = compute.hilega_milega(d11, period, series_bars=1)
    out = {"rsi_period": period, "srt_daily": v, "srt_daily_zone": srt.get("zone")}
    if hm.get("ok"):
        olb = hm.get("old_low_break") or {}
        out.update(daily_state=hm["state"], daily_verdict=hm["verdict"], daily_rsi=hm["rsi"],
                   daily_rsi_minus_red=hm["gap"], daily_bars_in_state=hm["bars_in_state"],
                   daily_above_buy_level=hm["above_buy_level"], daily_v_turn=hm["v_turn"],
                   daily_book_profit=hm["book_profit"],
                   close_vs_sma20_exit_pct=hm.get("sma20_gap_pct"),
                   old_low_break_level_pct=_pct(float(d11["close"].iloc[-1]), olb.get("level"))
                   if olb.get("level") else None)
    w = d11.resample("W-FRI").agg({"open": "first", "high": "max", "low": "min",
                                   "close": "last"}).dropna()
    hw = compute.hilega_milega(w, period, series_bars=1)
    sw = compute.srt_read(w["close"], weekly=True)
    if hw.get("ok"):
        out.update(weekly_state=hw["state"], weekly_verdict=hw["verdict"], weekly_rsi=hw["rsi"])
    if sw.get("ok"):
        out.update(srt_weekly=sw["srt"], srt_weekly_zone=sw["zone"])
    return out


def _volatility(src, d, ses, rv) -> dict:
    out = {}
    v = src.daily("VIX", d)
    if not v.empty and str(v.index[-1].date()) == d:
        c = v["close"]
        out.update(vix=_r(c.iloc[-1]), vix_day_pct=_pct(c.iloc[-2], c.iloc[-1]) if len(c) > 1 else None,
                   vix_week_pct=_pct(c.iloc[-6], c.iloc[-1]) if len(c) > 5 else None,
                   vix_rank_1y_pct=round(float((c.tail(250) <= c.iloc[-1]).mean()) * 100))
    else:
        out["vix_missing"] = "no India VIX bar for this session"
    if ses.get("em_iv"):
        iv = float(ses["em_iv"])
        out.update(atm_iv_pct=_r(iv, 1), one_day_sigma_pct=_r(iv / math.sqrt(252), 2),
                   iv_minus_realized_pts=_r(iv - rv, 1) if rv else None)
        if ses.get("em_one_sigma") and ses.get("em_spot"):
            out["to_expiry_one_sigma_pct"] = _r(ses["em_one_sigma"] / ses["em_spot"] * 100)
            out["to_expiry_days"] = ses.get("em_dte")
    return out


def _writer(ses) -> dict:
    keys = ("wcash_cr", "wcash_ce_cr", "wcash_pe_cr", "tilt_pct", "ladder_skew_pct",
            "ladder_straddle_pct")
    out = {k: ses.get(k) for k in keys if ses.get(k) is not None}
    if out:
        out["meaning"] = ("tilt > 0: put writers doing better than call writers today (bullish "
                          "pressure); skew > 0: puts gained on calls (bearish); straddle > 0: "
                          "ATM premium rose (vol up)")
    return out


def _positioning(ses, close) -> tuple:
    out, levels = {}, {}
    if ses.get("mp_strike"):
        out["max_pain_vs_spot_pct"] = _pct(close, ses["mp_strike"])
        out["max_pain_on_band_edge"] = bool(ses.get("mp_edge"))
        levels["max_pain"] = ses["mp_strike"]
    b = ses.get("builds")
    if b is not None:
        per = {}
        for x in b:                                  # one entry per contract
            k = (x["strike"], x["opt_type"])
            cur = per.get(k)
            if cur is None or (x.get("hits") or 0) > (cur.get("hits") or 0):
                per[k] = x
        cs = list(per.values())
        bull = [x for x in cs if x.get("bias") == "bullish"]
        bear = [x for x in cs if x.get("bias") == "bearish"]
        held = lambda xs: [x for x in xs if x.get("held") == 1]
        out.update(contracts_with_builds=len(cs), bullish_contracts=len(bull),
                   bearish_contracts=len(bear), bullish_held=len(held(bull)),
                   bearish_held=len(held(bear)),
                   bullish_held_hits=sum(x.get("hits") or 0 for x in held(bull)),
                   bearish_held_hits=sum(x.get("hits") or 0 for x in held(bear)))
        top = sorted(held(cs), key=lambda x: -(x.get("hits") or 0))[:4]
        out["top_held"] = [{"side": x.get("side"), "strike_vs_spot_pct": _pct(close, x["strike"]),
                            "hits": x.get("hits")} for x in top]
        levels["top_held_strikes"] = [{"side": x.get("side"), "strike": x["strike"],
                                       "hits": x.get("hits")} for x in top]
        out["meaning"] = ("a held put-write build is bullish positioning, a held call-write "
                          "bearish; validated as positions PERSISTING, not as price respecting "
                          "the strike")
    return out, levels


def _oi_flow(ses) -> dict:
    m = ses.get("markers")
    if m is None:
        return {}
    pw = [x for x in m if x["side"] == "put_writing"]
    cw = [x for x in m if x["side"] == "call_writing"]
    last = m[-1] if m else None
    return {"put_writing_markers": len(pw), "call_writing_markers": len(cw),
            "net_bullish_markers": len(pw) - len(cw),
            "max_put_writing_score": max((x["score"] for x in pw), default=None),
            "max_call_writing_score": max((x["score"] for x in cw), default=None),
            "last_marker": ({"side": last["side"], "after_14h": last["ts"][11:16] >= "14:00"}
                            if last else None)}


def _llt(ses) -> dict:
    p = ses.get("llt")
    if p is None:
        return {}
    by = {}
    for x in p:
        c = x.get("classification") or "UNCLEAR"
        a = by.setdefault(c, {"prints": 0, "lots": 0})
        a["prints"] += 1
        a["lots"] += int(x.get("lots") or 0)
    lots = lambda k: by.get(k, {}).get("lots", 0)
    return {"prints": len(p), "by_class": by,
            "net_bullish_lots": lots("FRESH LONGS") + lots("SHORT COVERING")
            - lots("FRESH SHORTS") - lots("LONG UNWINDING"),
            "closing_window_prints": sum(1 for x in p if x.get("closing_flag"))}


def _participants(conn, d) -> tuple:
    import participants as P
    lt = P.latest(conn, until=d)
    if not lt.get("ok"):
        return {}, {}
    sess = [r[0] for r in conn.execute(
        "SELECT DISTINCT d FROM participant_oi WHERE d<=? ORDER BY d DESC LIMIT 40", (d,))]
    lag = sess.index(lt["date"]) if lt["date"] in sess else None
    reads = {}
    for w in (5, 10, 20):
        h = P.history(conn, "idx_fut", w, until=d)
        if h.get("ok"):
            reads[w] = {c["name"]: c["read"] for c in h["classes"]}
    ho = P.history(conn, "idx_opt", 5, until=d)
    opt = {c["name"]: c["read"]["net"] for c in ho["classes"]} if ho.get("ok") else {}

    def block(name):
        row = (lt.get("participants") or {}).get(name) or {}
        b = {"idx_fut_net": row.get("fut_idx_net"), "idx_fut_net_chg_1d": row.get("fut_idx_net_chg"),
             "idx_fut_long": row.get("fut_idx_long"), "idx_fut_short": row.get("fut_idx_short"),
             "idx_opt_net_chg_1d": row.get("opt_idx_net_chg")}
        for w, rd in reads.items():
            r = rd.get(name)
            if not r:
                continue
            for side in ("long", "short"):
                s = r[side]
                b[f"{side}_{w}d_pct"] = s.get("pct")
                b[f"{side}_{w}d_expiry_step"] = s.get("expiry_chg")
                if s.get("pct_rank") is not None:
                    b[f"{side}_{w}d_rank_pct"] = s["pct_rank"]
            b[f"net_{w}d_chg"] = r["net"].get("chg")
            if r["net"].get("pct_rank") is not None:
                b[f"net_{w}d_rank_pct"] = r["net"]["pct_rank"]
        if opt.get(name):
            b["idx_opt_net_5avg_chg_5d"] = opt[name].get("chg")
        return b

    fii = block("FII")
    fii.update(file_sessions_old=lag, fii_dii_opposed=lt.get("fii_dii_opposed"),
               note="contract counts; *_expiry_step = part of the change that is the monthly "
                    "expiry roll-off, not a decision; rank = where this move sits in its own "
                    "history (0 = largest fall, 100 = largest rise)")
    return fii, {c: block(c) for c in ("Client", "Pro", "DII")}


def _banknifty(src, d, nifty_pt) -> dict:
    import compute
    bn = src.daily("BANKNIFTY", d)
    if bn.empty or str(bn.index[-1].date()) != d:
        return {}
    pt, _ = _price_trend(bn)
    out = {"bn_day_pct": pt["day_pct"], "bn_week_pct": pt["week_pct"],
           "bn_month_pct": pt["month_pct"], "bn_vs_sma20_pct": pt["vs_sma20_pct"],
           "bn_vs_sma200_pct": pt["vs_sma200_pct"]}
    for k, nk in (("rel_day_pts", "day_pct"), ("rel_week_pts", "week_pct"),
                  ("rel_month_pts", "month_pct")):
        a, b = pt.get(nk), nifty_pt.get(nk)
        out[k] = _r(a - b) if a is not None and b is not None else None
    try:
        d11 = bn.loc[pd.Timestamp(d) - pd.Timedelta(days=1100):]
        hm = compute.hilega_milega(d11, compute.HM_RSI_PERIOD, series_bars=1)
        if hm.get("ok"):
            out.update(bn_hm_state=hm["state"], bn_hm_rsi=hm["rsi"])
    except Exception:
        pass
    ses = src.session("BANKNIFTY", d)
    if ses.get("tilt_pct") is not None:
        out.update(bn_wcash_cr=ses.get("wcash_cr"), bn_tilt_pct=ses.get("tilt_pct"),
                   bn_ladder_skew_pct=ses.get("ladder_skew_pct"))
    return out


def _last_weekday(y: int, m: int, wd: int) -> date:
    import calendar
    x = date(y, m, calendar.monthrange(y, m)[1])
    return x - timedelta(days=(x.weekday() - wd) % 7)


def _calendar(d: str, ses: dict) -> dict:
    x = date.fromisoformat(d)
    def monthly(y, m):
        return _last_weekday(y, m, 1 if (y, m) >= (2025, 9) else 3)   # Tue from Sep 2025
    due = monthly(x.year, x.month)
    if due < x:
        nm = date(x.year + (x.month == 12), x.month % 12 + 1, 1)
        due = monthly(nm.year, nm.month)
    out = {"weekday": x.strftime("%A"), "next_session_after_weekend": x.weekday() == 4,
           "calendar_days_to_monthly_expiry": (due - x).days}
    if ses.get("expiry"):
        dte = (date.fromisoformat(ses["expiry"][:10]) - x).days
        out.update(calendar_days_to_nearest_expiry=dte, is_expiry_day=dte == 0,
                   expiry_tomorrow=dte == 1)
    return out


# ------------------------------------------------------------------ build --

def build(src: Sources, d: str) -> Optional[dict]:
    """Every NIFTY indicator after the `d` close. None if there is no daily
    bar for `d`. Each section fails soft into {"available": false, ...}."""
    nifty = src.daily("NIFTY", d)
    if nifty.empty or str(nifty.index[-1].date()) != d or len(nifty) < 60:
        return None
    close = float(nifty["close"].iloc[-1])
    p, levels = {"d": d}, {}

    def sec(name, fn):
        try:
            v = fn()
            if isinstance(v, tuple):
                v, lv = v
                levels.update(lv or {})
            p[name] = v if v else {"available": False, "why": "no data recorded for this session"}
        except Exception as e:
            p[name] = {"available": False, "why": f"{type(e).__name__}: {e}"[:200]}

    ses = {}
    try:
        ses = src.session("NIFTY", d)
    except Exception as e:
        ses = {"missing": f"{type(e).__name__}: {e}"}
    sec("price_trend", lambda: _price_trend(nifty))
    sec("momentum_reversal", lambda: _momentum(nifty))
    sec("strategy_signals", lambda: _strategy(nifty, src, d))
    sec("hm_srt", lambda: _hm(nifty, d))
    rv = (p.get("momentum_reversal") or {}).get("realized_vol_20d_pct")
    sec("volatility", lambda: _volatility(src, d, ses, rv))
    sec("writer_pressure", lambda: _writer(ses))
    sec("options_positioning", lambda: _positioning(ses, close))
    sec("oi_flow", lambda: _oi_flow(ses))
    sec("llt", lambda: _llt(ses))
    try:
        fii, other = _participants(src.conn, d)
    except Exception as e:
        fii = other = {"available": False, "why": f"{type(e).__name__}: {e}"[:200]}
    p["participants_fii"] = fii or {"available": False, "why": "no participant file on or before "
                                                                "this session"}
    p["participants_other"] = other or {"available": False, "why": "no participant file"}
    sec("banknifty_relative", lambda: _banknifty(src, d, p.get("price_trend") or {}))
    sec("expiry_calendar", lambda: _calendar(d, ses))
    if ses.get("missing"):
        p["session_reads_missing"] = ses["missing"]
    vix = (p.get("volatility") or {}).get("vix")
    if vix is not None:
        levels["vix"] = vix
    p["levels"] = levels
    return p


def blind(panel: dict) -> dict:
    """The panel without its date or any absolute index level."""
    return {k: v for k, v in panel.items() if k not in ("d", "levels")}


# ----------------------------------------------------------------- states --

def _b(v, edges, labels):
    if v is None:
        return None
    for e, lab in zip(edges, labels):
        if v < e:
            return lab
    return labels[-1]


def states(p: dict) -> dict:
    """Discrete readings for the history stats. None = not available."""
    g = lambda sec, k: (p.get(sec) or {}).get(k)
    s = {}
    s["day_move"] = _b(g("price_trend", "day_pct"), [-1, -0.3, 0.3, 1],
                       ["<=-1%", "-1..-0.3%", "flat ±0.3%", "+0.3..+1%", ">=+1%"])
    s["week_move"] = _b(g("price_trend", "week_pct"), [-2, -0.5, 0.5, 2],
                        ["<=-2%", "-2..-0.5%", "flat ±0.5%", "+0.5..+2%", ">=+2%"])
    for k in ("vs_sma20_pct", "vs_sma50_pct", "vs_sma200_pct"):
        v = g("price_trend", k)
        s[k.replace("_pct", "")] = None if v is None else ("above" if v > 0 else "below")
    s["range_52w"] = _b(g("price_trend", "pos_in_52w_range_pct"), [20, 50, 80],
                        ["bottom 20%", "20-50%", "50-80%", "top 20%"])
    s["close_in_day_range"] = _b(g("price_trend", "close_in_day_range_pct"), [33, 67],
                                 ["closed near low", "closed mid-range", "closed near high"])
    s["gap_open"] = _b(g("price_trend", "gap_open_pct"), [-0.3, 0.3],
                       ["gap down", "flat open", "gap up"])
    st = g("momentum_reversal", "streak_sessions")
    s["streak"] = None if st is None else ("down 3+" if st <= -3 else "down 1-2" if st < 0
                                           else "up 1-2" if st < 3 else "up 3+")
    s["day_move_size"] = _b(g("momentum_reversal", "day_move_in_atr"), [0.5, 1.2],
                            ["small (<0.5 ATR)", "normal", "large (>1.2 ATR)"])
    for k in ("golden_goose_bb21", "nidhi_kalash_ema53", "panther_cmf21",
              "ocean_treasure_vwma21_2h"):
        v = g("strategy_signals", k)
        s[k] = None if v in (None, "unknown") else v
    s["hm_daily"] = g("hm_srt", "daily_state")
    s["hm_weekly"] = g("hm_srt", "weekly_state")
    s["hm_daily_v_turn"] = None if g("hm_srt", "daily_v_turn") is None else \
        ("v-turn" if g("hm_srt", "daily_v_turn") else "no v-turn")
    s["srt_daily_zone"] = g("hm_srt", "srt_daily_zone")
    s["srt_weekly_zone"] = g("hm_srt", "srt_weekly_zone")
    s["vix_level"] = _b(g("volatility", "vix"), [12, 15, 20], ["<12", "12-15", "15-20", ">=20"])
    s["vix_day"] = _b(g("volatility", "vix_day_pct"), [-5, 5], ["fell >5%", "±5%", "rose >5%"])
    s["iv_vs_realized"] = _b(g("volatility", "iv_minus_realized_pts"), [-2, 3],
                             ["IV below realised", "IV ~ realised", "IV rich"])
    s["wcash_tilt"] = _b(g("writer_pressure", "tilt_pct"), [-40, -10, 10, 40],
                         ["<=-40", "-40..-10", "±10", "+10..+40", ">=+40"])
    s["ladder_skew"] = _b(g("writer_pressure", "ladder_skew_pct"), [-5, 5],
                          ["calls gained", "balanced", "puts gained"])
    s["ladder_straddle"] = _b(g("writer_pressure", "ladder_straddle_pct"), [-5, 5],
                              ["ATM premium fell", "flat", "ATM premium rose"])
    bh, rh = g("options_positioning", "bullish_held"), g("options_positioning", "bearish_held")
    s["held_builds"] = None if bh is None else ("more bullish held" if bh > rh else
                                                "more bearish held" if rh > bh else "even")
    s["max_pain"] = _b(g("options_positioning", "max_pain_vs_spot_pct"), [-0.5, 0.5],
                       ["below spot", "near spot", "above spot"])
    nm = g("oi_flow", "net_bullish_markers")
    s["oi_flow"] = None if nm is None else ("put-writing led" if nm > 0 else
                                            "call-writing led" if nm < 0 else "none/even")
    nl = g("llt", "net_bullish_lots")
    s["llt"] = None if nl is None else ("bullish prints" if nl > 0 else
                                        "bearish prints" if nl < 0 else "none/even")
    for who, sec in (("fii", "participants_fii"), ("client", "participants_other")):
        b = p.get(sec) or {}
        if who == "client":
            b = b.get("Client") or {}
        v = b.get("net_10d_chg")
        s[f"{who}_net_10d"] = None if v is None else ("rising" if v > 0 else "falling")
        s[f"{who}_short_10d"] = _b(b.get("short_10d_pct"), [-10, 10],
                                   ["shorts fell >10%", "±10%", "shorts rose >10%"])
        s[f"{who}_long_10d"] = _b(b.get("long_10d_pct"), [-10, 10],
                                  ["longs fell >10%", "±10%", "longs rose >10%"])
    s["bn_vs_nifty_week"] = _b(g("banknifty_relative", "rel_week_pts"), [-0.75, 0.75],
                               ["BANKNIFTY lagged", "in line", "BANKNIFTY led"])
    s["weekday"] = g("expiry_calendar", "weekday")
    dte = g("expiry_calendar", "calendar_days_to_nearest_expiry")
    s["expiry_distance"] = None if dte is None else ("expiry day" if dte == 0 else
                                                      "1 day" if dte == 1 else
                                                      "2-3 days" if dte <= 3 else "4+ days")
    return {k: v for k, v in s.items() if v is not None}


STATE_FAMILY = {
    "day_move": "price_trend", "week_move": "price_trend", "vs_sma20": "price_trend",
    "vs_sma50": "price_trend", "vs_sma200": "price_trend", "range_52w": "price_trend",
    "close_in_day_range": "price_trend", "gap_open": "price_trend",
    "streak": "momentum_reversal", "day_move_size": "momentum_reversal",
    "golden_goose_bb21": "strategy_signals", "nidhi_kalash_ema53": "strategy_signals",
    "panther_cmf21": "strategy_signals", "ocean_treasure_vwma21_2h": "strategy_signals",
    "hm_daily": "hm_srt", "hm_weekly": "hm_srt", "hm_daily_v_turn": "hm_srt",
    "srt_daily_zone": "hm_srt", "srt_weekly_zone": "hm_srt",
    "vix_level": "volatility", "vix_day": "volatility", "iv_vs_realized": "volatility",
    "wcash_tilt": "writer_pressure", "ladder_skew": "writer_pressure",
    "ladder_straddle": "writer_pressure", "held_builds": "options_positioning",
    "max_pain": "options_positioning", "oi_flow": "oi_flow", "llt": "llt",
    "fii_net_10d": "participants_fii", "fii_short_10d": "participants_fii",
    "fii_long_10d": "participants_fii", "client_net_10d": "participants_other",
    "client_short_10d": "participants_other", "client_long_10d": "participants_other",
    "bn_vs_nifty_week": "banknifty_relative", "weekday": "expiry_calendar",
    "expiry_distance": "expiry_calendar",
}


def outcomes(closes: list) -> dict:
    """{d: (ret1, ret5, d1, d5)} from [(d, close)] — the next session and the
    fifth, with the dates they become known (for point-in-time cuts)."""
    out = {}
    for i, (d, c) in enumerate(closes):
        r1 = (closes[i + 1][1] / c - 1) * 100 if i + 1 < len(closes) and c else None
        r5 = (closes[i + 5][1] / c - 1) * 100 if i + 5 < len(closes) and c else None
        out[d] = (r1, r5, closes[i + 1][0] if i + 1 < len(closes) else None,
                  closes[i + 5][0] if i + 5 < len(closes) else None)
    return out


def history_stats(rows: list, today: dict, known_by: str, min_n: int = MIN_STATE_N) -> dict:
    """For each of tonight's readings: how often NIFTY rose the next session
    and over the next five after past sessions with the SAME reading, against
    the base rate of all past sessions counted. `rows` = [(d, states, (ret1,
    ret5, d1, d5))]; only outcomes known by `known_by` are used."""
    base1 = [o[0] for d, s, o in rows if o[0] is not None and o[2] and o[2] <= known_by]
    base5 = [o[1] for d, s, o in rows if o[1] is not None and o[3] and o[3] <= known_by]
    if not base1:
        return {"sessions": 0, "readings": []}
    u1 = sum(1 for r in base1 if r > 0) / len(base1)
    u5 = sum(1 for r in base5 if r > 0) / len(base5) if base5 else None
    out = []
    for k, v in today.items():
        n1 = [o[0] for d, s, o in rows if s.get(k) == v and o[0] is not None and o[2]
              and o[2] <= known_by]
        n5 = [o[1] for d, s, o in rows if s.get(k) == v and o[1] is not None and o[3]
              and o[3] <= known_by]
        if not n1:
            out.append({"reading": k, "state": v, "family": STATE_FAMILY.get(k), "n": 0})
            continue
        up1 = sum(1 for r in n1 if r > 0) / len(n1)
        z1 = (up1 - u1) / math.sqrt(u1 * (1 - u1) / len(n1)) if 0 < u1 < 1 else None
        row = {"reading": k, "state": v, "family": STATE_FAMILY.get(k), "n": len(n1),
               "next_up_pct": round(up1 * 100, 1), "next_mean_pct": round(sum(n1) / len(n1), 3),
               "z_next": None if z1 is None else round(z1, 2)}
        if n5 and u5 is not None and 0 < u5 < 1:
            up5 = sum(1 for r in n5 if r > 0) / len(n5)
            # 5-session windows overlap and readings persist: n/5 effective
            row.update(week_up_pct=round(up5 * 100, 1),
                       week_mean_pct=round(sum(n5) / len(n5), 3),
                       z_week=round((up5 - u5) / math.sqrt(u5 * (1 - u5) / len(n5))
                                    / math.sqrt(5), 2))
        if len(n1) < min_n:
            row["note"] = "few sessions — listed, not judged"
        out.append(row)
    out.sort(key=lambda r: -abs(r.get("z_next") or 0))
    return {"sessions": len(base1), "base_next_up_pct": round(u1 * 100, 1),
            "base_week_up_pct": None if u5 is None else round(u5 * 100, 1),
            "readings": out,
            "caution": f"{len(out)} readings are compared at once, so a |z| near 2 is expected "
                       "for one or two of them by luck. Readings persist for weeks (e.g. above "
                       "SMA200), so neighbouring sessions are not independent."}


# ------------------------------------------------------------------- store --

def save(conn, d: str, panel: dict, source: str, underlying: str = "NIFTY") -> dict:
    s = states(panel)
    conn.execute("INSERT OR REPLACE INTO forecast_panel (underlying, d, panel_json, states_json, "
                 "source, built_at) VALUES (?,?,?,?,?,?)",
                 (underlying, d, json.dumps(panel, default=str), json.dumps(s), source,
                  datetime.now(IST).isoformat(timespec="seconds")))
    conn.commit()
    return s


def stats_rows(conn, underlying: str = "NIFTY", before: Optional[str] = None) -> list:
    """[(d, states, outcome)] for stored panels before `before`."""
    oc = outcomes(nifty_closes(conn))
    q = "SELECT d, states_json FROM forecast_panel WHERE underlying=?"
    args = [underlying]
    if before:
        q += " AND d<?"
        args.append(before)
    return [(r[0], json.loads(r[1]), oc.get(r[0], (None, None, None, None)))
            for r in conn.execute(q + " ORDER BY d", args)]

"""Participant-wise derivatives positioning (FII / DII / Pro / Client).

NSE publishes, every evening, the open interest of each participant CLASS in
equity derivatives. It is the only public view of who is actually positioned
which way, and it costs one CSV a day:

    nsearchives.nseindia.com/content/nsccl/fao_participant_oi_DDMMYYYY.csv

`nsearchives` is not behind the bot wall that blocks `www.nseindia.com` — the
same split found in #81 for the F&O ban list — so this needs no session
handshake and no Kite login.

FOUR CLASSES, and the interesting one is usually missing from other dashboards:
  FII     foreign institutions
  DII     domestic institutions
  Pro     proprietary desks — the market makers and prop shops
  Client  everyone else, i.e. retail plus unclassified

WHAT THIS IS AND IS NOT. It is end-of-day and daily, so it is a slow
positioning gauge, never a trading signal — by the time you read it the session
is over. It is also CONTRACT COUNTS, not rupees. Dashboards that print a rupee
figure for DII are estimating: NSE publishes a value report for FII only, so a
DII rupee number is somebody's contract-count times somebody else's average
value. We store contracts and refuse to invent the rupees.

ONE DAY SAYS LITTLE; THE TREND IS THE POINT. `history()` follows each class's
long and short OI across sessions, separately — net hides it when a class is
cutting longs and adding shorts at once. Three properties of this data shape
every number it reports:

  * MONTHLY EXPIRY IS A STEP, not a decision on that day. Whatever isn't rolled
    disappears: FII index-futures shorts fell 16-23% on each of the last four
    monthly expiries against a normal day's 2.3%. Each change is therefore
    split into the expiry-day step and the active remainder.
  * WEEKLY EXPIRY IS A SAWTOOTH in options. Index-option OI collapses ~42% on
    expiry and rebuilds ~15% a day, so raw option or "total" OI on two dates
    mostly compares their places in the cycle. Option stance is read on a
    5-session average, and raw totals are not offered.
  * "A LOT" IS MEASURED, NOT ASSERTED. Each move is ranked against that same
    series' own rolling N-session changes, and each level against its own past.

It describes who did what. Whether a trend predicts the market is untested.
"""
from __future__ import annotations

import calendar
import csv
import io
import sqlite3
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Optional

ARCHIVE = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d}.csv"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

FIELDS = [
    ("fut_idx_long", "Future Index Long"), ("fut_idx_short", "Future Index Short"),
    ("fut_stk_long", "Future Stock Long"), ("fut_stk_short", "Future Stock Short"),
    ("opt_idx_ce_long", "Option Index Call Long"),
    ("opt_idx_pe_long", "Option Index Put Long"),
    ("opt_idx_ce_short", "Option Index Call Short"),
    ("opt_idx_pe_short", "Option Index Put Short"),
    ("opt_stk_ce_long", "Option Stock Call Long"),
    ("opt_stk_pe_long", "Option Stock Put Long"),
    ("opt_stk_ce_short", "Option Stock Call Short"),
    ("opt_stk_pe_short", "Option Stock Put Short"),
    ("total_long", "Total Long Contracts"), ("total_short", "Total Short Contracts"),
]


HIST_DAYS = 430                 # calendar days of files for a year+ of sessions
HIST_TARGET_SESSIONS = 250
WINDOWS = (5, 10, 20, 60)
CLASSES = ("FII", "DII", "Pro", "Client")
EXPIRY_DROP = 0.18              # one-day fall in index-option OI that marks an expiry
MOVE_PCT = 10.0                 # a side "moved" at this % ... or when rare for it:
RARE_TAIL = 0.05                # ... in the outer 5% of its own rolling changes
MIN_RANK_WINDOWS = 60           # below this a rank means nothing — don't report one
AVG_SESSIONS = 5                # one weekly cycle, to flatten the option sawtooth
INSTRUMENTS = {
    "idx_fut": {"label": "Index futures", "long": "fut_idx_long", "short": "fut_idx_short",
                "net": "fut_idx_net"},
    "stk_fut": {"label": "Stock futures", "long": "fut_stk_long", "short": "fut_stk_short",
                "net": "fut_stk_net"},
    "idx_opt": {"label": "Index options", "net": "opt_idx_net"},
    "stk_opt": {"label": "Stock options", "net": "opt_stk_net"},
}
IST = timezone(timedelta(hours=5, minutes=30))


def fetch(d: date) -> dict:
    """One session's participant OI. Returns {"ok", "rows", "error"}."""
    url = ARCHIVE.format(d=d.strftime("%d%m%Y"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "rows": []}
    lines = list(csv.reader(io.StringIO(raw)))
    if len(lines) < 3:
        return {"ok": False, "error": "unexpected file shape", "rows": []}
    hdr = [h.strip() for h in lines[1]]
    rows = []
    for ln in lines[2:]:
        if not ln or not ln[0].strip():
            continue
        rec = dict(zip(hdr, [c.strip() for c in ln]))
        ct = rec.get("Client Type", "").strip()
        if not ct or ct.upper() == "TOTAL":       # TOTAL is definitionally zero-sum
            continue
        out = {"d": d.isoformat(), "client_type": ct}
        for key, col in FIELDS:
            try:
                out[key] = int(float(rec.get(col) or 0))
            except Exception:
                out[key] = 0
        out["fut_idx_net"] = out["fut_idx_long"] - out["fut_idx_short"]
        out["fut_stk_net"] = out["fut_stk_long"] - out["fut_stk_short"]
        # Options stance: long calls + short puts is a bullish lean, and the
        # mirror is bearish. One number, same convention for every class.
        out["opt_idx_net"] = ((out["opt_idx_ce_long"] + out["opt_idx_pe_short"])
                              - (out["opt_idx_ce_short"] + out["opt_idx_pe_long"]))
        out["opt_stk_net"] = ((out["opt_stk_ce_long"] + out["opt_stk_pe_short"])
                              - (out["opt_stk_ce_short"] + out["opt_stk_pe_long"]))
        rows.append(out)
    return {"ok": bool(rows), "rows": rows, "error": None if rows else "no rows"}


def store(conn, rows: list) -> int:
    cols = ["d", "client_type"] + [k for k, _ in FIELDS] + [
        "fut_idx_net", "fut_stk_net", "opt_idx_net", "opt_stk_net"]
    conn.executemany(
        f"INSERT OR REPLACE INTO participant_oi ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def backfill(conn, days: int = 10, pause: float = 0.0) -> dict:
    """Walk back `days` calendar days, storing whatever sessions exist.
    Weekends and holidays simply 404 — that is not an error worth raising."""
    today = date.today()
    got = miss = 0
    for i in range(days):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        if conn.execute("SELECT 1 FROM participant_oi WHERE d=? LIMIT 1",
                        (d.isoformat(),)).fetchone():
            continue
        # Skip days the exchange did not trade (confirmed in #85 by an
        # index-bar probe). Without this a holiday like 2026-09-14 is re-fetched
        # on every run forever, since NSE never publishes a file for it.
        try:
            if conn.execute("SELECT 1 FROM non_trading_day WHERE d=? LIMIT 1",
                            (d.isoformat(),)).fetchone():
                continue
        except Exception:
            pass                       # table may not exist on an older DB
        res = fetch(d)
        if res["ok"]:
            store(conn, res["rows"])
            got += 1
        else:
            miss += 1
        if pause:
            time.sleep(pause)
    return {"stored_sessions": got, "unavailable": miss}


def ensure_history(conn) -> Optional[dict]:
    """One-time deep fetch so the ranks have something to rank against. Cheap
    afterwards: a single COUNT once the table holds a year of sessions."""
    have = conn.execute("SELECT COUNT(DISTINCT d) FROM participant_oi").fetchone()[0]
    if have >= HIST_TARGET_SESSIONS:
        return None
    return backfill(conn, days=HIST_DAYS, pause=0.12)


def latest(conn, until: Optional[str] = None) -> dict:
    """The newest session's file (or the newest on/before `until`)."""
    d = conn.execute("SELECT MAX(d) FROM participant_oi WHERE d<=?",
                     (until or "9999-12-31",)).fetchone()[0]
    if not d:
        return {"ok": False, "reason": "no participant data yet"}
    prev = conn.execute("SELECT MAX(d) FROM participant_oi WHERE d<?", (d,)).fetchone()[0]
    def load(day):
        cur = conn.execute("SELECT * FROM participant_oi WHERE d=?", (day,))
        names = [c[0] for c in cur.description]
        return {r[names.index("client_type")]: dict(zip(names, r)) for r in cur}
    cur_, prv = load(d), (load(prev) if prev else {})
    for ct, row in cur_.items():
        p = prv.get(ct)
        for k in ("fut_idx_net", "opt_idx_net", "fut_stk_net", "opt_stk_net"):
            row[k + "_chg"] = (row[k] - p[k]) if p else None
    fii, dii = cur_.get("FII"), cur_.get("DII")
    opposed = bool(fii and dii and fii["fut_idx_net"] * dii["fut_idx_net"] < 0)
    return {"ok": True, "date": d, "prev_date": prev, "participants": cur_,
            "fii_dii_opposed": opposed,
            "note": "contract counts, end-of-day — a positioning gauge, not a signal"}


# ------------------------------------------------------------------ trend --

def _sessions(conn, until: Optional[str] = None) -> tuple:
    """(dates, {class: {column: [values]}}) for sessions all four classes have,
    on or before `until` (a replay must not see later files)."""
    cur = conn.execute("SELECT * FROM participant_oi WHERE d<=? ORDER BY d",
                       (until or "9999-12-31",))
    names = [c[0] for c in cur.description]
    by = {}
    for r in cur:
        row = dict(zip(names, r))
        by.setdefault(row["d"], {})[row["client_type"]] = row
    dates = [d for d in sorted(by) if all(c in by[d] for c in CLASSES)]
    return dates, {c: {k: [by[d][c][k] or 0 for d in dates] for k in names
                       if k not in ("d", "client_type")} for c in CLASSES}


def weekly_expiries(dates: list, client: dict) -> list:
    """Read from the data, so a holiday-shifted expiry (Mon 9 Nov 2026) is
    caught: the sessions where retail's index-option longs collapse."""
    oi = [a + b for a, b in zip(client["opt_idx_ce_long"], client["opt_idx_pe_long"])]
    return [dates[i] for i in range(1, len(dates))
            if oi[i - 1] and oi[i] <= oi[i - 1] * (1 - EXPIRY_DROP)]


def _last_weekday(y: int, m: int, wd: int) -> date:
    d = date(y, m, calendar.monthrange(y, m)[1])
    return d - timedelta(days=(d.weekday() - wd) % 7)


def monthly_expiries(dates: list, weekly: list) -> list:
    """The month's final weekly expiry. A month still in progress has none yet
    — its latest weekly (e.g. 9 Nov) is not the monthly."""
    out = []
    for y, m in sorted({(int(d[:4]), int(d[5:7])) for d in dates}):
        # NIFTY monthly: last Thursday until Aug 2025, last Tuesday since
        due = _last_weekday(y, m, 1 if (y, m) >= (2025, 9) else 3)
        if due.isoformat() > dates[-1]:
            continue
        lo = (due - timedelta(days=4)).isoformat()
        hit = [w for w in weekly if lo <= w <= due.isoformat()]
        if hit:
            out.append(hit[-1])
        else:                                      # expiry detector missed it
            prior = [d for d in dates if d <= due.isoformat()]
            if prior:
                out.append(prior[-1])
    return out


def _nifty_closes(conn, dates: list) -> dict:
    """{date: close}: our recorded candles, then the Fyers archive for older days."""
    out = {}
    try:
        for d, c in conn.execute(
                "SELECT substr(ts,1,10) d, close FROM underlying_candle WHERE underlying='NIFTY' "
                "AND ts>=? ORDER BY ts", (dates[0],)):
            out[d] = c                             # ordered: the day's last candle wins
    except sqlite3.Error:
        pass
    if any(d not in out for d in dates):
        try:
            import db
            arch = sqlite3.connect(f"file:{db.DATA_DIR / 'fyers_hist.db'}?mode=ro", uri=True, timeout=5)
            for ts, c in arch.execute("SELECT ts, c FROM index_bar WHERE underlying='NIFTY' AND res='1D'"):
                out.setdefault(datetime.fromtimestamp(int(ts), IST).date().isoformat(), c)
            arch.close()
        except sqlite3.Error:
            pass
    return out


def _rolling_mean(s: list, n: int) -> list:
    return [sum(s[max(0, i - n + 1):i + 1]) / (i - max(0, i - n + 1) + 1) for i in range(len(s))]


def _month_year(d: str) -> str:
    return date.fromisoformat(d).strftime("%b %Y")


def _rank(cur: float, past: list, dates: list, w: int, noun: str) -> dict:
    """How unusual this N-session move is against the series' own history."""
    if len(past) < MIN_RANK_WINDOWS:
        return {}
    pos = sum(1 for x in past if x <= cur) / len(past)
    since = _month_year(dates[0])
    out = {"pct_rank": round(pos * 100, 1), "n_windows": len(past)}
    if cur <= min(past):
        out["rare"] = f"largest {w}-session {noun[0]} since {since}"
    elif cur >= max(past):
        out["rare"] = f"largest {w}-session {noun[1]} since {since}"
    elif pos <= RARE_TAIL:
        out["rare"] = f"among the largest {noun[0]}s since {since}"
    elif pos >= 1 - RARE_TAIL:
        out["rare"] = f"among the largest {noun[1]}s since {since}"
    return out


def _level_since(s: list, dates: list) -> dict:
    """Is today's level the lowest/highest for a meaningful stretch?"""
    cur, i = s[-1], len(s) - 2
    lo = next((j for j in range(i, -1, -1) if s[j] <= cur), None)
    hi = next((j for j in range(i, -1, -1) if s[j] >= cur), None)
    out = {}
    if lo is None or i + 1 - lo >= 30:
        out["low_since"] = dates[lo] if lo is not None else f"start of data ({_month_year(dates[0])})"
    if hi is None or i + 1 - hi >= 30:
        out["high_since"] = dates[hi] if hi is not None else f"start of data ({_month_year(dates[0])})"
    return out


def _side(s: list, dates: list, w: int, monthly: set) -> dict:
    """One long-or-short series over the window, expiry step split out."""
    start, end = s[-1 - w], s[-1]
    steps = [(dates[i], s[i] - s[i - 1]) for i in range(len(s) - w, len(s)) if dates[i] in monthly]
    step = sum(v for _, v in steps)
    moves = [s[i] - s[i - 1] for i in range(len(s) - w, len(s))]
    out = {"start": start, "end": end, "chg": end - start,
           "pct": round((end / start - 1) * 100, 1) if start else None,
           "expiry_chg": step, "active_chg": (end - start) - step,
           "expiry_dates": [d for d, _ in steps],
           "days_up": sum(1 for v in moves if v > 0), "days_down": sum(1 for v in moves if v < 0)}
    past = [s[i] / s[i - w] - 1 for i in range(w, len(s)) if s[i - w]]
    if s[-1 - w]:
        out.update(_rank(s[-1] / s[-1 - w] - 1, past[:-1] or past, dates, w, ("fall", "rise")))
    out.update(_level_since(s, dates))
    return out


def _net(s: list, dates: list, w: int, monthly: set) -> dict:
    """Net changes sign, so % is meaningless: absolute change, absolute rank."""
    moves = [s[i] - s[i - 1] for i in range(len(s) - w, len(s))]
    out = {"start": s[-1 - w], "end": s[-1], "chg": s[-1] - s[-1 - w],
           "days_up": sum(1 for v in moves if v > 0), "days_down": sum(1 for v in moves if v < 0),
           "expiry_chg": sum(s[i] - s[i - 1] for i in range(len(s) - w, len(s)) if dates[i] in monthly)}
    past = [s[i] - s[i - w] for i in range(w, len(s))]
    out.update(_rank(s[-1] - s[-1 - w], past[:-1] or past, dates, w, ("fall", "rise")))
    return out


def _n(v) -> str:
    """Indian digit grouping."""
    v = int(round(v))
    sign, x = ("-" if v < 0 else ""), str(abs(v))
    if len(x) > 3:
        head, x = x[:-3], x[-3:]
        parts = []
        while head:
            parts.insert(0, head[-2:])
            head = head[:-2]
        x = ",".join(parts + [x])
    return sign + x


def _moved(r: dict) -> bool:
    if r.get("pct") is None:
        return False
    return abs(r["pct"]) >= MOVE_PCT or "rare" in r


def _day_month(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x.day} {x.strftime('%b')}"


def _sentence(read: dict, w: int) -> str:
    """Plain words for what this class's longs and shorts did. A change that is
    mostly the monthly expiry step is called that, not a decision: on one real
    window Pro's longs were "-61%" with -48,156 of it from expiry and +3,530
    otherwise — they had been adding."""
    verbs = {("long", True): "adding longs", ("long", False): "cutting longs",
             ("short", True): "adding shorts", ("short", False): "covering shorts"}
    parts = []
    for side in sorted(("long", "short"), key=lambda k: -abs(read[k].get("chg") or 0)):
        r = read[side]
        if not _moved(r):
            continue
        rng = f"{r['pct']:+.0f}% ({_n(r['start'])} → {_n(r['end'])})"
        step, chg = r["expiry_chg"], r["chg"]
        days = [_day_month(d) for d in r["expiry_dates"]]
        when = (", ".join(days[:-1]) + " and " + days[-1]) if len(days) > 1 else "".join(days)
        expiry_word = "expiries" if len(days) > 1 else "expiry"
        mostly_expiry = step and step * chg > 0 and abs(step) >= 0.5 * abs(chg)
        if mostly_expiry:
            parts.append(f"{side}s {rng} — mostly the {when} {expiry_word} "
                         f"(everything else: {'+' if r['active_chg'] > 0 else ''}{_n(r['active_chg'])})")
            continue
        txt = f"{verbs[(side, chg > 0)]} {rng}"
        if step:
            txt += f", {_n(step)} of it on the {when} {expiry_word}" if step * chg > 0 else ""
        if r.get("rare"):
            txt += f" — {r['rare']}"
        parts.append(txt)
    if not parts:
        return f"Little change in longs or shorts over {w} sessions."
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def history(conn, instrument: str = "idx_fut", window: int = 10,
            until: Optional[str] = None) -> dict:
    """Each class's positioning over the last `window` sessions, with reads.
    `until` cuts the data at a past session (forecast replay)."""
    if instrument not in INSTRUMENTS:
        return {"ok": False, "reason": f"instrument must be one of {list(INSTRUMENTS)}"}
    if window not in WINDOWS:
        return {"ok": False, "reason": f"window must be one of {list(WINDOWS)}"}
    dates, data = _sessions(conn, until)
    if len(dates) <= window:
        return {"ok": False, "reason": f"only {len(dates)} sessions stored so far — the history "
                                       "fills in automatically after a restart"}
    weekly = weekly_expiries(dates, data["Client"])
    monthly = set(monthly_expiries(dates, weekly))
    spec = INSTRUMENTS[instrument]
    futures = "long" in spec
    shown = max(window + 1, 21)
    d_shown = dates[-shown:]
    classes = []
    for c in CLASSES:
        net = data[c][spec["net"]]
        item = {"name": c, "series": {"net": net[-shown:]}}
        if futures:
            lg, sh = data[c][spec["long"]], data[c][spec["short"]]
            item["series"].update(long=lg[-shown:], short=sh[-shown:])
            item["read"] = {"long": _side(lg, dates, window, monthly),
                            "short": _side(sh, dates, window, monthly),
                            "net": _net(net, dates, window, monthly)}
            item["sentence"] = _sentence(item["read"], window)
        else:
            avg = _rolling_mean(net, AVG_SESSIONS)
            item["series"]["avg"] = [round(v) for v in avg[-shown:]]
            r = _net(avg, dates, window, set())
            r.update(start=round(r["start"]), end=round(r["end"]), chg=round(r["chg"]))
            r.pop("expiry_chg", None)
            item["read"] = {"net": r}
            lean = "more bullish" if r["chg"] > 0 else "more bearish"
            item["sentence"] = (f"Options stance {lean} by {_n(abs(r['chg']))} contracts "
                                f"({_n(r['start'])} → {_n(r['end'])}, {AVG_SESSIONS}-session average)"
                                + (f" — {r['rare']}" if r.get("rare") else "") + ".")
        classes.append(item)
    closes = _nifty_closes(conn, dates)
    nifty = [closes.get(d) for d in d_shown]
    a, b = closes.get(dates[-1 - window]), closes.get(dates[-1])
    return {"ok": True, "instrument": instrument, "label": spec["label"], "futures": futures,
            "window": window, "windows": list(WINDOWS),
            "instruments": [{"key": k, "label": v["label"]} for k, v in INSTRUMENTS.items()],
            "as_of": dates[-1], "window_start": dates[-1 - window], "first_date": dates[0],
            "sessions_stored": len(dates), "dates": d_shown, "nifty": nifty,
            "nifty_move": ({"start": a, "end": b, "pct": round((b / a - 1) * 100, 2)} if a and b else None),
            "weekly_expiries": [d for d in weekly if d >= d_shown[0]],
            "monthly_expiries": sorted(d for d in monthly if d >= d_shown[0]),
            "classes": classes}


def trend_summary(conn, window: int = 10) -> dict:
    """Compact trend reads for the assistant's market-context tool."""
    out = {"window_sessions": window,
           "note": "descriptive positioning; whether a trend predicts the market is untested"}
    for key in ("idx_fut", "stk_fut"):
        h = history(conn, key, window)
        if not h.get("ok"):
            return h
        out["as_of"], out["nifty_move"] = h["as_of"], h["nifty_move"]
        out[key] = {c["name"]: {"read": c["sentence"],
                                "net": [c["read"]["net"]["start"], c["read"]["net"]["end"]],
                                "expiry_step_long": c["read"]["long"]["expiry_chg"],
                                "expiry_step_short": c["read"]["short"]["expiry_chg"]}
                    for c in h["classes"]}
    return out

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
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date, timedelta

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


def backfill(conn, days: int = 10) -> dict:
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
        res = fetch(d)
        if res["ok"]:
            store(conn, res["rows"])
            got += 1
        else:
            miss += 1
    return {"stored_sessions": got, "unavailable": miss}


def latest(conn) -> dict:
    d = conn.execute("SELECT MAX(d) FROM participant_oi").fetchone()[0]
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

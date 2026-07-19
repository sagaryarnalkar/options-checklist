"""
LLT — Large-Lot-Trader detection on NIFTY current-month futures.

Origin: a Vtrender-style order-flow read (tweet ref: "LLT buyer, 1917 lots at
the last minute → expecting gap-up"). The source spec assumed a tick-level
KiteTicker daemon (bid/ask aggressor, 10-second windows); THIS implementation
is the minute-resolution adaptation that fits the existing recorder
architecture — the tweet's own example is a per-minute read. A tick-level
stage 2 is possible later without touching this schema.

Pipeline (detect_day, idempotent via UNIQUE(ts,symbol)):
  1. Walk futures_minute for a date; per minute compute traded lots
     (Δcumulative-volume ÷ lot size), price direction, ΔOI.
  2. A minute is an LLT print when lots ≥ max(LLT_MIN_LOTS, rolling
     median + LLT_MAD_K·MAD of the trailing LLT_BASE_WINDOW minutes) —
     same robust-threshold philosophy as the OI-Flow score gate.
  3. Side = minute price direction (up=BUY, down=SELL; flat minutes inherit
     the previous direction — the aggressor heuristic at minute scale).
  4. OI quadrant (per spec):  BUY+OI↑ FRESH LONGS · BUY+OI↓ SHORT COVERING ·
     SELL+OI↑ FRESH SHORTS · SELL+OI↓ LONG UNWINDING.  ΔOI here is
     contract-wide, so the classification is INFERRED — confidence HIGH when
     the print is >50% of the trailing 10-minute volume, else MEDIUM. The
     spec's "+60s re-check" maps to the NEXT minute's ΔOI (oi_delta_next).
  5. closing_flag for 15:00–15:30 IST prints (overnight-gap relevance).
  6. Cross-session matching: opposite side within ±15% of size across the
     last 3 sessions → matched_id ("possible same-player exit").
"""
from __future__ import annotations

import statistics
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# Config (documented defaults; minute-resolution adaptation of the spec)
LLT_MIN_LOTS = 750          # absolute floor for a minute's traded lots
LLT_MAD_K = 4.0             # adaptive gate: median + K·MAD of trailing volume
LLT_BASE_WINDOW = 60        # minutes in the adaptive baseline
LLT_CONF_WINDOW = 10        # HIGH confidence: print > 50% of this window's vol
LLT_MATCH_SESSIONS = 3      # cross-session matching lookback (sessions)
LLT_MATCH_TOLERANCE = 0.15  # ±15% size match


def detect_day(conn, date_iso: str, lot_size: int) -> int:
    """(Re)detect LLT prints for one day. Returns rows written. Idempotent."""
    rows = conn.execute(
        "SELECT ts, symbol, ltp, volume, oi FROM futures_minute "
        "WHERE DATE(ts)=? ORDER BY ts", (date_iso,)).fetchall()
    if len(rows) < 3 or not lot_size:
        return 0

    lots_hist: deque = deque(maxlen=LLT_BASE_WINDOW)
    conf_hist: deque = deque(maxlen=LLT_CONF_WINDOW)
    prints = []
    last_dir = None
    for i in range(1, len(rows)):
        p, r = rows[i - 1], rows[i]
        if r["volume"] is None or p["volume"] is None:
            continue
        dvol = r["volume"] - p["volume"]
        if dvol < 0:            # day rollover artifact / bad row
            dvol = 0
        lots = dvol / lot_size
        dltp = (r["ltp"] or 0) - (p["ltp"] or 0)
        doi = (r["oi"] - p["oi"]) if (r["oi"] is not None and p["oi"] is not None) else None
        side = "BUY" if dltp > 0 else ("SELL" if dltp < 0 else last_dir)
        if dltp:
            last_dir = side

        # adaptive + absolute gate (evaluated BEFORE adding this minute)
        gate = LLT_MIN_LOTS
        if len(lots_hist) >= 10:
            med = statistics.median(lots_hist)
            mad = statistics.median([abs(v - med) for v in lots_hist])
            gate = max(LLT_MIN_LOTS, med + LLT_MAD_K * 1.4826 * mad)
        window_vol = sum(conf_hist)
        if side and lots >= gate:
            # ΔOI one minute later, when available
            doi_next = None
            if i + 1 < len(rows) and rows[i + 1]["oi"] is not None and r["oi"] is not None:
                doi_next = rows[i + 1]["oi"] - r["oi"]
            if doi is None or doi == 0:
                classification = "UNCLEAR"
            elif side == "BUY":
                classification = "FRESH LONGS" if doi > 0 else "SHORT COVERING"
            else:
                classification = "FRESH SHORTS" if doi > 0 else "LONG UNWINDING"
            confidence = "HIGH" if (window_vol > 0 and dvol > 0.5 * window_vol) else "MEDIUM"
            hm = r["ts"][11:16]
            prints.append({
                "ts": r["ts"], "symbol": r["symbol"], "side": side,
                "lots": int(round(lots)), "price": r["ltp"],
                "oi_delta": doi, "oi_delta_next": doi_next,
                "classification": classification, "confidence": confidence,
                "closing_flag": 1 if "15:00" <= hm <= "15:30" else 0,
            })
        lots_hist.append(lots)
        conf_hist.append(dvol)

    written = 0
    for pr in prints:
        cur = conn.execute(
            "INSERT OR IGNORE INTO llt_prints (ts, symbol, side, lots, price,"
            " oi_delta, oi_delta_next, classification, confidence, closing_flag)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pr["ts"], pr["symbol"], pr["side"], pr["lots"], pr["price"],
             pr["oi_delta"], pr["oi_delta_next"], pr["classification"],
             pr["confidence"], pr["closing_flag"]))
        written += cur.rowcount
    conn.commit()
    if written:
        _match_cross_session(conn, date_iso)
    return written


def _match_cross_session(conn, date_iso: str) -> None:
    """Flag prints that mirror an opposite-side print of similar size within
    the last LLT_MATCH_SESSIONS sessions ("possible same-player exit")."""
    sessions = [r[0] for r in conn.execute(
        "SELECT DISTINCT DATE(ts) FROM llt_prints WHERE DATE(ts) <= ? "
        "ORDER BY DATE(ts) DESC LIMIT ?", (date_iso, LLT_MATCH_SESSIONS + 1)).fetchall()]
    if not sessions:
        return
    lo = min(sessions)
    todays = conn.execute(
        "SELECT id, ts, side, lots FROM llt_prints WHERE DATE(ts)=? AND matched_id IS NULL",
        (date_iso,)).fetchall()
    for t in todays:
        opp = "SELL" if t["side"] == "BUY" else "BUY"
        m = conn.execute(
            "SELECT id FROM llt_prints WHERE DATE(ts) >= ? AND ts < ? AND side=? "
            "AND ABS(lots - ?) <= ? ORDER BY ts DESC LIMIT 1",
            (lo, t["ts"], opp, t["lots"], int(t["lots"] * LLT_MATCH_TOLERANCE))).fetchone()
        if m:
            conn.execute("UPDATE llt_prints SET matched_id=? WHERE id=?", (m["id"], t["id"]))
    conn.commit()


def prints_for_dates(conn, dates: list) -> list:
    if not dates:
        return []
    q = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT * FROM llt_prints WHERE DATE(ts) IN ({q}) ORDER BY ts", dates).fetchall()
    return [dict(r) for r in rows]

"""
Paper-trading ledger.

Every actionable dashboard recommendation (context fresh / rollover /
scheduled) is assumed EXECUTED at PAPER_LOTS lots at the quoted premiums.
paper.sync() runs on every compute.py refresh: it marks open positions from
live quotes, applies each strategy's exit rules, settles expired legs at
intrinsic, and opens new positions. The goal is outcome tracking of the
dashboard's own advice — not order routing.

Sign convention (matches db.py): net cash PER UNIT, SELL premium positive,
BUY negative. entry_value > 0 = credit structure, < 0 = debit. Unrealized
P&L per unit = entry_value + mark_value, where mark_value is the net cash of
closing every leg now. Rupee figures multiply by lot_size × lots.

V1 limitations (documented in docs/APP_OVERVIEW.md):
- Marks only when compute.py runs (user refresh / auto-refresh) — not minutely.
- OT / GG-LEAPS monthly hedge rolls are NOT simulated; those structures close
  only on signal flip, expiry settle, or manual exit_reason via SQL.
- Fills at last-traded price, no slippage/costs.
"""
from __future__ import annotations

import json
from datetime import datetime, date, timedelta, timezone
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))
PAPER_LOTS = 10
ACTIONABLE = {"fresh", "rollover", "scheduled"}

# ---- Transaction-cost model (NSE index options, Zerodha-style) ----
# Every executed leg-side is charged; realized_pnl is NET of these. Rates as
# of 2026; update here if regulations change.
BROKERAGE_PER_ORDER = 20.0     # flat per executed order (one leg = one order)
STT_SELL_PCT       = 0.001     # 0.1% of premium on option SALES (Oct-2024 rate)
STT_EXERCISE_PCT   = 0.00125   # 0.125% of intrinsic when a LONG leg expires ITM
EXCH_TXN_PCT       = 0.0003503 # NSE transaction charge on premium (both sides)
SEBI_PCT           = 0.000001  # Rs 10 / crore
GST_PCT            = 0.18      # on brokerage + exchange txn + SEBI
STAMP_BUY_PCT      = 0.00003   # 0.003% of premium, BUY side only
SLIPPAGE_PCT       = 0.005     # model: 0.5% of premium per executed leg-side


def _exec_cost(is_buy: bool, premium: float, units: int) -> float:
    """Cost of one executed order: brokerage + statutory charges + modeled
    slippage. `premium` is per unit; `units` = lot_size × lots."""
    notional = max(premium, 0.0) * units
    exch = notional * EXCH_TXN_PCT
    sebi = notional * SEBI_PCT
    gst = (BROKERAGE_PER_ORDER + exch + sebi) * GST_PCT
    stt = 0.0 if is_buy else notional * STT_SELL_PCT
    stamp = notional * STAMP_BUY_PCT if is_buy else 0.0
    slip = notional * SLIPPAGE_PCT
    return BROKERAGE_PER_ORDER + exch + sebi + gst + stt + stamp + slip


def _entry_costs(legs, units: int) -> float:
    """All legs are executed orders at entry. SELL legs pay STT on premium."""
    return sum(_exec_cost(l["action"] == "BUY", l["premium"] or 0.0, units)
               for l in legs)


def _exit_costs(leg_fills, units: int) -> float:
    """leg_fills: [(leg, px, settled_at_expiry)]. Market closes are executed
    orders (closing a SHORT = buy order; closing a LONG = sell order, pays
    STT). Expiry settlements place no order: worthless legs cost nothing;
    a LONG leg expiring ITM pays exercise STT on intrinsic; a SHORT leg's
    assignment carries no STT for us."""
    total = 0.0
    for leg, px, settled in leg_fills:
        if settled:
            if leg["action"] == "BUY" and px > 0:
                total += px * units * STT_EXERCISE_PCT
            continue
        closing_is_buy = (leg["action"] == "SELL")
        total += _exec_cost(closing_is_buy, px or 0.0, units)
    return total

# Strategies whose rollover context means "close old structure, open fresh"
FULL_ROLL = {"golden_goose", "panther", "nidhi_kalash"}
# Direction-flip strategies (positional; close only when the signal flips)
FLIP_ONLY = {"ocean_treasure", "gg_leaps"}


def _leg_expiries(rec: dict) -> list:
    """Best-effort per-leg expiry (ISO date string) aligned with rec['legs']."""
    legs = rec.get("legs") or []
    exp = str(rec.get("expiry") or "")
    out = []
    import re
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", exp)
    for leg in legs:
        if leg.get("leg_expiry"):
            out.append(leg["leg_expiry"])
        elif len(dates) == 1:
            out.append(dates[0])
        elif len(dates) >= 2:
            # "short <d1>, hedge <d2>" convention: SELL legs → d1, BUY legs → d2
            out.append(dates[0] if leg.get("action") == "SELL" else dates[1])
        else:
            out.append(None)
    return out


def _quote_legs(kite, legs: list) -> dict:
    syms = [f"NFO:{l['tradingsymbol']}" for l in legs]
    quotes = {}
    for i in range(0, len(syms), 200):
        chunk = syms[i:i + 200]
        try:
            quotes.update(kite.quote(chunk))
        except Exception:
            for s in chunk:
                try:
                    quotes.update(kite.quote([s]))
                except Exception:
                    pass
    return {s.split(":", 1)[1]: (q or {}).get("last_price") for s, q in quotes.items()}


def _expiry_spot(conn, expiry_iso: str, fallback: Optional[float]) -> Optional[float]:
    """NIFTY close on the expiry day from the recorder's candles."""
    try:
        row = conn.execute(
            "SELECT close FROM underlying_candle WHERE underlying='NIFTY' "
            "AND DATE(ts)=? ORDER BY ts DESC LIMIT 1", (expiry_iso,)).fetchone()
        if row and row[0]:
            return float(row[0])
    except Exception:
        pass
    return fallback


def _close_cash(legs, leg_exps, quotes, conn, today_iso, spot) -> tuple:
    """Net cash per unit to close all legs now. Expired legs settle at
    intrinsic vs that day's NIFTY close. Returns (cash, notes, leg_fills)
    where leg_fills = [(leg, px, settled_at_expiry)] for the cost model."""
    cash, notes, fills = 0.0, [], []
    for leg, lexp in zip(legs, leg_exps):
        px = quotes.get(leg["tradingsymbol"])
        settled = False
        if lexp and lexp < today_iso:
            settled = True
            s = _expiry_spot(conn, lexp, spot)
            if s is None:
                notes.append(f"{leg['tradingsymbol']}: no expiry spot, used premium 0")
                px = 0.0
            else:
                k = leg["strike"]
                px = max(s - k, 0.0) if leg["option_type"] == "CE" else max(k - s, 0.0)
                notes.append(f"{leg['tradingsymbol']}: settled intrinsic {px:.2f}")
        if px is None:
            notes.append(f"{leg['tradingsymbol']}: NO QUOTE, mark degraded (used 0)")
            px = 0.0
        cash += (-px) if leg["action"] == "SELL" else (+px)
        fills.append((leg, px, settled))
    return cash, notes, fills


def _entry_value(legs) -> float:
    return sum((l["premium"] or 0) * (1 if l["action"] == "SELL" else -1) for l in legs)


def _open_trade(conn, strategy, rec, now_iso) -> int:
    legs = rec["legs"]
    ev = _entry_value(legs)
    lot_size = int(rec.get("lot_size") or 75)
    meta = {k: rec.get(k) for k in
            ("structure", "target_pct", "time_stop", "front_expiry", "back_expiry",
             "wing_offset", "debit_per_unit", "credit_per_unit", "margin_total")
            if rec.get(k) is not None}
    ecosts = _entry_costs(legs, lot_size * PAPER_LOTS)
    cur = conn.execute(
        "INSERT INTO paper_trades (strategy, direction, opened_ts, entry_context,"
        " lots, lot_size, legs_json, entry_value, expiry, meta_json, entry_costs) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (strategy, rec.get("direction"), now_iso, rec.get("context"), PAPER_LOTS,
         lot_size, json.dumps(legs), ev, str(rec.get("expiry") or ""),
         json.dumps(meta), ecosts))
    conn.commit()
    return cur.lastrowid


def _close_trade(conn, pos, exit_value, reason, now_iso, leg_fills=None):
    units = pos["lot_size"] * pos["lots"]
    gross = (pos["entry_value"] + exit_value) * units
    ecosts = pos.get("entry_costs")
    if ecosts is None:  # pre-cost-model trade: backfill from stored legs
        ecosts = _entry_costs(json.loads(pos["legs_json"]), units)
    xcosts = _exit_costs(leg_fills, units) if leg_fills else 0.0
    net = gross - ecosts - xcosts
    conn.execute(
        "UPDATE paper_trades SET status='closed', closed_ts=?, exit_reason=?,"
        " exit_value=?, gross_pnl=?, entry_costs=?, exit_costs=?, realized_pnl=?"
        " WHERE id=?",
        (now_iso, reason, exit_value, gross, ecosts, xcosts, net, pos["id"]))
    conn.commit()
    return net


def _exit_reason(strategy, pos, upnl_unit, rec, signal, today: date) -> Optional[str]:
    """Strategy exit rules. upnl_unit is per-unit unrealized P&L at the mark."""
    basis = abs(pos["entry_value"]) or 1e-9
    meta = json.loads(pos["meta_json"] or "{}")

    if strategy == "triple_calendar":
        if upnl_unit >= 0.08 * basis:
            return "target+8%"
        ts = meta.get("time_stop")
        if ts and today.isoformat() >= ts:
            return "time-stop(front-7d)"
        if upnl_unit <= -0.40 * basis:
            return "catastrophe-40%"
        return None
    if strategy == "batman":
        return "target+2%" if upnl_unit >= 0.02 * basis else None
    if strategy == "no_brainer":
        if upnl_unit >= 0.025 * basis:
            return "target+2.5%"
        if upnl_unit <= -0.03 * basis:
            return "stop-3%"
        return None
    if strategy in FULL_ROLL:
        if rec and rec.get("context") == "rollover":
            return "rollover-rebuild"
        return None
    if strategy in FLIP_ONLY:
        if signal and pos["direction"]:
            cur_dir = "bull" if "bull" in signal else ("bear" if "bear" in signal else None)
            if cur_dir and cur_dir != pos["direction"]:
                return "signal-flip"
        return None
    if strategy == "edb":
        # settle handled by the expired-legs path; nothing extra intraweek
        return None
    return None


def seed_from_payload(kite, payload: dict, conn) -> dict:
    """ONE-TIME book seeding (user instruction 2026-07-07): open paper
    positions for every strategy whose latest rec has a buildable structure
    but a non-actionable context (hold → 'late', or 'monitor') and no open
    position. Leg premiums are RE-QUOTED live so entries are at current
    prices; entry_context is 'seed-<original>' so later performance analysis
    can separate seeded entries from rule-timed ones."""
    now_iso = datetime.now(IST).isoformat()
    recs = payload.get("recommendations") or {}
    opened, skipped = [], []
    for strategy in sorted(recs):
        rec = recs[strategy]
        if not rec or rec.get("error") or rec.get("note") or not rec.get("legs"):
            skipped.append({"strategy": strategy, "why": "no buildable structure today"})
            continue
        pos = conn.execute(
            "SELECT id FROM paper_trades WHERE strategy=? AND status='open'",
            (strategy,)).fetchone()
        if pos:
            skipped.append({"strategy": strategy, "why": f"already open (#{pos['id']})"})
            continue
        legs = json.loads(json.dumps(rec["legs"]))
        quotes = _quote_legs(kite, legs)
        no_quote = []
        for l in legs:
            px = quotes.get(l["tradingsymbol"])
            if px is not None:
                l["premium"] = px
            else:
                no_quote.append(l["tradingsymbol"])
        rec2 = dict(rec)
        rec2["legs"] = legs
        rec2["context"] = f"seed-{rec.get('context') or 'unknown'}"
        tid = _open_trade(conn, strategy, rec2, now_iso)
        opened.append({"strategy": strategy, "id": tid,
                       "entry_value": round(_entry_value(legs), 2),
                       "requoted_live": True,
                       "no_quote_legs": no_quote or None})
    return {"as_of": now_iso, "opened": opened, "skipped": skipped}


def sync(kite, payload: dict, conn) -> dict:
    """Mark, exit, and open paper positions. Returns the data.json summary."""
    now = datetime.now(IST)
    now_iso, today = now.isoformat(), now.date()
    today_iso = today.isoformat()
    recs = payload.get("recommendations") or {}
    signals = payload.get("signals") or {}
    spot = (payload.get("instruments") or {}).get("nifty", {}).get("spot")

    summary = {"as_of": now_iso, "lots": PAPER_LOTS, "strategies": {}, "totals": {}}
    open_upnl = 0.0

    strategies = set(recs.keys()) | {
        r["strategy"] for r in conn.execute(
            "SELECT DISTINCT strategy FROM paper_trades").fetchall()}

    for strategy in sorted(strategies):
        rec = recs.get(strategy)
        rec_ok = bool(rec) and not rec.get("error") and not rec.get("note") and (rec.get("legs"))
        entry = {}

        pos = conn.execute(
            "SELECT * FROM paper_trades WHERE strategy=? AND status='open' "
            "ORDER BY id DESC LIMIT 1", (strategy,)).fetchone()

        if pos:
            pos = dict(pos)
            legs = json.loads(pos["legs_json"])
            units = pos["lot_size"] * pos["lots"]
            # lazy cost backfill for trades opened before the cost model
            if pos.get("entry_costs") is None:
                pos["entry_costs"] = _entry_costs(legs, units)
                conn.execute("UPDATE paper_trades SET entry_costs=? WHERE id=?",
                             (pos["entry_costs"], pos["id"]))
                conn.commit()
            leg_exps = _leg_expiries({"legs": legs, "expiry": pos["expiry"]})
            quotes = _quote_legs(kite, legs)
            mark, notes, fills = _close_cash(legs, leg_exps, quotes, conn, today_iso, spot)
            est_xcosts = _exit_costs(fills, units)
            gross_upnl_unit = pos["entry_value"] + mark
            # NET "if closed now": gross mark − entry costs − est. exit costs
            net_upnl = gross_upnl_unit * units - pos["entry_costs"] - est_xcosts
            conn.execute(
                "INSERT INTO paper_marks (trade_id, ts, mark_value, upnl, spot, note)"
                " VALUES (?,?,?,?,?,?)",
                (pos["id"], now_iso, mark, net_upnl, spot,
                 "; ".join(notes) if notes else None))
            conn.commit()

            # exits: expired legs force a settle-close; else strategy rules.
            # Strategy targets/stops are judged on the GROSS structure move
            # (the rule definitions predate the cost model); the ledger
            # records NET.
            reason = None
            if any(le and le < today_iso for le in leg_exps):
                reason = "expired-settle"
            if reason is None:
                reason = _exit_reason(strategy, pos, gross_upnl_unit,
                                      rec, signals.get(strategy), today)
            if reason:
                realized = _close_trade(conn, pos, mark, reason, now_iso, fills)
                entry["last_exit"] = {"reason": reason, "realized_rs": round(realized, 0),
                                      "closed_ts": now_iso}
                pos = None
            else:
                entry["open"] = {
                    "id": pos["id"], "opened_ts": pos["opened_ts"],
                    "direction": pos["direction"], "expiry": pos["expiry"],
                    "entry_value": round(pos["entry_value"], 2),
                    "mark_value": round(mark, 2),
                    "upnl_rs": round(net_upnl, 0),
                    "upnl_pct": round(100 * net_upnl / (abs(pos["entry_value"]) * units or 1e-9), 2),
                    "costs_rs": round(pos["entry_costs"] + est_xcosts, 0),
                    "mark_degraded": any("NO QUOTE" in n for n in notes),
                }
                open_upnl += net_upnl

        # entry: flat + actionable rec → open at quoted premiums
        if pos is None and rec_ok and rec.get("context") in ACTIONABLE:
            tid = _open_trade(conn, strategy, rec, now_iso)
            ev = _entry_value(rec["legs"])
            units = int(rec.get("lot_size") or 75) * PAPER_LOTS
            ecosts = _entry_costs(rec["legs"], units)
            entry["open"] = {
                "id": tid, "opened_ts": now_iso, "direction": rec.get("direction"),
                "expiry": str(rec.get("expiry") or ""),
                "entry_value": round(ev, 2), "mark_value": round(-ev, 2),
                "upnl_rs": round(-ecosts, 0), "upnl_pct": 0.0,
                "costs_rs": round(ecosts, 0), "opened_now": True,
            }

        # realized history
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) tot,"
            " SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins"
            " FROM paper_trades WHERE strategy=? AND status='closed'",
            (strategy,)).fetchone()
        entry["n_closed"] = row["n"]
        entry["wins"] = row["wins"] or 0
        entry["realized_rs"] = round(row["tot"], 0)
        summary["strategies"][strategy] = entry

    tot_realized = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl),0) FROM paper_trades WHERE status='closed'"
    ).fetchone()[0]
    summary["totals"] = {"realized_rs": round(tot_realized, 0),
                         "open_upnl_rs": round(open_upnl, 0)}
    return summary

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

What sync() does per strategy, in order:
  1. MARK the open position from live quotes (expired legs at intrinsic vs
     the expiry-day NIFTY close from the recorder's candles).
  2. EXIT if a rule fires — expired-settle first, then per strategy:
     GG/Panther/NK close on rollover context (rebuild reopens same run);
     batman +2%; no_brainer +2.5%/−3%; triple_calendar +8% of debit / hard
     time stop front−7d / −40% circuit; OT & GG-LEAPS on signal flip.
     Targets/stops fire on the GROSS structure move; the ledger records NET.
  3. If still open, ROLL the monthly hedge when due (PR #49): OT at T-4 from
     the hedge expiry, GG-LEAPS on the 18th (prior business day if weekend);
     catch-up safe; old hedge sold / next-monthly bought per each strategy's
     own selection rule; roll cash + costs fold into entry_value/entry_costs
     so `realized = entry_value + close_cash` holds across any number of
     rolls. Recorded in meta_json.hedge_rolls + a paper_marks note.
  4. OPEN a new position when flat and the rec is actionable.

Costs (PR #48): every executed leg-side pays brokerage ₹20/order, STT (0.1%
of sell premium; 0.125% of intrinsic when a LONG leg expires ITM), NSE txn
0.03503%, SEBI, GST 18%, buy-side stamp, and 0.5%-of-premium modeled
slippage. Expiry settlements place no order. realized_pnl is NET;
gross_pnl / entry_costs / exit_costs keep the breakdown.

Known limits (keep in mind when judging results):
- Marks only when compute.py runs (daily 15:16 IST scheduler / manual
  Refresh) — not minutely.
- Entries tagged entry_context='seed-late' are the one-time 2026-07-07
  book seeding (mid-trend entries at live quotes) — separate them from
  rule-timed entries in performance analysis.
- Triple Calendar's 4th-calendar wing-breach adjustment is NOT simulated;
  a breach rides to the time stop.
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
# Direction-flip strategies: the SHORT leg holds until the signal flips, but
# the monthly HEDGE leg rolls on its calendar day (simulated in _roll_hedge —
# OT at T-4 from the hedge expiry, GG-LEAPS on the 18th weekend-adjusted).
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


# ---- Monthly hedge rolls (OT / GG-LEAPS) ----

def _hedge_roll_due(strategy, legs, leg_exps, today: date) -> Optional[int]:
    """Index of the hedge (BUY) leg if its calendar roll is due, else None.
    OT: roll at T-4 from the hedge's expiry. GG-LEAPS: roll on the 18th of
    the hedge-expiry month (prior business day if the 18th is a weekend).
    Both are catch-up safe: a missed roll day stays due on the next sync."""
    if strategy not in FLIP_ONLY:
        return None
    for i, (l, le) in enumerate(zip(legs, leg_exps)):
        if l["action"] != "BUY" or not le:
            continue
        hexp = date.fromisoformat(le)
        if hexp < today:
            return None  # already expired — the settle path owns this
        if strategy == "ocean_treasure":
            if (hexp - today).days <= 4:
                return i
        else:  # gg_leaps
            rd = date(hexp.year, hexp.month, 18)
            while rd.weekday() >= 5:
                rd -= timedelta(days=1)
            if today >= rd:
                return i
        return None
    return None


def _monthly_expiry_after(expiries, after: date) -> Optional[date]:
    """Monthly expiry = the LAST listed expiry of its month. First one
    strictly after `after`."""
    bym = {}
    for e in expiries:
        k = (e.year, e.month)
        if k not in bym or e > bym[k]:
            bym[k] = e
    for m in sorted(bym.values()):
        if m > after:
            return m
    return None


def _roll_hedge(kite, conn, pos, legs, hedge_idx, leg_exps, now_iso) -> tuple:
    """Close the monthly hedge at quote, open next month's hedge per the
    strategy rule (GG-LEAPS: strike ~2% further-OTM from the short, 500s;
    OT: premium ₹20–50 within 700 pts, 500s, further-OTM). Roll cash and
    costs fold into the position basis (entry_value / entry_costs) so the
    final realized P&L includes every roll. Returns (ok, note)."""
    old = legs[hedge_idx]
    short = next((l for l in legs if l["action"] == "SELL"), None)
    if short is None:
        return False, "no short leg found"
    units = pos["lot_size"] * pos["lots"]
    opt_type = old["option_type"]
    old_exp = date.fromisoformat(leg_exps[hedge_idx])
    try:
        instruments = kite.instruments("NFO")
    except Exception as e:
        return False, f"instruments fetch failed: {e}"
    exps = sorted({i["expiry"] for i in instruments
                   if i.get("name") == "NIFTY" and i.get("expiry")
                   and i.get("instrument_type") in ("CE", "PE")})
    new_exp = _monthly_expiry_after(exps, old_exp)
    if new_exp is None:
        return False, "no next monthly expiry in instruments"
    chain = {int(i["strike"]): i for i in instruments
             if i.get("name") == "NIFTY" and i.get("expiry") == new_exp
             and i.get("instrument_type") == opt_type
             and int(i["strike"]) % 500 == 0}
    further_otm = (lambda k: k < short["strike"]) if opt_type == "PE" \
        else (lambda k: k > short["strike"])
    if pos["strategy"] == "gg_leaps":
        target = short["strike"] * (0.98 if opt_type == "PE" else 1.02)
        cands = sorted((k for k in chain if further_otm(k)),
                       key=lambda k: abs(k - target))[:2]
    else:  # ocean_treasure
        cands = [k for k in chain
                 if further_otm(k) and abs(k - short["strike"]) <= 700]
    if not cands:
        return False, "no candidate hedge strike on the further-OTM side"
    q = _quote_legs(kite, [old] + [{"tradingsymbol": chain[k]["tradingsymbol"]}
                                   for k in cands])
    old_px = q.get(old["tradingsymbol"])
    if old_px is None:
        return False, "no quote for outgoing hedge"
    best = None
    for k in cands:
        px = q.get(chain[k]["tradingsymbol"])
        if px is None or px <= 0:
            continue
        if pos["strategy"] == "gg_leaps":
            score = (abs(k - short["strike"] * (0.98 if opt_type == "PE" else 1.02)),)
        else:
            score = (0 if 20 <= px <= 50 else 1, abs(px - 35))
        if best is None or score < best[0]:
            best = (score, k, px)
    if best is None:
        return False, "no quoted hedge candidate"
    _, new_k, new_px = best

    roll_cash = old_px - new_px      # sell old hedge (+), buy new (−), per unit
    roll_costs = _exec_cost(False, old_px, units) + _exec_cost(True, new_px, units)
    new_leg = {"action": "BUY", "transaction_type": "BUY",
               "tradingsymbol": chain[new_k]["tradingsymbol"],
               "strike": int(new_k), "option_type": opt_type,
               "leg_expiry": str(new_exp), "premium": new_px,
               "quantity": pos["lot_size"]}
    note = (f"hedge rolled: {old['tradingsymbol']} sold @{old_px} → "
            f"{new_leg['tradingsymbol']} bought @{new_px} "
            f"(cash {roll_cash:+.2f}/unit, costs ₹{roll_costs:,.0f})")

    # pin explicit expiries on all legs so future parsing never guesses
    for l, le in zip(legs, leg_exps):
        if le and not l.get("leg_expiry"):
            l["leg_expiry"] = le
    legs[hedge_idx] = new_leg
    pos["entry_value"] += roll_cash
    pos["entry_costs"] = (pos.get("entry_costs") or 0.0) + roll_costs
    short_exp = next((l.get("leg_expiry") for l in legs if l["action"] == "SELL"), None)
    pos["expiry"] = f"short {short_exp}, hedge {new_exp}" if short_exp else pos["expiry"]
    meta = json.loads(pos["meta_json"] or "{}")
    meta.setdefault("hedge_rolls", []).append({
        "ts": now_iso, "old": old["tradingsymbol"], "old_px": old_px,
        "new": new_leg["tradingsymbol"], "new_px": new_px,
        "cash_per_unit": round(roll_cash, 2), "costs_rs": round(roll_costs, 0)})
    pos["meta_json"] = json.dumps(meta)
    conn.execute(
        "UPDATE paper_trades SET legs_json=?, entry_value=?, entry_costs=?,"
        " meta_json=?, expiry=? WHERE id=?",
        (json.dumps(legs), pos["entry_value"], pos["entry_costs"],
         pos["meta_json"], pos["expiry"], pos["id"]))
    conn.commit()
    return True, note


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
                # Monthly hedge roll (OT: T-4, GG-LEAPS: 18th) — a real trader
                # rolls only if staying in the trade, so this runs AFTER the
                # exit checks. On success, re-mark against the new legs.
                roll_note = None
                hidx = _hedge_roll_due(strategy, legs, leg_exps, today)
                if hidx is not None:
                    ok, roll_note = _roll_hedge(kite, conn, pos, legs, hidx,
                                                leg_exps, now_iso)
                    if ok:
                        leg_exps = _leg_expiries({"legs": legs, "expiry": pos["expiry"]})
                        quotes = _quote_legs(kite, legs)
                        mark, notes, fills = _close_cash(legs, leg_exps, quotes,
                                                         conn, today_iso, spot)
                        est_xcosts = _exit_costs(fills, units)
                        gross_upnl_unit = pos["entry_value"] + mark
                        net_upnl = gross_upnl_unit * units - pos["entry_costs"] - est_xcosts
                        conn.execute(
                            "INSERT INTO paper_marks (trade_id, ts, mark_value,"
                            " upnl, spot, note) VALUES (?,?,?,?,?,?)",
                            (pos["id"], now_iso, mark, net_upnl, spot, roll_note))
                        conn.commit()
                    else:
                        roll_note = f"HEDGE ROLL FAILED (will retry next sync): {roll_note}"
                entry["open"] = {
                    "id": pos["id"], "opened_ts": pos["opened_ts"],
                    "direction": pos["direction"], "expiry": pos["expiry"],
                    "entry_value": round(pos["entry_value"], 2),
                    "mark_value": round(mark, 2),
                    "upnl_rs": round(net_upnl, 0),
                    "upnl_pct": round(100 * net_upnl / (abs(pos["entry_value"]) * units or 1e-9), 2),
                    "costs_rs": round(pos["entry_costs"] + est_xcosts, 0),
                    "mark_degraded": any("NO QUOTE" in n for n in notes),
                    "hedge_roll": roll_note,
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

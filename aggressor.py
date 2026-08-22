"""Tick-level bid/ask aggressor classification (KiteTicker daemon).

WHY THIS EXISTS. The OI Flow tab classifies traded volume by the direction the
price moved over a one-MINUTE bar. That is a proxy: within a minute an uptick
usually means buyers lifted the offer, but a minute containing two-way trade
gets collapsed to a single label, and on a thin strike the label is noise.

This module measures the thing the proxy approximates — for each trade, was the
aggressor the buyer (hit the ask) or the seller (hit the bid). Doing that needs
the quote that was standing BEFORE the trade, which only a tick feed carries.

METHOD (Lee-Ready, the standard). Keep the previous tick's best bid/ask per
instrument; classify the current tick's last_price against that standing quote.
Trades at or above the ask are buyer-aggressive, at or below the bid are
seller-aggressive, and anything strictly inside the spread is UNREADABLE and is
counted as such rather than guessed. That is why a print total and a "readable"
total differ, and both are reported.

HONEST LIMITS, because this looks more precise than it is:
  * Kite ticks are SNAPSHOTS, not every trade. Between two ticks the volume
    counter may have advanced by several trades on both sides; we attribute the
    whole delta to the side the latest print indicates. On an active strike
    that is a good approximation, on a quiet one it is lumpy.
  * A midpoint print is genuinely ambiguous and stays unreadable. We do not
    fall back to a tick-test to manufacture a side.
  * Aggressor side says who was impatient. It does NOT say who was opening or
    closing, nor which side was the customer. Pair it with OI change (which the
    chain recorder already stores) to infer a fresh build.

DEPLOYMENT. This runs as its own process, not inside the web app: a websocket
that must stay connected all session does not belong in a request-serving
worker, and a reconnect storm should not take the dashboard with it. SQLite is
in WAL mode, so this writing while the recorder writes is safe.

    python3 aggressor.py            # runs until the closing bell

The classification is pure and unit-tested; the live socket path cannot be
exercised outside market hours, so it is written defensively and logs loudly.
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from datetime import datetime

import db
import recorder

BUY, SELL, UNKNOWN = "buy", "sell", "unknown"
FLUSH_SECONDS = 20          # how often partial minutes are written out


def classify(last_price: float, bid: float | None, ask: float | None) -> str:
    """Lee-Ready against the STANDING quote. Pure — this is the testable core.

    A print inside the spread is unreadable and says so; inventing a side there
    is how a 50/50 gets reported as conviction.
    """
    if not last_price or bid is None or ask is None or bid <= 0 or ask <= 0:
        return UNKNOWN
    if ask < bid:                      # crossed/stale book — do not guess
        return UNKNOWN
    if last_price >= ask:
        return BUY
    if last_price <= bid:
        return SELL
    return UNKNOWN


def _best(depth: dict, side: str):
    try:
        lvl = (depth or {}).get(side) or []
        p = lvl[0].get("price") if lvl else None
        return p if p and p > 0 else None
    except Exception:
        return None


class Accumulator:
    """Per-(token, minute) aggressor totals, driven by volume deltas."""

    def __init__(self):
        self.prev = {}                                  # token -> (bid, ask, volume)
        self.bins = defaultdict(lambda: {"buy": 0, "sell": 0, "unknown": 0,
                                         "ticks": 0, "last": 0.0})

    def on_tick(self, t: dict) -> None:
        tok = t.get("instrument_token")
        ltp = t.get("last_price")
        vol = t.get("volume_traded")
        if tok is None or ltp is None:
            return
        depth = t.get("depth") or {}
        bid, ask = _best(depth, "buy"), _best(depth, "sell")
        pbid, pask, pvol = self.prev.get(tok, (None, None, None))
        # volume traded since the previous tick — the quantity to attribute
        dv = 0
        if vol is not None and pvol is not None and vol >= pvol:
            dv = vol - pvol
        elif vol is not None and pvol is None:
            dv = 0                                       # first tick: no baseline
        side = classify(ltp, pbid, pask)
        ts = (t.get("exchange_timestamp") or datetime.now(recorder.IST))
        key = (tok, ts.strftime("%Y-%m-%dT%H:%M:00"))
        b = self.bins[key]
        b["ticks"] += 1
        b["last"] = ltp
        if dv > 0:
            b[side] += dv
        self.prev[tok] = (bid, ask, vol)

    def drain(self, keep_current: bool = True) -> list:
        """Return finished minutes as rows; keep the in-progress one if asked."""
        now_min = datetime.now(recorder.IST).strftime("%Y-%m-%dT%H:%M:00")
        out, keep = [], {}
        for (tok, minute), b in self.bins.items():
            if keep_current and minute == now_min:
                keep[(tok, minute)] = b
                continue
            readable = b["buy"] + b["sell"]
            out.append({
                "ts": minute, "instrument_token": tok,
                "buy_units": b["buy"], "sell_units": b["sell"],
                "unknown_units": b["unknown"], "ticks": b["ticks"],
                "last_price": b["last"],
                "readable_units": readable,
                "buy_pct": round(b["buy"] / readable * 100, 1) if readable else None,
            })
        self.bins = defaultdict(lambda: {"buy": 0, "sell": 0, "unknown": 0,
                                         "ticks": 0, "last": 0.0}, keep)
        return out


def store(conn, rows: list) -> int:
    if not rows:
        return 0
    cols = ("ts", "instrument_token", "buy_units", "sell_units", "unknown_units",
            "readable_units", "buy_pct", "ticks", "last_price")
    conn.executemany(
        f"INSERT OR REPLACE INTO aggressor_minute ({','.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})",
        [tuple(r.get(c) for c in cols) for r in rows])
    conn.commit()
    return len(rows)


def tracked_tokens(kite) -> dict:
    """The instruments worth a tick subscription: the same ATM+-10 options the
    chain recorder stores, so the two views describe the same contracts."""
    instruments = recorder._get_instruments(kite)
    tokens = {}
    for name in ("NIFTY", "BANKNIFTY"):
        exp = recorder._nearest_expiry(instruments, name)
        if not exp:
            continue
        try:
            spot = kite.ltp([f"NSE:{'NIFTY 50' if name == 'NIFTY' else 'NIFTY BANK'}"])
            spot = list(spot.values())[0]["last_price"]
        except Exception:
            continue
        opts = [i for i in instruments if i.get("name") == name
                and i.get("expiry") == exp and i.get("instrument_type") in ("CE", "PE")]
        strikes = sorted({float(i["strike"]) for i in opts})
        if not strikes:
            continue
        atm = min(strikes, key=lambda k: abs(k - spot))
        i0 = strikes.index(atm)
        band = set(strikes[max(0, i0 - 10): i0 + 11])
        for i in opts:
            if float(i["strike"]) in band:
                tokens[i["instrument_token"]] = {
                    "tradingsymbol": i["tradingsymbol"], "name": name,
                    "strike": float(i["strike"]), "opt_type": i["instrument_type"],
                    "expiry": str(exp)}
    return tokens


def run_daemon() -> int:
    """Connect, subscribe in FULL mode, and write minute rows until the close."""
    from kiteconnect import KiteTicker
    from kite_auth import get_kite_from_cache, load_env, read_cached_session

    kite = get_kite_from_cache()
    sess = read_cached_session()
    if kite is None or not sess:
        print("[aggressor] no Kite session — nothing to do")
        return 2
    api_key, _ = load_env()
    tokens = tracked_tokens(kite)
    if not tokens:
        print("[aggressor] no instruments resolved")
        return 3
    print(f"[aggressor] subscribing to {len(tokens)} contracts")

    acc = Accumulator()
    with db.get_conn() as conn:
        db.store_aggressor_meta(conn, tokens)

    kws = KiteTicker(api_key, sess["access_token"])
    state = {"last_flush": time.time(), "rows": 0}

    def on_ticks(ws, ticks):
        for t in ticks:
            try:
                acc.on_tick(t)
            except Exception as e:
                print(f"[aggressor] tick error {type(e).__name__}: {e}")
        if time.time() - state["last_flush"] >= FLUSH_SECONDS:
            state["last_flush"] = time.time()
            rows = acc.drain()
            if rows:
                try:
                    with db.get_conn() as conn:
                        state["rows"] += store(conn, rows)
                except Exception as e:
                    print(f"[aggressor] store failed {type(e).__name__}: {e}")
        if not recorder.is_market_hours():
            print(f"[aggressor] market closed — {state['rows']} minute-rows written")
            try:
                with db.get_conn() as conn:
                    store(conn, acc.drain(keep_current=False))
            finally:
                ws.stop()

    def on_connect(ws, response):
        ws.subscribe(list(tokens))
        ws.set_mode(ws.MODE_FULL, list(tokens))       # FULL carries depth
        print("[aggressor] connected, mode=FULL")

    def on_error(ws, code, reason):
        print(f"[aggressor] socket error {code}: {reason}")

    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_error = on_error
    kws.connect()                                      # blocks
    return 0


if __name__ == "__main__":
    sys.exit(run_daemon())

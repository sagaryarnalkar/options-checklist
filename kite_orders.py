"""Real Zerodha orders from the HeroZero terminal, on the dashboard's own Kite login.

The terminal (optionhub/, Mr Chartist's Option Hub) has order screens: its order ticket, the
Strategy Builder / option-chain baskets and the 1Cliq keys. Its own Zerodha adapter never gets a
Kite token. In dashboard mode those screens send ORDER INTENTS to its proxy
(optionhub/server/dashboard.mjs), which forwards them here. This module is the only code that
turns one into a real Kite order, and every rule below is checked HERE, whatever the screen
already checked.

WHO CAN CALL (app.py `_hub_orders_auth`)
  * X-Hub-Feed = the hub cookie token (the same secret as the read-only feed), AND
  * the request came straight from loopback, not through Caddy. Caddy always adds
    X-Forwarded-For; the hub proxy calls 127.0.0.1:8000 directly. A leaked token alone cannot
    place an order from the internet. The browser reaches the proxy only through Caddy's hub
    cookie lock.

THE RULES (owner's choices, 2026-11-18)
  * KITE_ORDERS=on in .env, and the halt switch (DATA_DIR/.kite_orders_halt) off. Halt blocks
    new orders and modifications; cancelling is always allowed (it only reduces risk).
  * NFO only: NIFTY and BANKNIFTY options and futures, today's expiry or later.
  * Quantity = lots × lot_size from Kite's own instrument dump, never from the client.
    1 ≤ lots ≤ 25 per order.
  * Order types LIMIT / SL / MARKET / SL-M; products NRML / MIS; validity DAY / IOC.
    MARKET and SL-M carry market_protection=-1 (Kite's automatic protection; exchanges reject
    unprotected API market orders).
  * Prices positive and on the instrument's tick. A LIMIT price or SL trigger must sit inside a
    fat-finger band around LTP: options ±max(50%, ₹20), futures ±3%. A 10× typo is refused;
    a deliberate deep limit is still possible within the band.
  * At most MAX_ORDERS_PER_DAY placed per IST day, at least MIN_GAP_S apart, one at a time.
    SEBI's no-registration line for self-built automation is 10 orders/second.
  * Idempotent: the client's intent id (6–20 alphanumerics) is the order's Kite TAG and a
    UNIQUE key in kite_order_log. A repeat returns the first outcome and sends nothing. If the
    Kite call raises (timeout, connection drop), the day's order book is searched for the tag
    BEFORE we report failure, so a place that did reach Kite is never reported as failed.

STATIC IP. Since 1 Apr 2026 Kite accepts order placement, modification and cancellation only
from the IP registered on the Kite Connect app (one change per week). Reads (order book, LTP,
margins) work from any IP. Order calls are forced onto IPv4: api.kite.trade also answers on
IPv6, and a droplet with IPv6 would otherwise leave from an address that is not registered.

Every attempt (refused ones too) is written to kite_order_log. Nothing here ever logs a token.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime
from typing import Optional

import db
import recorder

IST = recorder.IST

UNDERLYINGS = ("NIFTY", "BANKNIFTY")
INSTRUMENT_TYPES = ("CE", "PE", "FUT")
MAX_LOTS = 25
ORDER_TYPES = ("LIMIT", "SL", "MARKET", "SL-M")
MODIFY_ORDER_TYPES = ("LIMIT", "SL")
PRODUCTS = ("NRML", "MIS")
VALIDITIES = ("DAY", "IOC")
SIDES = ("BUY", "SELL")
MAX_ORDERS_PER_DAY = 300
MIN_GAP_S = 0.15
OPT_BAND_PCT, OPT_BAND_ABS = 0.50, 20.0
FUT_BAND_PCT = 0.03
MAX_BASKET = 12
INTENT_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")
ORDER_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,40}$")
OPEN_STATUSES = {"OPEN", "TRIGGER PENDING", "OPEN PENDING", "VALIDATION PENDING", "PUT ORDER REQ RECEIVED",
                 "MODIFY PENDING", "MODIFY VALIDATION PENDING", "AMO REQ RECEIVED"}
SOURCES = ("ticket", "basket", "oneclick", "orders")


class OrderRefused(Exception):
    """A refusal BEFORE anything reached Kite (or Kite's own refusal, verbatim)."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


_lock = threading.Lock()                  # one order call at a time
_last_sent = {"at": 0.0}
_index_cache: dict = {"src": None, "by_symbol": {}}
_kite_cache: dict = {"token": None, "kite": None}


# ------------------------------------------------------------------ switches --

def halt_file():
    return db.DATA_DIR / ".kite_orders_halt"


def enabled() -> bool:
    return os.environ.get("KITE_ORDERS", "").strip().lower() in ("on", "1", "true", "yes")


def halted() -> bool:
    return halt_file().exists()


def set_halt(on: bool) -> bool:
    f = halt_file()
    if on:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(datetime.now(IST).isoformat(timespec="seconds"))
    elif f.exists():
        f.unlink()
    _log("halt", None, "orders", {"halted": on}, "sent", None, "halted" if on else "resumed")
    return halted()


# ------------------------------------------------------------------- Kite --

def _ipv4_session():
    """requests.Session whose connections leave over IPv4 only (binding an IPv4 source makes
    socket.create_connection skip the AAAA answers)."""
    import requests
    from requests.adapters import HTTPAdapter

    class _IPv4Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["source_address"] = ("0.0.0.0", 0)
            return super().init_poolmanager(*args, **kwargs)

    s = requests.Session()
    s.mount("https://", _IPv4Adapter())
    return s


def _kite():
    """KiteConnect on the dashboard's cached session, with an IPv4-only HTTP session."""
    import hub_feed
    try:
        base = hub_feed._kite()
    except hub_feed.FeedRefused as e:
        raise OrderRefused("NOT_LOGGED_IN", "The dashboard is not logged in to Kite today. "
                                            "Log in on the dashboard, then try again.", 403) from None
    tok = base.access_token
    if _kite_cache["token"] != tok:
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=base.api_key)
        k.set_access_token(tok)
        k.reqsession = _ipv4_session()
        _kite_cache.update(token=tok, kite=k)
    return _kite_cache["kite"]


def _kite_message(e: Exception) -> str:
    """Kite's own words for a refusal (its exceptions carry the API message)."""
    msg = str(getattr(e, "message", "") or e).strip()
    return msg[:300] or e.__class__.__name__


def _exp_iso(v) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)[:10]


def _index(kite) -> dict:
    """NIFTY/BANKNIFTY options and futures from the recorder's daily NFO dump, by tradingsymbol."""
    ins = recorder._get_instruments(kite)
    if _index_cache["src"] is not ins:
        today = datetime.now(IST).date().isoformat()
        by_symbol = {}
        for r in ins:
            if r.get("name") not in UNDERLYINGS or r.get("instrument_type") not in INSTRUMENT_TYPES:
                continue
            if _exp_iso(r.get("expiry")) < today:
                continue
            by_symbol[r["tradingsymbol"]] = r
        _index_cache.update(src=ins, by_symbol=by_symbol)
    return _index_cache["by_symbol"]


# ------------------------------------------------------------ pure checks --

def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def resolve(by_symbol: dict, leg: dict) -> dict:
    """The instrument row for a leg: by tradingsymbol, else by (underlying, expiry, strike, type)."""
    ts = str(leg.get("tradingsymbol") or "").strip().upper()
    if ts:
        row = by_symbol.get(ts)
        if not row:
            raise OrderRefused("NOT_ALLOWED", f"{ts} is not a NIFTY or BANKNIFTY option or future "
                                              "that is still trading. Only those can be ordered here.")
        return row
    und = str(leg.get("underlying") or "").strip().upper()
    if und not in UNDERLYINGS:
        raise OrderRefused("NOT_ALLOWED", f"Only {' and '.join(UNDERLYINGS)} can be ordered here"
                                          f"{f' (not {und})' if und else ''}.")
    typ = str(leg.get("opt_type") or "").strip().upper()
    if typ not in INSTRUMENT_TYPES:
        raise OrderRefused("BAD_ORDER", "Contract type must be CE, PE or FUT.")
    exp = str(leg.get("expiry") or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", exp):
        raise OrderRefused("BAD_ORDER", "Expiry must be YYYY-MM-DD.")
    strike = _num(leg.get("strike")) if typ != "FUT" else 0.0
    if typ != "FUT" and not (strike and strike > 0):
        raise OrderRefused("BAD_ORDER", "Strike missing.")
    for row in by_symbol.values():
        if (row.get("name") == und and row.get("instrument_type") == typ
                and _exp_iso(row.get("expiry")) == exp
                and (typ == "FUT" or abs(float(row.get("strike") or 0) - strike) < 1e-6)):
            return row
    label = f"{und} {exp} {'FUT' if typ == 'FUT' else f'{strike:g} {typ}'}"
    raise OrderRefused("UNKNOWN_CONTRACT", f"No tradable contract {label} in Kite's instrument list.")


def band(inst: dict, ltp: Optional[float]) -> Optional[tuple]:
    """(low, high) a LIMIT price or SL trigger must sit in, or None without a usable LTP."""
    if not ltp or ltp <= 0:
        return None
    if inst.get("instrument_type") == "FUT":
        w = ltp * FUT_BAND_PCT
    else:
        w = max(ltp * OPT_BAND_PCT, OPT_BAND_ABS)
    return max(0.0, ltp - w), ltp + w


def _on_tick(price: float, tick: float) -> bool:
    if tick <= 0:
        return True
    n = price / tick
    return abs(n - round(n)) < 1e-6


def check_leg(leg: dict, inst: dict, ltp: Optional[float]) -> dict:
    """Validate one leg against the rules; return the exact kwargs for kite.place_order.

    Pure (no I/O): `inst` is the instrument-dump row, `ltp` the last price (None = unknown)."""
    side = str(leg.get("side") or "").upper()
    if side not in SIDES:
        raise OrderRefused("BAD_ORDER", "Side must be BUY or SELL.")
    otype = str(leg.get("order_type") or "").upper()
    if otype not in ORDER_TYPES:
        raise OrderRefused("BAD_ORDER", f"Order type must be one of {', '.join(ORDER_TYPES)}.")
    product = str(leg.get("product") or "NRML").upper()
    if product not in PRODUCTS:
        raise OrderRefused("BAD_ORDER", f"Product must be {' or '.join(PRODUCTS)}.")
    validity = str(leg.get("validity") or "DAY").upper()
    if validity not in VALIDITIES:
        raise OrderRefused("BAD_ORDER", f"Validity must be {' or '.join(VALIDITIES)}.")
    lots = leg.get("lots")
    if isinstance(lots, bool) or not isinstance(lots, int) or lots < 1:
        raise OrderRefused("BAD_ORDER", "Lots must be a whole number of at least 1.")
    if lots > MAX_LOTS:
        raise OrderRefused("TOO_MANY_LOTS", f"{lots} lots is over the {MAX_LOTS}-lot limit per order.")
    lot = int(inst.get("lot_size") or 0)
    if lot <= 0:
        raise OrderRefused("NO_LOT_SIZE", f"Kite lists no lot size for {inst.get('tradingsymbol')}.")
    tick = float(inst.get("tick_size") or 0.05)
    qty = lots * lot
    if "quantity" in leg and leg["quantity"] is not None and _num(leg["quantity"]) != qty:
        raise OrderRefused("LOT_SIZE_MISMATCH",
                           f"The screen asked for {leg['quantity']} units; {lots} lot(s) of "
                           f"{inst.get('tradingsymbol')} is {qty} (lot {lot}). Nothing was sent.")
    out = {"variety": "regular", "exchange": "NFO", "tradingsymbol": inst["tradingsymbol"],
           "transaction_type": side, "quantity": qty, "product": product,
           "order_type": otype, "validity": validity}
    rng = band(inst, ltp)

    def _priced(name: str) -> float:
        v = _num(leg.get(name))
        if v is None or v <= 0:
            raise OrderRefused("BAD_PRICE", f"{otype} orders need a {name.replace('_', ' ')} above zero.")
        if not _on_tick(v, tick):
            raise OrderRefused("BAD_PRICE", f"{name.replace('_', ' ').capitalize()} {v:g} is not on the "
                                            f"₹{tick:g} tick.")
        if rng and not (rng[0] <= v <= rng[1]):
            raise OrderRefused("PRICE_BAND",
                               f"{name.replace('_', ' ').capitalize()} {v:g} is too far from the last "
                               f"price {ltp:g} (allowed {rng[0]:.2f}–{rng[1]:.2f}). A typo? Nothing was sent.")
        return v

    if otype in ("LIMIT", "SL"):
        out["price"] = _priced("price")
    if otype in ("SL", "SL-M"):
        out["trigger_price"] = _priced("trigger_price")
    if otype in ("MARKET", "SL-M"):
        out["market_protection"] = -1
    if otype == "SL":
        p, t = out["price"], out["trigger_price"]
        if (side == "BUY" and p < t) or (side == "SELL" and p > t):
            raise OrderRefused("BAD_PRICE", f"A {side} stop-loss limit must have its price "
                                            f"{'at or above' if side == 'BUY' else 'at or below'} the trigger.")
    if leg.get("tag"):
        out["tag"] = str(leg["tag"])
    return out


def leg_summary(inst: dict, params: dict) -> dict:
    lot = int(inst.get("lot_size") or 0)
    return {"tradingsymbol": params["tradingsymbol"], "exchange": "NFO",
            "underlying": inst.get("name"), "expiry": _exp_iso(inst.get("expiry")),
            "strike": float(inst.get("strike") or 0), "opt_type": inst.get("instrument_type"),
            "side": params["transaction_type"], "order_type": params["order_type"],
            "product": params["product"], "validity": params["validity"],
            "lot_size": lot, "lots": params["quantity"] // lot if lot else None,
            "quantity": params["quantity"], "price": params.get("price"),
            "trigger_price": params.get("trigger_price"), "tick_size": float(inst.get("tick_size") or 0.05)}


# --------------------------------------------------------------------- log --

def _log(action: str, intent: Optional[str], source: Optional[str], req: dict, outcome: str,
         order_id: Optional[str], message: Optional[str]) -> Optional[int]:
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO kite_order_log (ts, action, intent, source, request_json, outcome, order_id, message) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (datetime.now(IST).isoformat(timespec="milliseconds"), action, intent, source,
                 json.dumps(req, default=str)[:4000], outcome, order_id, (message or "")[:500]))
            conn.commit()
            return cur.lastrowid
    except Exception as e:  # the log must never be why an order did or did not go
        print(f"  ⚠️ kite_order_log write failed: {e}")
        return None


def _update(row_id: Optional[int], outcome: str, order_id: Optional[str], message: Optional[str]) -> None:
    if not row_id:
        return
    try:
        with db.get_conn() as conn:
            conn.execute("UPDATE kite_order_log SET outcome=?, order_id=?, message=? WHERE id=?",
                         (outcome, order_id, (message or "")[:500], row_id))
            conn.commit()
    except Exception as e:
        print(f"  ⚠️ kite_order_log update failed: {e}")


def _claim(action: str, intent: str, source: str, req: dict) -> tuple:
    """Insert the 'sending' row for (action, intent). Returns (row_id, None), or (None, previous)
    when the intent was already used — the caller answers with the previous outcome."""
    try:
        with db.get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO kite_order_log (ts, action, intent, source, request_json, outcome) "
                "VALUES (?,?,?,?,?, 'sending')",
                (datetime.now(IST).isoformat(timespec="milliseconds"), action, intent, source,
                 json.dumps(req, default=str)[:4000]))
            conn.commit()
            return cur.lastrowid, None
    except Exception as e:
        if "UNIQUE" not in str(e).upper():
            raise OrderRefused("LOG_UNAVAILABLE", f"Order log unavailable ({e}); nothing was sent.", 503)
    with db.get_conn() as conn:
        row = conn.execute("SELECT outcome, order_id, message FROM kite_order_log "
                           "WHERE action=? AND intent=?", (action, intent)).fetchone()
    return None, {"outcome": row[0], "order_id": row[1], "message": row[2]} if row else {}


def sent_today() -> int:
    day = datetime.now(IST).date().isoformat()
    with db.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM kite_order_log WHERE action='place' AND outcome='sent' "
                            "AND ts >= ?", (day,)).fetchone()[0]


def recent(limit: int = 50) -> list:
    with db.get_conn() as conn:
        rows = conn.execute("SELECT ts, action, intent, source, outcome, order_id, message FROM kite_order_log "
                            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    keys = ("ts", "action", "intent", "source", "outcome", "order_id", "message")
    return [dict(zip(keys, r)) for r in rows]


# --------------------------------------------------------------- guards --

def _require_on(for_cancel: bool = False) -> None:
    if not enabled():
        raise OrderRefused("ORDERS_OFF", "Live orders are switched off on the server "
                                         "(KITE_ORDERS is not 'on' in .env).", 403)
    if halted() and not for_cancel:
        raise OrderRefused("HALTED", "Live orders are HALTED (the halt switch on the Orders page). "
                                     "Cancelling still works.", 403)


def _intent(v) -> str:
    s = str(v or "")
    if not INTENT_RE.fullmatch(s):
        raise OrderRefused("BAD_INTENT", "Every order needs a fresh intent id (6–20 letters/digits).", 400)
    return s


def _pace() -> None:
    gap = MIN_GAP_S - (time.monotonic() - _last_sent["at"])
    if gap > 0:
        time.sleep(gap)


def _ltps(kite, symbols: list) -> dict:
    if not symbols:
        return {}
    try:
        q = kite.ltp([f"NFO:{s}" for s in symbols])
    except Exception:
        return {}
    return {k.split(":", 1)[1]: _num(v.get("last_price")) for k, v in (q or {}).items()}


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_jsonable(v) for v in o]
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    return o


# ------------------------------------------------------------------ API --

def status() -> dict:
    out = {"ok": True, "enabled": enabled(), "halted": halted(), "logged_in": None,
           "rules": {"underlyings": list(UNDERLYINGS), "instrument_types": list(INSTRUMENT_TYPES),
                     "exchange": "NFO", "max_lots": MAX_LOTS, "order_types": list(ORDER_TYPES),
                     "products": list(PRODUCTS), "validities": list(VALIDITIES),
                     "max_orders_per_day": MAX_ORDERS_PER_DAY, "max_basket_legs": MAX_BASKET,
                     "band": {"option_pct": OPT_BAND_PCT * 100, "option_min_rupees": OPT_BAND_ABS,
                              "future_pct": FUT_BAND_PCT * 100}},
           "static_ip_note": "Kite accepts orders only from the IP registered on your Kite Connect app "
                             "(IP Whitelist). Order calls leave this server over IPv4."}
    try:
        out["sent_today"] = sent_today()
    except Exception:
        out["sent_today"] = None
    try:
        _kite()
        out["logged_in"] = True
    except OrderRefused:
        out["logged_in"] = False
    return out


def preview(legs: list) -> dict:
    """Resolve and check legs WITHOUT placing anything: exact contracts, quantities, LTP, the
    rule verdict per leg and (best effort) Kite's own margin for the whole basket."""
    if not isinstance(legs, list) or not legs:
        raise OrderRefused("BAD_ORDER", "No legs to preview.", 400)
    if len(legs) > MAX_BASKET:
        raise OrderRefused("BAD_ORDER", f"At most {MAX_BASKET} legs per basket.", 400)
    kite = _kite()
    idx = _index(kite)
    rows, insts = [], []
    for leg in legs:
        try:
            insts.append(resolve(idx, leg if isinstance(leg, dict) else {}))
        except OrderRefused as e:
            insts.append(None)
            rows.append({"ok": False, "code": e.code, "message": e.message})
            continue
        rows.append(None)
    ltp = _ltps(kite, [i["tradingsymbol"] for i in insts if i])
    params = []
    for k, (leg, inst) in enumerate(zip(legs, insts)):
        if not inst:
            continue
        p = ltp.get(inst["tradingsymbol"])
        try:
            prm = check_leg(leg, inst, p)
            params.append(prm)
            rows[k] = {"ok": True, **leg_summary(inst, prm), "ltp": p, "band": band(inst, p)}
        except OrderRefused as e:
            rows[k] = {"ok": False, "code": e.code, "message": e.message, "tradingsymbol": inst["tradingsymbol"],
                       "lot_size": int(inst.get("lot_size") or 0), "tick_size": float(inst.get("tick_size") or 0.05),
                       "ltp": p, "band": band(inst, p)}
    margin = None
    if params and all(r and r.get("ok") for r in rows):
        try:
            m = kite.basket_order_margins([{
                "exchange": "NFO", "tradingsymbol": x["tradingsymbol"], "transaction_type": x["transaction_type"],
                "variety": "regular", "product": x["product"], "order_type": x["order_type"],
                "quantity": x["quantity"], "price": x.get("price", 0), "trigger_price": x.get("trigger_price", 0),
            } for x in params], consider_positions=True, mode="compact")
            margin = {"initial": _num((m.get("initial") or {}).get("total")),
                      "final": _num((m.get("final") or {}).get("total"))}
        except Exception as e:
            margin = {"error": _kite_message(e)}
    return {"ok": all(r and r.get("ok") for r in rows), "legs": rows, "margin": margin,
            "enabled": enabled(), "halted": halted()}


def _send_place(kite, params: dict):
    """kite.place_order, or its raw POST when this kiteconnect predates market_protection."""
    import inspect
    sig = inspect.signature(kite.place_order).parameters
    takes_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.values())
    if "market_protection" in params and "market_protection" not in sig and not takes_any:
        body = {k: v for k, v in params.items() if k != "variety" and v is not None}
        return kite._post("order.place", url_args={"variety": params["variety"]}, params=body)["order_id"]
    return kite.place_order(**params)


def _reconcile_by_tag(kite, tag: str) -> Optional[dict]:
    try:
        for o in kite.orders() or []:
            if str(o.get("tag") or "") == tag or tag in (o.get("tags") or []):
                return o
    except Exception:
        return None
    return None


def place(intent: str, source: str, leg: dict) -> dict:
    """Place ONE order. Returns {"ok": True, "order_id", ...} or raises OrderRefused."""
    src = source if source in SOURCES else "ticket"
    req = {"leg": leg}
    try:
        if not isinstance(leg, dict):
            raise OrderRefused("BAD_ORDER", "The order must be a JSON object.", 400)
        intent = _intent(intent)
        _require_on()
    except OrderRefused as e:
        _log("place", None, src, req, "refused", None, f"{e.code}: {e.message}")
        raise
    row_id, prev = _claim("place", intent, src, req)
    if prev is not None:
        if prev.get("outcome") == "sent":
            return {"ok": True, "order_id": prev.get("order_id"), "duplicate": True,
                    "message": "Already placed under this intent; nothing new was sent."}
        if prev.get("outcome") == "sending":
            raise OrderRefused("IN_FLIGHT", "This order is still being sent. Check the order book.", 409)
        raise OrderRefused("INTENT_USED", f"This attempt already ended ({prev.get('outcome')}: "
                                          f"{prev.get('message') or 'no message'}). Place it again as a new order.", 409)
    try:
        with _lock:
            if sent_today() >= MAX_ORDERS_PER_DAY:
                raise OrderRefused("DAILY_LIMIT", f"{MAX_ORDERS_PER_DAY} orders already placed today.", 429)
            kite = _kite()
            inst = resolve(_index(kite), leg if isinstance(leg, dict) else {})
            ltp = _ltps(kite, [inst["tradingsymbol"]]).get(inst["tradingsymbol"])
            params = check_leg({**leg, "tag": intent}, inst, ltp)
            _pace()
            try:
                order_id = _send_place(kite, params)
            except Exception as e:
                _last_sent["at"] = time.monotonic()
                found = _reconcile_by_tag(kite, intent)
                if found and found.get("order_id"):
                    order_id = str(found["order_id"])
                else:
                    msg = _kite_message(e)
                    _update(row_id, "error", None, msg)
                    raise OrderRefused("KITE_REFUSED", f"Kite: {msg}", 502) from None
            _last_sent["at"] = time.monotonic()
    except OrderRefused as e:
        if e.code != "KITE_REFUSED":
            _update(row_id, "refused", None, f"{e.code}: {e.message}")
        raise
    except Exception as e:  # unexpected: record and refuse loudly
        _update(row_id, "error", None, repr(e)[:300])
        raise OrderRefused("SERVER_ERROR", f"Order not confirmed: {e}. Check the order book.", 500) from None
    _update(row_id, "sent", str(order_id), None)
    print(f"  🟠 KITE ORDER {params['transaction_type']} {params['quantity']} {params['tradingsymbol']} "
          f"{params['order_type']} {params.get('price', '')} → {order_id} [{src} {intent}]")
    return {"ok": True, "order_id": str(order_id), **leg_summary(inst, params), "ltp": ltp,
            "message": "Sent to Kite. Its status is not known yet — the order book will show it."}


def _current(kite, order_id: str) -> dict:
    if not ORDER_ID_RE.fullmatch(str(order_id or "")):
        raise OrderRefused("BAD_ORDER", "Invalid order id.", 400)
    try:
        hist = kite.order_history(order_id)
    except Exception as e:
        raise OrderRefused("UNKNOWN_ORDER", f"Kite: {_kite_message(e)}", 404) from None
    if not hist:
        raise OrderRefused("UNKNOWN_ORDER", "Kite has no such order today.", 404)
    return hist[-1]


def modify(intent: str, order_id: str, changes: dict) -> dict:
    """Change price / trigger / lots / LIMIT↔SL of an open NIFTY or BANKNIFTY order."""
    req = {"order_id": order_id, "changes": changes}
    try:
        intent = _intent(intent)
        _require_on()
    except OrderRefused as e:
        _log("modify", None, "orders", req, "refused", order_id, f"{e.code}: {e.message}")
        raise
    row_id, prev = _claim("modify", intent, "orders", req)
    if prev is not None:
        if prev.get("outcome") == "sent":
            return {"ok": True, "order_id": prev.get("order_id"), "duplicate": True}
        raise OrderRefused("INTENT_USED", "This modification was already attempted.", 409)
    try:
        with _lock:
            kite = _kite()
            cur = _current(kite, order_id)
            if str(cur.get("status") or "").upper() not in OPEN_STATUSES:
                raise OrderRefused("NOT_OPEN", f"Order is {cur.get('status')}; only open orders can be changed.", 409)
            if cur.get("exchange") != "NFO":
                raise OrderRefused("NOT_ALLOWED", "Only NFO orders can be changed here.")
            inst = resolve(_index(kite), {"tradingsymbol": cur.get("tradingsymbol")})
            otype = str(changes.get("order_type") or cur.get("order_type") or "").upper()
            if otype not in MODIFY_ORDER_TYPES:
                raise OrderRefused("BAD_ORDER", "Only LIMIT and SL orders can be changed here.")
            lot = int(inst.get("lot_size") or 0)
            lots = changes.get("lots")
            if lots is None:
                lots = int(cur.get("quantity") or 0) // lot if lot else 0
            leg = {"side": cur.get("transaction_type"), "order_type": otype, "product": cur.get("product"),
                   "validity": cur.get("validity") or "DAY", "lots": lots,
                   "price": changes.get("price", cur.get("price")),
                   "trigger_price": changes.get("trigger_price", cur.get("trigger_price"))}
            ltp = _ltps(kite, [inst["tradingsymbol"]]).get(inst["tradingsymbol"])
            params = check_leg(leg, inst, ltp)
            _pace()
            try:
                kite.modify_order(variety=cur.get("variety") or "regular", order_id=str(order_id),
                                  quantity=params["quantity"], price=params.get("price"),
                                  order_type=params["order_type"], trigger_price=params.get("trigger_price"),
                                  validity=params["validity"])
            except Exception as e:
                _update(row_id, "error", str(order_id), _kite_message(e))
                raise OrderRefused("KITE_REFUSED", f"Kite: {_kite_message(e)}", 502) from None
            finally:
                _last_sent["at"] = time.monotonic()
    except OrderRefused as e:
        if e.code != "KITE_REFUSED":
            _update(row_id, "refused", str(order_id), f"{e.code}: {e.message}")
        raise
    _update(row_id, "sent", str(order_id), None)
    print(f"  🟠 KITE MODIFY {order_id} → {params['order_type']} {params['quantity']} @ {params.get('price')}")
    return {"ok": True, "order_id": str(order_id), **leg_summary(inst, params)}


def cancel(intent: str, order_id: str) -> dict:
    """Cancel any open order of the day (cancelling only reduces risk, so it works while halted)."""
    req = {"order_id": order_id}
    try:
        intent = _intent(intent)
        _require_on(for_cancel=True)
    except OrderRefused as e:
        _log("cancel", None, "orders", req, "refused", order_id, f"{e.code}: {e.message}")
        raise
    row_id, prev = _claim("cancel", intent, "orders", req)
    if prev is not None:
        if prev.get("outcome") == "sent":
            return {"ok": True, "order_id": prev.get("order_id"), "duplicate": True}
        raise OrderRefused("INTENT_USED", "This cancellation was already attempted.", 409)
    try:
        with _lock:
            kite = _kite()
            cur = _current(kite, order_id)
            if str(cur.get("status") or "").upper() not in OPEN_STATUSES:
                raise OrderRefused("NOT_OPEN", f"Order is {cur.get('status')}; nothing to cancel.", 409)
            _pace()
            try:
                kite.cancel_order(variety=cur.get("variety") or "regular", order_id=str(order_id))
            except Exception as e:
                _update(row_id, "error", str(order_id), _kite_message(e))
                raise OrderRefused("KITE_REFUSED", f"Kite: {_kite_message(e)}", 502) from None
            finally:
                _last_sent["at"] = time.monotonic()
    except OrderRefused as e:
        if e.code != "KITE_REFUSED":
            _update(row_id, "refused", str(order_id), f"{e.code}: {e.message}")
        raise
    _update(row_id, "sent", str(order_id), None)
    print(f"  🟠 KITE CANCEL {order_id}")
    return {"ok": True, "order_id": str(order_id)}


def book() -> dict:
    """Today's Kite order book (Kite's own rows), newest first."""
    kite = _kite()
    try:
        rows = kite.orders() or []
    except Exception as e:
        raise OrderRefused("KITE_REFUSED", f"Kite: {_kite_message(e)}", 502) from None
    rows = sorted(rows, key=lambda o: str(o.get("order_timestamp") or ""), reverse=True)
    return {"ok": True, "orders": _jsonable(rows), "enabled": enabled(), "halted": halted()}


def order_state(order_id: str) -> dict:
    """The latest state of one order (last entry of its Kite history)."""
    o = _current(_kite(), order_id)
    keep = ("order_id", "status", "status_message", "tradingsymbol", "transaction_type", "quantity",
            "filled_quantity", "pending_quantity", "average_price", "price", "trigger_price", "order_type",
            "product", "order_timestamp", "tag")
    return {"ok": True, "order": _jsonable({k: o.get(k) for k in keep})}

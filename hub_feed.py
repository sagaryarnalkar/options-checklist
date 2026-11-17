"""Kite data for the Option Hub tab, from the dashboard's own Kite login (#111).

Option Hub's Zerodha adapter builds its option chain from two Kite calls: the NFO instrument dump
and /quote. In our build its Kite client (optionhub/server/dashboard.mjs, via kiteFetch) sends both HERE
instead of api.kite.trade. Its code never holds a real Kite token, and nothing it sends reaches
Kite: orders, positions, funds and history get a read-only refusal there.

Each contract's quote is the NEWER of
  * the recorder's latest minute (ATM±10 of the nearest expiry: zero extra Kite calls), and
  * a top-up: ONE kite.quote of the whole expiry (≤500 contracts) made here, at most every 30 s
    per expiry, only inside seconds 15–55 of a minute, never while a recorder tick runs, ≥1.1 s
    after the previous top-up, and only while the hub is actually asking. Shared by every hub tab.

Why not the recorder alone: on 16 Nov 2026 the 21 recorded strikes held 55%/43% of NIFTY's
call/put OI and 26%/14% of BANKNIFTY's — the hub's PCR would have read 0.73 for 0.94 (NIFTY)
and 0.65 for 1.24 (BANKNIFTY), max pain 61,300 for 59,000. So an expiry is served only with a
top-up ≤90 s old, never as the 21-strike slice on its own.

Refused (→ Option Hub falls back to NSE by itself): outside 09:15–15:30 IST, market holidays,
before the dashboard's Kite login, and anything the recorder does not cover (SENSEX/BFO, stocks).
No bid/ask: the recorder keeps no depth, and mixing the two would make it flicker.

OI change / price change: his Zerodha adapter drops everything but LTP/OI/volume, so his chain
showed OI change 0 and no build-up labels. prev() supplies the previous session's OFFICIAL
end-of-day OI and close from NSE's F&O bhavcopy (one ~1.4 MB zip a day from nsearchives, not
bot-walled; OpnIntrst is in UNITS like Kite — verified: 13 Nov NIFTY 17-Nov 25100 PE bhav
7,008,690 = NSE chain 107,826 contracts × 65), and serve.mjs adds previous_oi /
previous_close_price to his broker-proxy response — the fields his parser already reads.
NOTE the recorder's own 15:30 OI does NOT match that official EOD (same contract: 8,715,265 at
15:30:01 vs 7,008,690 official), so it is never used as the baseline.

NSE website relay (nse()): outside Kite hours (and for indices, FII/DII, holidays …) his proxy reads
www.nseindia.com itself — which refuses its Node client from the droplet. In our build
(optionhub/server/dashboard.mjs, via nseJson) those reads come here instead; see the section below.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from typing import Optional

import db
import recorder

IST = recorder.IST
# NFO `name` → recorder underlying key, and Kite spot key → underlying key
SERVED = {conf["nfo_name"]: key for key, conf in recorder.UNDERLYINGS.items()}
SPOT_KEYS = {f'{conf["exchange"]}:{conf["tradingsymbol"]}': key for key, conf in recorder.UNDERLYINGS.items()}

TOPUP_EVERY_S = 30          # at most one Kite call per expiry per this many seconds
GATE_SECONDS = (15, 55)     # top-ups only inside this part of a minute (recorder fires at :00)
TOPUP_MAX_AGE_S = 90        # an expiry is served only with a top-up this fresh
RECORDER_MAX_AGE_S = 150    # recorder rows older than this are ignored
KITE_GAP_S = 1.1            # Kite quote limit is 1 req/s
TICK_HUNG_S = 90            # a recorder tick "running" longer than this no longer blocks top-ups
MAX_KEYS = 500              # Kite /quote cap per call (incl. the spot key)

CSV_COLUMNS = ("instrument_token", "exchange_token", "tradingsymbol", "name", "last_price", "expiry",
               "strike", "tick_size", "lot_size", "instrument_type", "segment", "exchange")


class FeedRefused(Exception):
    """Answered to the hub as a Kite-style error: {"status":"error","error_type",...}."""

    def __init__(self, status: int, message: str, error_type: str = "GeneralException"):
        super().__init__(message)
        self.status, self.message, self.error_type = status, message, error_type


_lock = threading.Lock()
_topups: dict = {}                      # (name, expiry_iso) -> {"at", "quotes", "spot"}
_last_call = {"at": 0.0}
_kite_cache: dict = {"token": None, "kite": None}
_index_cache: dict = {"src": None, "by_symbol": {}, "by_expiry": {}}
stats = {"topup_calls": 0, "topup_errors": 0, "last_error": None, "last_call_at": None}


# ---------------------------------------------------------------- plumbing --

def _kite():
    """KiteConnect on the dashboard's cached session. No profile() round trip per call (unlike
    kite_auth.get_kite_from_cache) — a dead token surfaces as the top-up's own error."""
    from kite_auth import load_env, read_cached_session
    sess = read_cached_session()
    if not sess or not sess.get("access_token"):
        raise FeedRefused(403, "The dashboard is not logged in to Kite today. Log in on the dashboard; "
                               "until then Option Hub uses NSE.", "TokenException")
    tok = sess["access_token"]
    if _kite_cache["token"] != tok:
        from kiteconnect import KiteConnect
        try:
            api_key, _ = load_env()
        except SystemExit as e:  # load_env exits when the key is missing
            raise FeedRefused(503, f"Kite API key missing on the server: {e}") from None
        k = KiteConnect(api_key=api_key)
        k.set_access_token(tok)
        _kite_cache.update(token=tok, kite=k)
    return _kite_cache["kite"]


def _require_open(kite, now_dt: datetime) -> None:
    if not recorder.is_market_hours(now_dt):
        raise FeedRefused(503, "Outside market hours (09:15–15:30 IST): Option Hub uses NSE.")
    if not recorder.session_open_today(kite, now_dt):
        raise FeedRefused(503, "No trading session today (market holiday?): Option Hub uses NSE.")


def _exp_iso(v) -> str:
    return v.isoformat() if isinstance(v, (date, datetime)) else str(v)[:10]


def _index(kite) -> dict:
    """Served option contracts from the recorder's daily NFO dump, keyed by tradingsymbol."""
    ins = recorder._get_instruments(kite)
    if _index_cache["src"] is not ins:
        today = datetime.now(IST).date().isoformat()
        by_symbol, by_expiry = {}, {}
        for r in ins:
            if r.get("name") not in SERVED or r.get("instrument_type") not in ("CE", "PE"):
                continue
            exp = _exp_iso(r.get("expiry"))
            if exp < today:
                continue
            by_symbol[r["tradingsymbol"]] = r
            by_expiry.setdefault((r["name"], exp), []).append(r)
        _index_cache.update(src=ins, by_symbol=by_symbol, by_expiry=by_expiry)
    return _index_cache


def _ts_epoch(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _recorder_latest(name: str, expiry: str, now: float) -> Optional[dict]:
    """The recorder's newest minute for (underlying, expiry), or None if stale/absent."""
    under = SERVED[name]
    day = datetime.now(IST).date().isoformat()
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT ts FROM chain_snapshot WHERE underlying=? AND expiry=? AND ts>=? ORDER BY ts DESC LIMIT 1",
            (under, expiry, day)).fetchone()
        if not row:
            return None
        ts = row[0]
        at = _ts_epoch(ts)
        if now - at > RECORDER_MAX_AGE_S:
            return None
        rows = conn.execute(
            "SELECT strike, opt_type, ltp, volume, oi, spot FROM chain_snapshot "
            "WHERE underlying=? AND expiry=? AND ts=?", (under, expiry, ts)).fetchall()
    out = {"at": at, "rows": {}, "spot": None}
    for strike, typ, ltp, vol, oi, spot in rows:
        out["rows"][(int(strike), typ)] = (ltp, vol, oi)
        out["spot"] = spot
    return out


def _gate_open(now_dt: datetime, now: float) -> bool:
    lo, hi = GATE_SECONDS
    if not (lo <= now_dt.second <= hi):
        return False
    t = recorder.TICK
    if t["started"] > t["finished"] and now - t["started"] < TICK_HUNG_S:
        return False
    return now - _last_call["at"] >= KITE_GAP_S


def _topup(kite, idx: dict, name: str, expiry: str, now_dt: datetime, ref_spot: Optional[float]) -> Optional[dict]:
    """The expiry's latest top-up, refreshed with one kite.quote when due and the gate is open."""
    key = (name, expiry)
    with _lock:
        cur = _topups.get(key)
        now = time.time()
        if cur and now - cur["at"] < TOPUP_EVERY_S:
            return cur
        if not _gate_open(now_dt, now):
            return cur
        contracts = idx["by_expiry"].get(key) or []
        if not contracts:
            return cur
        spot_key = next(k for k, v in SPOT_KEYS.items() if v == SERVED[name])
        ref = ref_spot or (cur and cur["spot"] and cur["spot"][1])
        if len(contracts) > MAX_KEYS - 1 and ref:
            contracts = sorted(contracts, key=lambda r: abs(float(r["strike"]) - ref))[: MAX_KEYS - 1]
        keys = [f"NFO:{r['tradingsymbol']}" for r in contracts[: MAX_KEYS - 1]]
        _last_call["at"] = now
        stats["topup_calls"] += 1
        stats["last_call_at"] = now
        try:
            q = kite.quote(keys + [spot_key]) or {}
        except Exception as e:  # noqa: BLE001 — keep serving the previous top-up while it is fresh
            stats["topup_errors"] += 1
            stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            return cur
        at = time.time()
        sp = (q.get(spot_key) or {}).get("last_price")
        cur = {
            "at": at,
            "quotes": {k: (v.get("last_price"), v.get("volume"), v.get("oi"))
                       for k, v in q.items() if k != spot_key and v},
            "spot": (at, sp) if sp else None,
        }
        _topups[key] = cur
        return cur


def _iso(at: float) -> str:
    return datetime.fromtimestamp(at, IST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- public --

def instruments_csv(exchange: str) -> str:
    """Kite's instrument-dump CSV for the contracts this feed serves (NIFTY/BANKNIFTY options)."""
    if exchange != "NFO":
        raise FeedRefused(404, f"The dashboard feed serves NFO only (asked for {exchange}).", "InputException")
    kite = _kite()
    _require_open(kite, datetime.now(IST))
    idx = _index(kite)
    lines = [",".join(CSV_COLUMNS)]
    for r in idx["by_symbol"].values():
        strike = float(r.get("strike") or 0)
        vals = {**r, "expiry": _exp_iso(r.get("expiry")), "strike": f"{strike:g}", "last_price": 0}
        lines.append(",".join(str(vals.get(c, "")) for c in CSV_COLUMNS))
    return "\n".join(lines) + "\n"


def quote(keys: list) -> dict:
    """Kite /quote `data` for the requested "EX:SYMBOL" keys (absent = no fresh data)."""
    now_dt = datetime.now(IST)
    now = time.time()
    kite = _kite()
    _require_open(kite, now_dt)
    idx = _index(kite)

    groups: dict = {}
    spot_names = set()
    for k in keys:
        if k in SPOT_KEYS:
            spot_names.add(SPOT_KEYS[k])
            continue
        ex, _, sym = k.partition(":")
        r = idx["by_symbol"].get(sym) if ex == "NFO" else None
        if r:
            groups.setdefault((r["name"], _exp_iso(r["expiry"])), []).append((k, r))

    out: dict = {}
    spot_obs: dict = {}  # underlying key -> [(at, price)]
    served_options = 0
    for (name, exp), items in groups.items():
        rec = _recorder_latest(name, exp, now)
        top = _topup(kite, idx, name, exp, now_dt, rec and rec["spot"])
        under = SERVED[name]
        if rec and rec["spot"]:
            spot_obs.setdefault(under, []).append((rec["at"], rec["spot"]))
        if top and top.get("spot"):
            spot_obs.setdefault(under, []).append(top["spot"])
        if not top or now - top["at"] > TOPUP_MAX_AGE_S:
            continue  # never the 21-strike slice on its own
        for k, r in items:
            cands = []
            tq = top["quotes"].get(k)
            if tq and tq[0] is not None:
                cands.append((top["at"], tq, "kite-topup"))
            if rec:
                rq = rec["rows"].get((int(float(r["strike"])), r["instrument_type"]))
                if rq and rq[0] is not None:
                    cands.append((rec["at"], rq, "recorder"))
            if not cands:
                continue
            at, (ltp, vol, oi), src = max(cands, key=lambda c: c[0])
            out[k] = {"instrument_token": r.get("instrument_token"), "timestamp": _iso(at),
                      "last_price": ltp, "volume": vol or 0, "oi": oi or 0, "source": src}
            served_options += 1

    for under in spot_names:
        obs = list(spot_obs.get(under, []))
        if not obs:  # spot-only request (/quote/ltp): recorder minute or any fresh top-up
            name = recorder.UNDERLYINGS[under]["nfo_name"]
            for (n, _e), t in list(_topups.items()):
                if n == name and t.get("spot"):
                    obs.append(t["spot"])
            with db.get_conn() as conn:
                row = conn.execute("SELECT ts, spot FROM chain_snapshot WHERE underlying=? AND ts>=? "
                                   "ORDER BY ts DESC LIMIT 1",
                                   (under, now_dt.date().isoformat())).fetchone()
            if row:
                obs.append((_ts_epoch(row[0]), row[1]))
        obs = [o for o in obs if o and o[1] and now - o[0] <= RECORDER_MAX_AGE_S]
        if obs:
            at, px = max(obs, key=lambda o: o[0])
            spot_key = next(k for k, v in SPOT_KEYS.items() if v == under)
            out[spot_key] = {"instrument_token": recorder.UNDERLYINGS[under]["instrument_token"],
                             "timestamp": _iso(at), "last_price": px}

    if groups and not served_options:
        raise FeedRefused(503, "No fresh dashboard chain for this expiry yet (Option Hub uses NSE meanwhile).")
    if not out:
        raise FeedRefused(503, "Nothing the dashboard records matches this request (Option Hub uses NSE).")
    return out


def profile() -> dict:
    """What the hub's Broker Settings "Test connection" sees (GET /user/profile)."""
    _kite()  # raises TokenException-style refusal when the dashboard is logged out
    return {"user_id": "DASHBOARD", "user_name": "Dashboard Kite feed (read-only)",
            "user_shortname": "Dashboard feed", "broker": "ZERODHA", "email": "",
            "exchanges": ["NSE", "NFO"], "products": [], "order_types": []}


# ------------------------------------------------- previous session (bhavcopy) --

BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"
BHAV_RETRY_S = 600
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
_prev_lock = threading.Lock()
_prev: dict = {"for_day": None, "session": None, "legs": None, "tried_at": 0.0, "error": None}


def parse_bhav(csv_bytes: bytes) -> dict:
    """{(name, expiry_iso, strike, CE|PE): (open_interest_units, close)} for the served names' options,
    plus {(name, expiry_iso, 0, "FUT"): (oi, close)} for EVERY future (stocks and indices: the F&O
    stock list's OI change)."""
    import csv
    import io
    out = {}
    for row in csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))):
        name, typ = row.get("TckrSymb"), row.get("OptnTp")
        if row.get("FinInstrmTp") in ("STF", "IDF"):
            try:
                out[(name, row["XpryDt"][:10], 0, "FUT")] = (
                    int(float(row["OpnIntrst"] or 0)), float(row["ClsPric"] or 0) or None)
            except (KeyError, ValueError):
                pass
            continue
        if name not in SERVED or typ not in ("CE", "PE"):
            continue
        try:
            out[(name, row["XpryDt"][:10], int(float(row["StrkPric"])), typ)] = (
                int(float(row["OpnIntrst"] or 0)), float(row["ClsPric"] or 0) or None)
        except (KeyError, ValueError):
            continue
    return out


def _fetch_bhav(today: date):
    """The latest published F&O bhavcopy BEFORE today: (session_date, parsed). Walks back ≤7 days
    (weekends, holidays → 404)."""
    import io
    import zipfile
    from datetime import timedelta
    import requests
    for back in range(1, 8):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        r = requests.get(BHAV_URL.format(d=d), headers={"User-Agent": _UA, "Accept": "*/*"}, timeout=25)
        if r.status_code == 404:
            continue
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        return d, parse_bhav(z.read(z.namelist()[0]))
    raise RuntimeError("no F&O bhavcopy published in the last 7 days")


def _prev_legs() -> tuple:
    """(session_date, legs) for today, downloading once per IST day; (None, None) if unavailable."""
    today = datetime.now(IST).date()
    with _prev_lock:
        p = _prev
        if p["for_day"] == today and p["legs"] is not None:
            return p["session"], p["legs"]
        if p["for_day"] == today and time.time() - p["tried_at"] < BHAV_RETRY_S:
            return None, None
        p.update(for_day=today, tried_at=time.time())
        try:
            session, legs = _fetch_bhav(today)
            p.update(session=session, legs=legs, error=None)
            return session, legs
        except Exception as e:  # noqa: BLE001 — OI change just stays blank until the retry
            p.update(session=None, legs=None, error=f"{type(e).__name__}: {str(e)[:160]}")
            return None, None


def prev(symbol: str, expiry: Optional[str] = None) -> dict:
    """Previous-session OI (units) and close per strike for one expiry, for serve.mjs to add to his
    chain as previous_oi / previous_close_price. Contracts absent from a loaded bhavcopy were
    listed today → previous OI 0."""
    name = (symbol or "").upper()
    if name not in SERVED:
        raise FeedRefused(404, f"No previous-session data for {symbol}.", "InputException")
    session, legs = _prev_legs()
    if legs is None:
        raise FeedRefused(503, f"NSE F&O bhavcopy unavailable ({_prev['error'] or 'retrying'}).")
    today = datetime.now(IST).date().isoformat()
    if not expiry:
        exps = sorted({e for (n, e, _s, t) in legs if n == name and e >= today and t != "FUT"})
        if not exps:
            raise FeedRefused(404, f"No {name} expiry in the bhavcopy.", "InputException")
        expiry = exps[0]
    out: dict = {}
    for (n, e, strike, typ), (oi, close) in legs.items():
        if n == name and e == expiry and typ in ("CE", "PE"):
            out.setdefault(f"{strike:g}", {})[typ] = {"previous_oi": oi, "previous_close_price": close}
    return {"session": session.isoformat(), "expiry": expiry, "legs": out}


# ------------------------------------------------------ NSE website relay --
# Option Hub's proxy reads www.nseindia.com/api/* (option chain, indices, market status, FII/DII,
# holidays, VIX history …) with Node's own fetch. From the droplet NSE's bot wall (Akamai) refuses
# that client — it fingerprints the TLS handshake, and a datacenter IP gets the strict treatment:
# the 16 Nov 2026 install smoke test got 502 for the NIFTY chain while, from the SAME server, our
# python `requests` session read the ASM/GSM lists and corporate filings (#81 found the same split
# for urllib vs requests there; from a home connection Node, urllib and requests all get 200).
# So the hub's nseJson (optionhub/server/dashboard.mjs) sends those reads here and we make them
# with `requests`. Read-only GETs of /api/* only; cached briefly; one call at a time, spaced, with
# a back-off after a timeout — the IP is shared with the CSP scanner's NSE reads.

NSE_WWW = "https://www.nseindia.com"
NSE_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
NSE_PATH_RE = re.compile(r"/api/[A-Za-z0-9][A-Za-z0-9_\-/]*(\?[A-Za-z0-9_\-.~%&=+,:]*)?")
NSE_CACHE_S = 5.0           # his proxy caches 15 s itself; this only folds simultaneous asks
NSE_GAP_S = 0.3             # spacing between our NSE calls
NSE_TIMEOUT_S = 10.0        # his client gives up at 15 s
NSE_BACKOFF_S = 30.0        # after a timeout / connection failure, answer 503 without calling
NSE_REPRIME_S = 240.0       # refresh the homepage cookie handshake this often
NSE_CACHE_MAX = 200

_nse_lock = threading.Lock()                  # one NSE call at a time
_nse_cache_lock = threading.Lock()
_nse: dict = {"session": None, "primed_at": 0.0, "last_call": 0.0, "down_until": 0.0}
_nse_cache: dict = {}                         # path -> (at, status, content_type, body)
nse_stats = {"calls": 0, "cache_hits": 0, "errors": 0, "last_ok_at": None, "last_error": None}


def _nse_session(fresh: bool = False):
    """A requests session primed on the homepage, like csp._nse_session (the path proven on the
    droplet). A failed handshake still returns the bare session: the APIs answer without it."""
    import requests
    if fresh or _nse["session"] is None or time.time() - _nse["primed_at"] > NSE_REPRIME_S:
        ses = requests.Session()
        ses.headers.update({"User-Agent": NSE_UA, "Accept-Language": "en-US,en;q=0.9"})
        try:
            ses.get(NSE_WWW, timeout=NSE_TIMEOUT_S, headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        except Exception:  # noqa: BLE001 — bare session below
            pass
        _nse.update(session=ses, primed_at=time.time())
    return _nse["session"]


def _nse_get(path: str, fresh: bool = False):
    gap = NSE_GAP_S - (time.time() - _nse["last_call"])
    if gap > 0:
        time.sleep(gap)
    ses = _nse_session(fresh)
    try:
        return ses.get(NSE_WWW + path, timeout=NSE_TIMEOUT_S, headers={
            "Accept": "application/json, text/plain, */*", "Referer": NSE_WWW + "/option-chain"})
    finally:
        _nse["last_call"] = time.time()
        nse_stats["calls"] += 1


def _nse_refused(r) -> bool:
    """Bot-wall answers: 401/403, or an HTML page where JSON was asked for."""
    if r.status_code in (401, 403):
        return True
    return (r.ok and "json" not in (r.headers.get("content-type") or "")
            and r.content.lstrip()[:1] not in (b"{", b"["))


def nse(path: str) -> tuple:
    """(status, content_type, body) of GET www.nseindia.com<path>, for the hub's proxy."""
    if not isinstance(path, str) or len(path) > 400 or not NSE_PATH_RE.fullmatch(path):
        raise FeedRefused(400, "Only www.nseindia.com/api/* reads are relayed.", "InputException")
    now = time.time()
    with _nse_cache_lock:
        hit = _nse_cache.get(path)
        if hit and now - hit[0] < NSE_CACHE_S:
            nse_stats["cache_hits"] += 1
            return hit[1:]
    with _nse_lock:
        with _nse_cache_lock:                 # someone may have fetched it while we waited
            hit = _nse_cache.get(path)
            if hit and time.time() - hit[0] < NSE_CACHE_S:
                nse_stats["cache_hits"] += 1
                return hit[1:]
        if time.time() < _nse["down_until"]:
            raise FeedRefused(503, f"NSE relay backing off after an error ({nse_stats['last_error']}).")
        try:
            r = _nse_get(path)
            if _nse_refused(r):               # stale handshake? one retry on a fresh session
                r = _nse_get(path, fresh=True)
        except Exception as e:  # noqa: BLE001 — timeout / connection: back off, his proxy retries
            nse_stats["errors"] += 1
            nse_stats["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            _nse["down_until"] = time.time() + NSE_BACKOFF_S
            raise FeedRefused(504, f"NSE did not answer ({type(e).__name__}).")
        ctype = r.headers.get("content-type") or "application/octet-stream"
        if r.status_code == 200 and not _nse_refused(r):
            nse_stats["last_ok_at"] = time.time()
            with _nse_cache_lock:
                if len(_nse_cache) >= NSE_CACHE_MAX:
                    _nse_cache.clear()
                _nse_cache[path] = (time.time(), 200, ctype, r.content)
        else:
            nse_stats["errors"] += 1
            nse_stats["last_error"] = f"HTTP {r.status_code} {path.split('?')[0]}"
        return r.status_code, ctype, r.content


# ------------------------------------------------ F&O stock quotes (Kite) --
# Option Hub's watchlist, dashboard and screener read every F&O stock's quote from NSE's
# "equity-derivatives" list (/api/equity-stockIndices?index=SECURITIES IN F&O), which NSE retired
# (404 for every client, Nov 2026) — so those pages showed labelled SAMPLE numbers. fno_stocks()
# answers in NSE's own shape from the dashboard's Kite login: ONE kite.quote of every F&O stock's cash
# quote (NSE:<name>) plus its near-month future (OI); OI change vs NSE's official previous-session OI
# (bhavcopy). Cached 30 s; Kite calls keep the chain top-ups' spacing and never overlap a recorder tick.

FNO_TTL_S = 30
FNO_STALE_OK_S = 120         # while the gate is closed, a reading this fresh is served as is
INDEX_NAMES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
_fno_lock = threading.Lock()
_fno: dict = {"at": 0.0, "body": None}
fno_stats = {"calls": 0, "errors": 0, "last_error": None}


def _wait_for_gate(timeout_s: float = 25.0) -> bool:
    """Block until a Kite quote may go out (seconds 15–55, no recorder tick running, ≥1.1 s apart)."""
    end = time.time() + timeout_s
    while time.time() < end:
        if _gate_open(datetime.now(IST), time.time()):
            return True
        time.sleep(0.25)
    return False


def _prev_fut_oi() -> dict:
    _session, legs = _prev_legs()
    return {(n, e): oi for (n, e, _s, t), (oi, _c) in (legs or {}).items() if t == "FUT"}


def fno_stocks() -> dict:
    """{"data": [NSE equity-derivatives rows]} for every F&O stock, from Kite."""
    with _fno_lock:
        now = time.time()
        body = _fno["body"]
        if body and now - _fno["at"] < FNO_TTL_S:
            return body
        kite = _kite()
        if body and now - _fno["at"] < FNO_STALE_OK_S and not _gate_open(datetime.now(IST), now):
            return body
        if not _wait_for_gate():
            if body:
                return body
            raise FeedRefused(503, "Kite is busy (recorder tick); try again in a few seconds.")
        today = datetime.now(IST).date().isoformat()
        futs: dict = {}
        for r in recorder._get_instruments(kite):
            name = r.get("name")
            if r.get("instrument_type") != "FUT" or not name or name in INDEX_NAMES:
                continue
            exp = _exp_iso(r.get("expiry"))
            if exp >= today and (name not in futs or exp < futs[name][0]):
                futs[name] = (exp, r["tradingsymbol"])
        names = sorted(futs)
        keys = [f"NSE:{n}" for n in names] + [f"NFO:{futs[n][1]}" for n in names]
        q: dict = {}
        try:
            for i in range(0, len(keys), MAX_KEYS):
                if i and not _wait_for_gate(5.0):
                    break
                _last_call["at"] = time.time()
                fno_stats["calls"] += 1
                q.update(kite.quote(keys[i:i + MAX_KEYS]) or {})
        except Exception as e:  # noqa: BLE001
            fno_stats["errors"] += 1
            fno_stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            if body:
                return body
            raise FeedRefused(502, f"Kite quote failed: {type(e).__name__}.")
        prev = _prev_fut_oi()
        rows = []
        for n in names:
            c = q.get(f"NSE:{n}") or {}
            ltp = c.get("last_price")
            if not ltp:
                continue
            o = c.get("ohlc") or {}
            close = o.get("close") or None
            f = q.get(f"NFO:{futs[n][1]}") or {}
            oi = f.get("oi")
            p = prev.get((n, futs[n][0]))
            rows.append({
                "symbol": n, "identifier": n, "lastPrice": ltp,
                "change": round(ltp - close, 2) if close else 0.0,
                "pChange": round((ltp - close) / close * 100, 2) if close else 0.0,
                "open": o.get("open") or ltp, "dayHigh": o.get("high") or ltp, "dayLow": o.get("low") or ltp,
                "previousClose": close or ltp, "totalTradedVolume": c.get("volume") or 0,
                "openInterest": oi, "changeinOpenInterest": (oi - p) if (oi is not None and p is not None) else None,
                "meta": {"industry": ""},
            })
        body = {"data": rows, "source": "kite", "asOf": int(time.time() * 1000)}
        _fno.update(at=time.time(), body=body)
        return body


# ------------------------------------------- session history (recorder) --
# Option Hub's intraday charts (GEX levels through the session, hedging pressure, options flow, OI
# time series, OI replay, expiry-day straddles) run on chain snapshots its BROWSER records while a page
# is open in market hours; with none they draw a labelled simulated session. Its proxy can backfill
# that store from a server recorder (GET /api/x/snapshots) — ours records every minute of every
# session (chain_snapshot: ATM±10 of the nearest expiry), so snapshots() answers in its format.
# IV per row is backed out of the LTP (Black-Scholes, recommend._implied_vol) because his GEX needs it.
# Units follow the live Kite chain the hub shows (OI and cumulative volume in UNITS).

SNAP_MAX = 800
_snap_cache: dict = {}        # (under, expiry, day) -> payload list, completed days only


def _ts_ms(ts: str) -> int:
    return int(datetime.fromisoformat(ts).timestamp() * 1000)


def _iso_ist(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, IST).isoformat(timespec="seconds")


def _build_snaps(rows: list, under: str, expiry: str) -> list:
    from recommend import _implied_vol
    exp_dt = datetime.fromisoformat(f"{expiry}T15:30:00+05:30")
    out, cur, cur_ts = [], None, None
    for ts, spot, strike, typ, ltp, vol, oi in rows:
        if ts != cur_ts:
            if cur and cur["rows"]:
                out.append(cur)
            cur_ts = ts
            cur = {"symbol": under, "expiry": expiry, "ts": _ts_ms(ts), "spot": spot or 0, "rows": []}
            t_years = max((exp_dt - datetime.fromisoformat(ts)).total_seconds(), 0) / (365 * 86400)
        iv = None
        if ltp and spot and t_years > 0:
            iv = _implied_vol(float(ltp), float(spot), float(strike), t_years, typ)
        cur["rows"].append({"strike": float(strike), "side": typ, "ltp": float(ltp or 0), "oi": float(oi or 0),
                            "vol": float(vol or 0), "iv": round(iv * 100, 2) if iv else 0.0, "bid": 0.0, "ask": 0.0})
    if cur and cur["rows"]:
        out.append(cur)
    return out


def snapshots(symbol: str, expiry: Optional[str] = None, from_ms: Optional[float] = None,
              to_ms: Optional[float] = None, latest: bool = False) -> dict:
    """{"symbol","expiry","from","to","snapshots":[…],"truncated"} — Option Hub's server-recorder
    payload. latest=True: the most recent recorded session (today's, or the last one before it)."""
    under = SERVED.get((symbol or "").upper())
    if not under:
        raise FeedRefused(404, f"No recorded history for {symbol}.", "InputException")
    if expiry is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry):
        raise FeedRefused(400, "expiry must be YYYY-MM-DD", "InputException")
    with db.get_conn() as conn:
        if latest or from_ms is None:
            q = "SELECT substr(MAX(ts),1,10) FROM chain_snapshot WHERE underlying=?" + (" AND expiry=?" if expiry else "")
            day = conn.execute(q, (under, expiry) if expiry else (under,)).fetchone()[0]
            if not day:
                return {"symbol": symbol.upper(), "expiry": expiry, "from": 0, "to": 0, "snapshots": [], "truncated": False}
            lo, hi = f"{day}T00:00:00", f"{day}T23:59:59+99"
        else:
            lo = _iso_ist(from_ms)
            hi = _iso_ist(to_ms if to_ms is not None else time.time() * 1000)
        if not expiry:
            r = conn.execute("SELECT expiry, COUNT(*) c FROM chain_snapshot WHERE underlying=? AND ts>=? AND ts<=? "
                             "GROUP BY expiry ORDER BY c DESC LIMIT 1", (under, lo, hi)).fetchone()
            if not r:
                return {"symbol": symbol.upper(), "expiry": None, "from": 0, "to": 0, "snapshots": [], "truncated": False}
            expiry = r[0]
        day = lo[:10]
        whole_past_day = (latest or from_ms is None) and day < datetime.now(IST).date().isoformat()
        key = (under, expiry, day)
        if whole_past_day and key in _snap_cache:
            snaps = _snap_cache[key]
        else:
            rows = conn.execute(
                "SELECT ts, spot, strike, opt_type, ltp, volume, oi FROM chain_snapshot "
                "WHERE underlying=? AND expiry=? AND ts>=? AND ts<=? ORDER BY ts, strike, opt_type",
                (under, expiry, lo, hi)).fetchall()
            snaps = _build_snaps(rows, symbol.upper(), expiry)
            if whole_past_day:
                if len(_snap_cache) > 40:
                    _snap_cache.clear()
                _snap_cache[key] = snaps
    truncated = len(snaps) > SNAP_MAX
    if truncated:
        step = len(snaps) / SNAP_MAX
        snaps = [snaps[int(i * step)] for i in range(SNAP_MAX)]
    return {"symbol": symbol.upper(), "expiry": expiry,
            "from": snaps[0]["ts"] if snaps else 0, "to": snaps[-1]["ts"] if snaps else 0,
            "snapshots": snaps, "truncated": truncated}


def status() -> dict:
    """Small summary for /hub/status (public: market data plumbing only)."""
    now = time.time()
    return {
        "prev_session": _prev["session"].isoformat() if _prev.get("session") else None,
        "prev_error": _prev["error"],
        "topups": {f"{n} {e}": round(now - t["at"]) for (n, e), t in _topups.items()},
        "topup_calls": stats["topup_calls"],
        "topup_errors": stats["topup_errors"],
        "last_error": stats["last_error"],
        "last_call_age_s": round(now - stats["last_call_at"]) if stats["last_call_at"] else None,
        "fno_stocks": {"calls": fno_stats["calls"], "errors": fno_stats["errors"], "last_error": fno_stats["last_error"],
                       "rows": len((_fno["body"] or {}).get("data") or []),
                       "age_s": round(now - _fno["at"]) if _fno["at"] else None},
        "nse_relay": {
            "calls": nse_stats["calls"], "cache_hits": nse_stats["cache_hits"],
            "errors": nse_stats["errors"], "last_error": nse_stats["last_error"],
            "last_ok_age_s": round(now - nse_stats["last_ok_at"]) if nse_stats["last_ok_at"] else None,
        },
    }

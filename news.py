"""News / disclosure check for CSP candidates.

The CSP scanner deliberately hunts stocks that have ALREADY fallen — that is
where the premium is. The whole edge depends on the fall being a vol scare you
can sell into, rather than the market repricing the company. This module
answers "why did it fall?" so that distinction is visible before you write the
put.

Sourcing was investigated before this was written (see PR #82); the findings
that shaped the design:

* The authoritative source is NOT news or tweets, it is the EXCHANGE FILING
  FEED. SEBI LODR compels disclosure of every material event, so filings are
  upstream of the reporting. NSE serves the whole market for a date range in
  ONE call (~0.3s, ~500 filings/day), joined to our rows on `symbol` — the
  same market-wide trick that made the ASM/GSM lists cheap in #81.
* That feed carries a `News Verification` category, which is the exchange
  itself demanding a company explain a news story. It is precisely the check
  being asked for, already performed by the regulator. It fired on TCS on
  2026-08-12 over the Chandrasekaran exit, the day TCS fell ~6%.
* Google News RSS covers what filings structurally cannot: short-seller
  reports, investigative pieces, and regulator-SIDE action (a SEBI or CCI
  order is announced by the regulator, not the company). Free, no key, but
  ~0.9s per symbol, so it runs only for names that actually need explaining.
* Twitter/X was rejected. The syndication endpoint returns 0 bytes and x.com
  is a JS shell; nitter works today but instances die routinely. More
  importantly the fast Indian business accounts are a FIREHOSE, not a
  per-stock lookup — absence of a mention would carry no information.

SEVERITY IS DELIBERATELY CONSERVATIVE. Classifying "is this fundamental-
altering?" is genuinely hard and getting it wrong in the confident direction
is expensive. A chairman stepping down moved TCS 6% while leaving earnings
power untouched — that may be exactly the overreaction a put seller wants. So
this module SURFACES everything and auto-penalises only the narrow set that is
bad regardless of interpretation. The judgement stays with the user.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import csp

NSE_ANN = "/api/corporate-announcements?index=equities&from_date={frm}&to_date={to}"
NSE_ANN_REFERER = "/companies-listing/corporate-filings-announcements"
GNEWS = ("https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en")

FETCH_DAYS = 2            # how much of the filing feed to pull (cheap, one call)
DEFAULT_WINDOW_H = 24     # how much of it to actually attach to a row
FRESH_H = 4               # "just happened" — highlighted separately in the UI
DEEP_CHECK_MAX = 8        # Google News lookups per scan (~0.9s each)

# Severity is decided on `desc`, NOT on the announcement body.
#
# This was learned the hard way against the live feed. Matching keywords in the
# free text produced confident nonsense: KFINTECH scored "hard" for a product
# launch titled "...Eliminate Signature FRAUD in BFSI" (a product that fights
# fraud), ELGIRUBCO for the "winding up" of two dormant foreign subsidiaries,
# and IRMENERGY for an NCLT-convened meeting that was a routine scheme of
# arrangement, not insolvency. `desc` is a controlled vocabulary of ~75 exchange
# categories; the body is prose. Only the vocabulary is trustworthy, so the body
# is now used for exactly one thing: the direction of a credit-rating change.
_HARD_DESC = re.compile(
    r"corporate insolvency resolution"
    r"|resignation of statutory auditor"
    r"|action\(s\) (initiated|taken) or orders passed"
    r"|disruption of operations"
    r"|delayed/non-submission of financial results"
    r"|suspension of trading|delisting",
    re.I)
# The exchange is asking questions, or the board / auditors / raters are moving.
# Worth reading before you sell a put; not automatically disqualifying.
#
# "Price movement" and "Rumour Verification" are the highest-signal entries
# here: both are the exchange formally requiring a company to explain a move or
# a story — the same question this module exists to ask, asked by the regulator.
_WATCH_DESC = re.compile(
    r"news verification|rumour verification|clarification|price movement"
    r"|credit rating"
    r"|change in management|change in director|change in auditors"
    r"|resignation|cessation|retirement|change in company secretary"
    r"|disclosure of material issue|pendency of litigation"
    r"|licenses/ ?regulatory approvals"
    r"|restructuring|amalgamation/merger|one time settlement",
    re.I)
_RATING_BAD = re.compile(r"downgrad|revised downward|negative outlook|\bdefault\b", re.I)


def _sev(desc: str, text: str) -> str:
    """hard | watch | info — classified on the category, see the note above."""
    d = desc or ""
    if _HARD_DESC.search(d):
        return "hard"
    if re.search(r"credit rating", d, re.I):
        # the only place the body gets a vote: an upgrade is not a warning
        return "hard" if _RATING_BAD.search(f"{d} {text or ''}") else "watch"
    if _WATCH_DESC.search(d):
        return "watch"
    return "info"


def _parse_dt(s: str):
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def fetch_announcements(days: int = FETCH_DAYS) -> dict:
    """Whole-market NSE filings for the last `days`, indexed by symbol.

    One request covers every name we could possibly scan, so this stays cheap
    no matter how many ideas come back. Returns {"ok", "by_symbol", "error"}.
    """
    ses = csp._nse_session()
    if ses is None:
        return {"ok": False, "error": "NSE handshake failed", "by_symbol": {}}
    now = datetime.now()
    path = NSE_ANN.format(frm=(now - timedelta(days=days)).strftime("%d-%m-%Y"),
                          to=now.strftime("%d-%m-%Y"))
    try:
        r = ses.get(csp.NSE_HOME + path, timeout=30,
                    headers={"Accept": "*/*",
                             "Referer": csp.NSE_HOME + NSE_ANN_REFERER})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "by_symbol": {}}
    if not isinstance(data, list):
        data = data.get("data") or []

    by_symbol, names = {}, {}
    for it in data:
        sym = (it or {}).get("symbol")
        dt = _parse_dt(it.get("an_dt") or "")
        if not sym or not dt:
            continue
        desc = (it.get("desc") or "").strip()
        text = (it.get("attchmntText") or "").strip()
        by_symbol.setdefault(sym, []).append({
            "ts": dt.isoformat(timespec="seconds"),
            "age_h": round((now - dt).total_seconds() / 3600, 1),
            "desc": desc,
            "text": text[:400],
            "url": it.get("attchmntFile") or None,
            "severity": _sev(desc, text),
        })
        if it.get("sm_name"):
            names[sym] = it["sm_name"]
    for v in by_symbol.values():
        v.sort(key=lambda x: x["ts"], reverse=True)
    return {"ok": True, "by_symbol": by_symbol, "names": names,
            "total": len(data), "error": None}


_STOPWORDS = {"limited", "ltd", "india", "indian", "industries", "company",
              "corporation", "corp", "enterprises", "the", "and", "of",
              "services", "products", "holdings", "group"}


def _relevant(title: str, symbol: str, company: str | None) -> bool:
    """Is this headline actually ABOUT the company?

    Necessary, not fussy. Querying a short ticker returns sector SEO spam:
    "UPL" pulled back "Global Microbial Biostimulants Market Size" and
    "Hydrogel Seed Coating Market to Reach 210 Index by 2035". Surfacing those
    beside a row is worse than showing nothing — it invents a news event where
    there is none, which is the exact error this module exists to prevent.
    """
    t = (title or "").lower()
    words = set(re.findall(r"[a-z]+", t))
    if symbol and symbol.lower() in words:
        return True
    toks = [w for w in re.findall(r"[a-z]+", (company or "").lower())
            if len(w) > 3 and w not in _STOPWORDS]
    return any(w in t for w in toks[:2])


def google_news(query: str, hours: int = DEFAULT_WINDOW_H, limit: int = 6,
                symbol: str = "", company: str | None = None) -> list:
    """Third-party coverage — the half the filing feed cannot see.

    Google's `when:` operator is DAY-granular, so the hour window has to be
    applied client-side on pubDate; we ask for a day and discard the rest.
    Headlines that fail the relevance check are dropped, not shown.
    """
    url = GNEWS.format(q=urllib.parse.quote(f"{query} when:2d"))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": csp._BROWSER_UA})
        raw = urllib.request.urlopen(req, timeout=20).read()
        items = ET.fromstring(raw).findall(".//item")
    except Exception:
        return []
    now, out = datetime.now(), []
    for it in items:
        title = (it.findtext("title") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        dt = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                dt = datetime.strptime(pub, fmt)
                break
            except Exception:
                pass
        if dt is None:
            continue
        age = (now - dt.replace(tzinfo=None)).total_seconds() / 3600
        if age > hours or age < -2:
            continue
        if (symbol or company) and not _relevant(title, symbol, company):
            continue
        out.append({"ts": dt.replace(tzinfo=None).isoformat(timespec="seconds"),
                    "age_h": round(age, 1), "title": title[:220],
                    "source": (it.findtext("source") or "").strip() or None})
        if len(out) >= limit:
            break
    return out


def attach(rows: list, window_h: int = DEFAULT_WINDOW_H,
           deep_check: int = DEEP_CHECK_MAX, names: dict | None = None) -> dict:
    """Attach filings (and, for the names that need it, news) to scan rows.

    Every row gets `news` = {checked, filings, headlines, worst, ...}. A row
    with `checked: False` must NOT read as "nothing happened" — the UI has to
    show the difference between a clean check and a check that never ran.
    """
    ann = fetch_announcements()
    idx = ann.get("by_symbol") or {}
    nm = dict(ann.get("names") or {})
    if names:
        nm.update(names)

    hard = watch = 0
    need_deep = []
    for r in rows:
        sym = r["symbol"]
        items = [x for x in idx.get(sym, []) if x["age_h"] <= window_h]
        worst = ("hard" if any(x["severity"] == "hard" for x in items)
                 else "watch" if any(x["severity"] == "watch" for x in items)
                 else "info" if items else None)
        r["news"] = {
            "checked": bool(ann.get("ok")),
            "error": ann.get("error"),
            "window_h": window_h,
            "filings": items[:6],
            "n_filings": len(items),
            "fresh": sum(1 for x in items if x["age_h"] <= FRESH_H),
            "headlines": [],
            "worst": worst,
            "company": nm.get(sym),
        }
        if not ann.get("ok"):
            continue
        if worst == "hard":
            hard += 1
            r["risk_flags"] = list(r.get("risk_flags") or []) + [
                f"filing: {items[0]['desc']}"]
            r["score"] = max(0.0, round(r["score"] - 20, 1))
        elif worst == "watch":
            watch += 1
            r["score"] = max(0.0, round(r["score"] - 8, 1))
        # A sharp fall with no filing to explain it is exactly when the
        # third-party feed earns its keep — that is where a short-seller
        # report or a regulator's own order would show up.
        drop = r.get("cycle_drop_pct")
        d5 = r.get("d5_pct")
        if (worst in (None, "info", "watch")
                and ((drop is not None and drop <= -8)
                     or (d5 is not None and d5 <= -5))):
            need_deep.append(r)

    need_deep.sort(key=lambda r: (r.get("d5_pct") if r.get("d5_pct") is not None
                                  else 0))
    n_deep = 0
    for r in need_deep[:deep_check]:
        q = r["news"].get("company") or r["symbol"]
        heads = google_news(f'"{q}" share price', hours=window_h,
                            symbol=r["symbol"], company=r["news"].get("company"))
        r["news"]["headlines"] = heads
        n_deep += 1
        if heads and r["news"]["worst"] is None:
            r["news"]["worst"] = "info"

    if rows:
        rows.sort(key=lambda r: r["score"], reverse=True)
    return {"ok": bool(ann.get("ok")), "error": ann.get("error"),
            "filings_total": ann.get("total"), "window_h": window_h,
            "hard": hard, "watch": watch, "deep_checked": n_deep}

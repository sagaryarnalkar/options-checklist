"""Schooling: the nightly NIFTY call, made over PAST sessions one at a time,
from the start of the data, so experience exists before the first live night.

TWO PHASES
  prep  No Claude, minutes. Daily bars into index_daily (Kite since 2015;
        Fyers 1D fills what Kite could not give), participant files back to
        the start, and an indicator panel (panel.build) for every session -
        the same builder the live call uses, reading history.db for the
        intraday reads and the Fyers archive for futures. After prep alone,
        the live call already gets `history_says` over every past session.
  run   One Claude call per session, in date order. Each briefing is built
        exactly like the live one, then BLINDED: no dates, no index levels,
        no tools. Each session's call is graded with only the closes known by
        then; the post-mortem, lessons notebook and family scorecard carry
        forward session to session. Rows go to forecast_replay, never to the
        live record.

POINT IN TIME. Panels use data on/before the session (participants are cut,
futures and bars sliced). history_stats counts only outcomes known by the
session. Earlier replay calls are shown with any outcome or post-mortem not
yet known at the session masked, even if a later pass already graded them.

MEMORY. Blinding hides dates, but a model may still recognise a famous
session from its moves. Sessions before the model's training cutoff
(FORECAST_MODEL_CUTOFF) are therefore reported separately from those after
it; the after-cutoff record is the clean test, and the live record cleaner
still.

COST. One Claude Code run per session on the owner's subscription (two model
turns: the forecast, then the end of the turn). ~1,180 sessions since 2022 is
a large job, so it is paced (`pace_s` between sessions), capped per IST day
(`day_cap`), and on a usage-limit error sleeps until the reset time Claude
Code quotes instead of failing. Resumable: a stop, crash or app restart loses
nothing; finished sessions are skipped.

SHARED LIMIT. Schooling, the live call, Ask Claude and the owner's own Claude
use all draw on ONE subscription. A 5-hour session window that schooling
drains just before 20:30 starves the live call (all four retries fail). So
on weekdays no session STARTS from 15:15 to 22:45 IST: any window schooling
opened has reset by 20:15. A WEEKLY limit stops the run outright - waiting
days would lock the owner out of Claude meanwhile; that is their call.

CLI: python3 forecast_replay.py prep [--since D] [--until D] [--redo]
     python3 forecast_replay.py run [--since D] [--until D] [--model M]
                                    [--pace S] [--cap N] [--max N] [--prep]
     python3 forecast_replay.py status
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import db
import forecast as F
import panel

IST = timezone(timedelta(hours=5, minutes=30))
START = "2022-01-03"                       # first session of the Fyers archive
STATUS_FILE = db.DATA_DIR / "forecast_replay_status.json"
STOP_FILE = db.DATA_DIR / "forecast_replay_stop"
LOG_FILE = db.DATA_DIR / "forecast_replay.log"
QUIET = ((15, 15), (22, 45))   # weekdays: a 5-h window opened before 15:15 resets by 20:15
DEFAULT_PACE_S = 20
DEFAULT_DAY_CAP = 250
USAGE_BACKOFF_S = 1800
MAX_FAILS_IN_ROW = 5
# Claude Code says e.g. "You've hit your session limit · resets 8:10am (UTC)";
# the old "hit your limit" alone missed it and five sessions failed instead.
LIMIT_RE = re.compile(r"usage limit|rate.?limit|limit (reached|exceeded)|\b429\b|\b529\b|"
                      r"overloaded|too many requests|hit your (\w+[ -])*limit|"
                      r"(session|weekly|daily|opus|sonnet|\d+.hour) limit|out of (extra )?usage|"
                      r"quota", re.I)
WEEKLY_RE = re.compile(r"weekly limit|week(ly)? usage", re.I)
RESET_RE = re.compile(r"resets?\s+(?:at\s+)?(?:(?P<mon>[A-Z][a-z]{2})[a-z]*\s+(?P<day>\d{1,2}),?\s+"
                      r"(?:at\s+)?)?(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)?"
                      r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_]+)*)\))?", re.I)
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
AUTH_RE = re.compile(r"\b401\b|unauthori[sz]ed|invalid (api key|token|x-api-key)|"
                     r"authentication_error|please run /login|oauth token", re.I)


# ----------------------------------------------------------------- status --

def _status(**kw) -> None:
    cur = read_status()
    cur.update(kw, pid=os.getpid(), updated=datetime.now(IST).isoformat(timespec="seconds"))
    try:
        STATUS_FILE.write_text(json.dumps(cur, default=str))
    except Exception:
        pass


def read_status() -> dict:
    try:
        d = json.loads(STATUS_FILE.read_text())
    except Exception:
        return {"running": False}
    if d.get("running") and d.get("pid"):
        try:
            os.kill(int(d["pid"]), 0)
        except (OSError, ValueError):
            d.update(running=False, note="the process ended without finishing (restart?) - "
                                         "start it again to resume")
    return d


def mark_starting(pid: int, args: list) -> None:
    try:
        STATUS_FILE.write_text(json.dumps({
            "running": True, "pid": pid, "phase": "starting", "args": args,
            "updated": datetime.now(IST).isoformat(timespec="seconds")}))
    except Exception:
        pass


def _log(msg: str) -> None:
    print(f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------- prep --

def _arch():
    p = db.DATA_DIR / "fyers_hist.db"
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    return c


def _hist():
    p = db.DATA_DIR / "history.db"
    if not p.exists():
        return None
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _yesterday() -> str:
    return (datetime.now(IST).date() - timedelta(days=1)).isoformat()


def prep(since: str = START, until: Optional[str] = None, redo: bool = False,
         participants_too: bool = True) -> dict:
    """Daily bars, participant files and a panel for every session. No Claude."""
    until = until or _yesterday()
    out = {"since": since, "until": until}
    _status(running=True, phase="prep", step="daily bars", since=since, until=until)
    arch = _arch()
    with db.get_conn() as conn:
        try:
            from kite_auth import get_kite_from_cache
            kite = get_kite_from_cache()
        except Exception:
            kite = None
        if kite is not None:
            out["kite_daily"] = panel.refresh_index_daily(kite, conn, F.DAILY_SINCE)
        else:
            out["kite_daily"] = "no Kite session - using the Fyers archive's daily bars only"
        if arch is not None:
            out["fyers_daily"] = panel.fill_index_daily_from_fyers(conn, arch)
        _log(f"daily bars: {out.get('kite_daily')} / {out.get('fyers_daily')}")
        if participants_too:
            _status(step="participant files (one NSE request per missing session)")
            import participants
            days = (datetime.now(IST).date() - date.fromisoformat(since)).days + 45
            try:
                out["participants"] = participants.backfill(conn, days=days, pause=0.25)
            except Exception as e:
                out["participants"] = f"{type(e).__name__}: {e}"
            _log(f"participants: {out['participants']}")
        days = [r[0] for r in conn.execute(
            "SELECT d FROM index_daily WHERE sym='NIFTY' AND d>=? AND d<=? ORDER BY d",
            (since, until))]
        have = set() if redo else {r[0] for r in conn.execute(
            "SELECT d FROM forecast_panel WHERE underlying='NIFTY'")}
        todo = [d for d in days if d not in have]
        src = panel.Sources(conn, hist=_hist(), arch=arch)
        built = failed = 0
        t0 = time.time()
        for i, d in enumerate(todo):
            if STOP_FILE.exists():
                out["stopped"] = True
                break
            try:
                p = panel.build(src, d)
                if p is None:
                    failed += 1
                    continue
                panel.save(conn, d, p, "archive" if src.hist is not None else "live-db")
                built += 1
            except Exception as e:
                failed += 1
                _log(f"panel {d}: {type(e).__name__}: {e}")
            if i % 20 == 0 or i == len(todo) - 1:
                el = time.time() - t0
                _status(step="panels", progress=f"{i + 1}/{len(todo)}", day=d,
                        eta_min=round((len(todo) - i - 1) * el / max(i + 1, 1) / 60, 1))
        out.update(sessions=len(days), panels_built=built, panels_failed=failed,
                   panels_existing=len(days) - len(todo))
    _log(f"prep done: {out}")
    return out


# -------------------------------------------------------------------- run --

def _mask(r: dict, d: str) -> dict:
    """An earlier call as it stood on session d: nothing known only later."""
    r = dict(r)
    if not r.get("next_session") or r["next_session"] > d:
        r.update(ret1=None, next_session=None, next_close=None, postmortem=None)
    if not r.get("s5_session") or r["s5_session"] > d:
        r.update(ret5=None, s5_session=None, s5_close=None)
    return r


def replay_prompt(conn, d: str, seq: int) -> tuple:
    """The blinded briefing for session d, point in time."""
    bars = F.closes(conn, until=d)
    if not bars or bars[-1]["d"] != d:
        raise RuntimeError(f"no daily close for {d}")
    F.grade(conn, bars, F.REPLAY)
    row = conn.execute("SELECT panel_json, states_json FROM forecast_panel WHERE underlying=? "
                       "AND d=?", (F.UNDERLYING, d)).fetchone()
    if not row:
        raise RuntimeError(f"no panel for {d} - run prep")
    p, today = json.loads(row[0]), json.loads(row[1])
    hs = panel.history_stats(panel.stats_rows(conn, before=d), today, known_by=d)
    rows = [_mask(r, d) for r in F._load(conn, F.REPLAY) if r["status"] == "ok" and r["as_of"] < d]
    prev = next((r for r in reversed(rows) if r.get("next_session") == d), None)
    base = F.baselines(bars)
    header = F._header(d, base, blind=True, seq=seq)
    text = F.compose("replay", header, panel.blind(p), hs, bars, rows,
                     F._latest_lessons(rows), prev)
    return text, {**base, "ref_close": bars[-1]["c"], "has_prev": prev is not None}


def limit_reset(err: str, now: datetime) -> Optional[datetime]:
    """When a usage limit lifts, from Claude Code's own message: 'resets
    8:10am (UTC)', 'resets 3pm', 'resets Nov 12, 8am (Asia/Kolkata)', or the
    older 'usage limit reached|<epoch>'. None when it cannot be read."""
    from zoneinfo import ZoneInfo
    m = re.search(r"\|(\d{10})\b", err)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), IST)
    m = RESET_RE.search(err)
    if not m:
        return None
    h, mi, ap = int(m["h"]), int(m["m"] or 0), (m["ap"] or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    try:
        tz = ZoneInfo(m["tz"]) if m["tz"] else datetime.now().astimezone().tzinfo
    except Exception:
        tz = timezone.utc if (m["tz"] or "").upper() in ("UTC", "GMT") else None
    if tz is None:
        return None
    loc = now.astimezone(tz)
    if m["mon"]:
        mon = MONTHS.get(m["mon"][:3].lower())
        if not mon:
            return None
        t = loc.replace(month=mon, day=int(m["day"]), hour=h, minute=mi, second=0, microsecond=0)
        if t < loc - timedelta(days=1):
            t = t.replace(year=t.year + 1)
    else:
        t = loc.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= loc - timedelta(minutes=10):     # a reset minutes ago = clock skew, not tomorrow
            t += timedelta(days=1)
    return t.astimezone(IST)


def _quiet_left(now: datetime) -> int:
    if now.weekday() >= 5:                      # no live call on weekends
        return 0
    a = now.replace(hour=QUIET[0][0], minute=QUIET[0][1], second=0, microsecond=0)
    b = now.replace(hour=QUIET[1][0], minute=QUIET[1][1], second=0, microsecond=0)
    return int((b - now).total_seconds()) if a <= now < b else 0


def _sleep(secs: int, why: str) -> bool:
    """Sleep in steps so a stop request is honoured. False = stopped."""
    until = datetime.now(IST) + timedelta(seconds=secs)
    _status(paused=why, resume_at=until.isoformat(timespec="seconds"))
    while datetime.now(IST) < until:
        if STOP_FILE.exists():
            return False
        time.sleep(min(15, max(1, (until - datetime.now(IST)).total_seconds())))
    _status(paused=None, resume_at=None)
    return True


def run(since: str = START, until: Optional[str] = None, model: Optional[str] = None,
        pace_s: int = DEFAULT_PACE_S, day_cap: int = DEFAULT_DAY_CAP,
        max_sessions: Optional[int] = None, do_prep: bool = False) -> dict:
    st = read_status()
    if st.get("running") and int(st.get("pid") or 0) not in (0, os.getpid()):
        return {"ok": False, "reason": f"another replay is running (pid {st.get('pid')})"}
    try:
        os.nice(10)
    except Exception:
        pass
    try:
        STOP_FILE.unlink()
    except Exception:
        pass
    until = until or _yesterday()
    if do_prep:
        prep(since, until)
    import assistant as A
    cfg = A.configured()
    if not (cfg["sdk_installed"] and cfg["auth_ok"]):
        res = {"ok": False, "reason": "Claude is not configured on the server (SDK / token)"}
        _status(running=False, phase="idle", **res)
        return res
    with db.get_conn() as conn:
        all_days = [r[0] for r in conn.execute(
            "SELECT d FROM forecast_panel WHERE underlying=? AND d>=? ORDER BY d",
            (F.UNDERLYING, START))]
    seq_of = {d: i + 1 for i, d in enumerate(all_days)}
    days = [d for d in all_days if since <= d <= until]
    _status(running=True, phase="run", since=since, until=until, model=model or F.MODEL
            or "plan default", pace_s=pace_s, day_cap=day_cap, total=len(days),
            paused=None, error=None, stopped=False, step=None, progress=None, day=None)
    _log(f"run: {len(days)} sessions {since}..{until}, model={model or F.MODEL or 'default'}")
    made = failed = in_row = 0
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    t0 = time.time()
    stopped = False
    i = 0
    while i < len(days):
        d = days[i]
        if STOP_FILE.exists() or (max_sessions and made >= max_sessions):
            stopped = True
            break
        with db.get_conn() as conn:
            ex = conn.execute("SELECT status FROM forecast_replay WHERE underlying=? AND as_of=?",
                              (F.UNDERLYING, d)).fetchone()
            today = datetime.now(IST).date().isoformat()
            n_today = conn.execute("SELECT COUNT(*) FROM forecast_replay WHERE "
                                   "substr(created_at,1,10)=?", (today,)).fetchone()[0]
        if ex and ex[0] == "ok":
            i += 1
            continue
        q = _quiet_left(datetime.now(IST))
        if q and not _sleep(q, "keeping clear of the live 20:30 call"):
            stopped = True
            break
        if n_today >= day_cap:
            nxt = (datetime.now(IST) + timedelta(days=1)).replace(hour=0, minute=5, second=0)
            if not _sleep(int((nxt - datetime.now(IST)).total_seconds()),
                          f"daily cap of {day_cap} sessions reached"):
                stopped = True
                break
            continue
        tools: list = []
        ans = prompt = None
        base = {}
        try:
            with db.get_conn() as conn:
                prompt, base = replay_prompt(conn, d, seq_of[d])
            ans = asyncio.run(asyncio.wait_for(
                F.ask(F.system_text("replay"), prompt, ctx=None, model=model), F.RUN_TIMEOUT_S))
            if not ans["structured"]:
                raise RuntimeError(ans["error"] or "no structured output")
            f = F.normalize(ans["structured"])
        except Exception as e:
            err = f"{type(e).__name__}: {e}"[:600]
            if LIMIT_RE.search(err):
                now = datetime.now(IST)
                at = limit_reset(err, now)
                if WEEKLY_RE.search(err):
                    _status(error=f"weekly Claude limit - stopped (resets "
                                  f"{at.strftime('%a %d %b %H:%M IST') if at else 'later'}); "
                                  f"start again after that: {err[:200]}")
                    _log(f"{d}: weekly limit - stopping ({err[:160]})")
                    break
                wait = int((at - now).total_seconds()) + 120 if at else USAGE_BACKOFF_S
                wait = max(60, min(wait, 6 * 3600))
                _log(f"{d}: usage limit - waiting {wait // 60} min ({err[:120]})")
                if not _sleep(wait, "Claude usage limit"
                              + (f" (resets {at.strftime('%H:%M')} IST)" if at else "")):
                    stopped = True
                    break
                continue                                   # same session again
            if AUTH_RE.search(err):
                _status(error=err)
                _log(f"{d}: credentials problem - stopping: {err}")
                break
            with db.get_conn() as conn:
                F.store(conn, d, "failed", "replay", base, error=err, prompt=prompt, tools=tools,
                        ans=ans, table=F.REPLAY, seq=seq_of[d])
            failed += 1
            in_row += 1
            _log(f"{d}: failed {err[:200]}")
            if in_row >= MAX_FAILS_IN_ROW:
                _status(error=f"{in_row} sessions failed in a row - stopped: {err}")
                break
            i += 1
            continue
        with db.get_conn() as conn:
            F.store(conn, d, "ok", "replay", base, f, prompt=prompt, tools=tools, ans=ans,
                    table=F.REPLAY, seq=seq_of[d])
        made += 1
        in_row = 0
        for k in usage:
            usage[k] += int((ans.get("usage") or {}).get(k) or 0)
        left = sum(1 for x in days[i + 1:])
        per = (time.time() - t0) / max(made + failed, 1)
        _status(progress=f"{i + 1}/{len(days)}", day=d, made=made, failed=failed,
                last_p_up=f["p1"], usage=usage,
                eta_hours=round(left * per / 3600, 1),
                model_seen=ans.get("model"))
        _log(f"{d} S{seq_of[d]}: p_up {f['p1']} e {f['e1']} ({ans.get('model')})")
        i += 1
        if pace_s and i < len(days) and not _sleep(pace_s, "pacing"):
            stopped = True
            break
    # grade everything the closes allow, so the scorecard is complete
    with db.get_conn() as conn:
        F.grade(conn, F.closes(conn), F.REPLAY)
    res = {"ok": True, "made": made, "failed": failed, "stopped": stopped,
           "elapsed_hours": round((time.time() - t0) / 3600, 2), "usage": usage}
    _status(running=False, phase="idle", paused=None, **res)
    _log(f"run done: {res}")
    return res


# ----------------------------------------------------------------- public --

def plan(conn, since: str, until: Optional[str] = None) -> dict:
    """What a run from `since` would replay: the same session list run() walks
    (panels in [since, until]) minus the ones already called."""
    until = until or _yesterday()
    n = conn.execute("SELECT COUNT(*) FROM forecast_panel WHERE underlying=? AND d>=? AND d>=? "
                     "AND d<=?", (F.UNDERLYING, START, since, until)).fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM forecast_panel p JOIN forecast_replay r "
                        "ON r.underlying=p.underlying AND r.as_of=p.d AND r.status='ok' "
                        "WHERE p.underlying=? AND p.d>=? AND p.d>=? AND p.d<=?",
                        (F.UNDERLYING, START, since, until)).fetchone()[0]
    return {"since": since, "until": until, "sessions": n, "done": done, "left": n - done}


def public(conn, since: Optional[str] = None) -> dict:
    rows = F._load(conn, F.REPLAY)
    ok = [r for r in rows if r["status"] == "ok"]
    n_panels, p_first, p_last = conn.execute(
        "SELECT COUNT(*), MIN(d), MAX(d) FROM forecast_panel WHERE underlying=?",
        (F.UNDERLYING,)).fetchone()
    calls = [{"as_of": r["as_of"], "seq": r.get("seq"), "p1": r["p1"], "e1": r["e1"],
              "ret1": r.get("ret1"), "p5": r["p5"], "ret5": r.get("ret5"),
              "summary": r["summary"], "postmortem": r.get("postmortem"),
              "model": r.get("model")} for r in reversed(ok[-40:])]
    return {"ok": True, "status": read_status(), "panels": {"count": n_panels, "first": p_first,
                                                           "last": p_last},
            "summary": F.schooling_summary(conn), "calls": calls,
            "failed": sum(1 for r in rows if r["status"] != "ok"),
            "plan": plan(conn, since) if since else None,
            "defaults": {"since": START, "pace_s": DEFAULT_PACE_S, "day_cap": DEFAULT_DAY_CAP,
                         "model_cutoff": F.MODEL_CUTOFF, "model": F.MODEL}}


def stop() -> None:
    STOP_FILE.write_text(datetime.now(IST).isoformat())


# -------------------------------------------------------------------- CLI --

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prep", "run", "status"])
    ap.add_argument("--since", default=START)
    ap.add_argument("--until")
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--pace", type=int, default=DEFAULT_PACE_S)
    ap.add_argument("--cap", type=int, default=DEFAULT_DAY_CAP)
    ap.add_argument("--max", type=int)
    ap.add_argument("--prep", action="store_true")
    a = ap.parse_args()
    db.init_db()
    if a.cmd == "status":
        print(json.dumps(read_status(), indent=2))
    elif a.cmd == "prep":
        try:
            STOP_FILE.unlink()
        except Exception:
            pass
        res = prep(a.since, a.until, a.redo)
        _status(running=False, phase="idle", last_prep=res)
    else:
        print(json.dumps(run(a.since, a.until, a.model, a.pace, a.cap, a.max, a.prep), default=str))

/**
 * Toolbar chip with the dashboard's Kite account: available margin, net P&L of today's positions and the
 * open count (the dashboard's /account, read through this terminal's proxy — server/dashboard.mjs).
 * Logged out → a "Log in to Kite" link to the dashboard's login (top window: Kite refuses to be framed).
 *
 * Auto-login (the dashboard's old behaviour, now here because the dashboard hands the owner straight to
 * this terminal): the first time in a local day that the account reads logged out, the whole window
 * bounces through the dashboard's /login. With a live Kite web session Zerodha returns at once; Kite's
 * callback goes to the dashboard, which hands back to the terminal. Once per day, so a declined login or
 * a logout does not loop. Never from inside a frame (browsers block that navigation).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { LogIn } from "lucide-react";
import { PROXY_BASE } from "@/lib/api/proxyClient";
import { DESK_URL, formatInr, maybeAutoLogin } from "@/lib/desk";
import { cn } from "@/lib/utils";

interface Account {
  logged_in: boolean;
  margin_available?: number | null;
  margin_used?: number | null;
  day_pnl?: number | null;
  n_open_positions?: number | null;
  margin_error?: string;
  pnl_error?: string;
}

const REFRESH_MS = 60_000;
/** Logged out: look again sooner, so a login done in another tab shows within seconds. */
const REFRESH_LOGGED_OUT_MS = 10_000;

export function DeskAccountChip({ className }: { className?: string }) {
  const [acct, setAcct] = useState<Account | null>(null);

  useEffect(() => {
    if (!DESK_URL) return;
    let alive = true;
    let timer: number | undefined;
    const load = async () => {
      window.clearTimeout(timer);
      let next = REFRESH_MS;
      try {
        const r = await fetch(`${PROXY_BASE}/api/dashboard/account`, { cache: "no-store" });
        const j = (await r.json()) as Account;
        if (alive && r.ok && typeof j?.logged_in === "boolean") {
          setAcct(j);
          if (!j.logged_in) {
            next = REFRESH_LOGGED_OUT_MS;
            let storage: Storage | null = null;
            try {
              storage = window.localStorage;
            } catch {
              storage = null;
            }
            if (maybeAutoLogin(storage, (u) => window.location.assign(u))) return;
          }
        }
      } catch {
        /* keep the last reading */
      }
      if (alive) timer = window.setTimeout(load, next);
    };
    void load();
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      alive = false;
      window.clearTimeout(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  if (!DESK_URL || !acct) return null;

  const pill = "flex h-7 shrink-0 items-center gap-2 rounded-pill bg-control-track px-2.5 text-body-sm tabular-nums transition-colors hover:text-foreground focus-visible:shadow-focus focus-visible:outline-none";
  if (!acct.logged_in) {
    return (
      <a href={`${DESK_URL}/login`} target="_top" className={cn(pill, "text-primary-ink", className)} title="The dashboard is not logged in to Kite today">
        <LogIn className="h-3.5 w-3.5" strokeWidth={2} aria-hidden="true" />
        Log in to Kite
      </a>
    );
  }
  const pnl = typeof acct.day_pnl === "number" ? acct.day_pnl : null;
  return (
    <Link to="/desk/positions" className={cn(pill, "text-muted-foreground", className)} title="Kite account — open Positions">
      <span title="Available margin (equity live balance)">
        <span className="hidden text-muted-foreground 2xl:inline">avail </span>
        <span className="font-medium text-foreground">{formatInr(acct.margin_available)}</span>
      </span>
      <span className="h-3 w-px bg-separator" aria-hidden="true" />
      <span title="Net P&L across today's positions (realized + MTM)">
        <span className="hidden text-muted-foreground 2xl:inline">P&amp;L </span>
        <span className={cn("font-medium", pnl === null ? "text-foreground" : pnl < 0 ? "text-bearish" : "text-bullish")}>
          {formatInr(pnl)}
        </span>
      </span>
      {typeof acct.n_open_positions === "number" && (
        <span className="text-muted-foreground" title={`${acct.n_open_positions} open positions`}>
          ({acct.n_open_positions}<span className="hidden 2xl:inline"> open</span>)
        </span>
      )}
    </Link>
  );
}

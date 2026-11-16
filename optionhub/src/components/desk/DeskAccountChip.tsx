/**
 * Toolbar chip with the dashboard's Kite account: available margin, net P&L of today's positions and the
 * open count (the dashboard's /account, read through this terminal's proxy — server/dashboard.mjs).
 * Logged out → a "Log in to Kite" link to the dashboard's login (top window: Kite refuses to be framed).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { LogIn } from "lucide-react";
import { PROXY_BASE } from "@/lib/api/proxyClient";
import { DESK_URL, formatInr } from "@/lib/desk";
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

export function DeskAccountChip({ className }: { className?: string }) {
  const [acct, setAcct] = useState<Account | null>(null);

  useEffect(() => {
    if (!DESK_URL) return;
    let alive = true;
    const load = async () => {
      try {
        const r = await fetch(`${PROXY_BASE}/api/dashboard/account`, { cache: "no-store" });
        const j = (await r.json()) as Account;
        if (alive && r.ok && typeof j?.logged_in === "boolean") setAcct(j);
      } catch {
        /* keep the last reading */
      }
    };
    void load();
    const id = window.setInterval(load, REFRESH_MS);
    const onFocus = () => void load();
    window.addEventListener("focus", onFocus);
    return () => {
      alive = false;
      window.clearInterval(id);
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

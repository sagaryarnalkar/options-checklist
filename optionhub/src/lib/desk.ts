/**
 * HeroZero desk: the options-checklist dashboard's own tabs (Dashboard, Positions, By Strategy, OI Flow,
 * Paper Trades, Cash Puts) inside this terminal. They live under /desk/* and are shown in ONE persistent
 * iframe of the dashboard in embed mode (components/desk/DeskFrame.tsx); switching tabs is a postMessage,
 * not a reload.
 *
 * VITE_DASHBOARD_URL (set by the dashboard app's build, option_hub.py) is the dashboard's origin. Without
 * it the desk is off: no routes, no header chip. Pure data — vite.config.ts imports routes.ts in Node,
 * where import.meta.env does not exist.
 */

function readDeskUrl(): string {
  let env: string | undefined;
  try {
    env = import.meta.env?.VITE_DASHBOARD_URL as string | undefined;
  } catch {
    env = undefined;
  }
  const v = (env ?? "").trim().replace(/\/+$/, "");
  return /^https?:\/\/[^/]+$/.test(v) ? v : "";
}

/** Dashboard origin, e.g. "https://sagy-options.duckdns.org"; "" when the desk is off. */
export const DESK_URL: string = readDeskUrl();

export interface DeskTab {
  /** The dashboard's data-tab key. */
  id: string;
  path: string;
  label: string;
  title: string;
  icon: string;
  description: string;
  keywords: string[];
}

export const DESK_TABS: readonly DeskTab[] = [
  { id: "dashboard", path: "/desk/dashboard", label: "Dashboard", title: "HeroZero Dashboard", icon: "Gauge",
    description: "Live data, market direction, triggers and today's strategy recommendations.", keywords: ["checklist", "signals", "triggers", "recommendations", "claude", "forecast"] },
  { id: "positions", path: "/desk/positions", label: "Positions", title: "Positions", icon: "BriefcaseBusiness",
    description: "The live Kite book: legs, greeks, payoff, margin and Ask Claude.", keywords: ["book", "kite", "greeks", "margin", "payoff", "pnl"] },
  { id: "strategy", path: "/desk/strategy", label: "By Strategy", title: "By Strategy", icon: "ListChecks",
    description: "The nine saved strategies with concrete trade recommendations.", keywords: ["golden goose", "panther", "nidhi kalash", "ocean treasure", "batman", "calendar"] },
  { id: "oiflow", path: "/desk/oi-flow", label: "OI Flow", title: "OI Flow", icon: "Radar",
    description: "Recorded per-minute order flow, LLT prints, holds and the signal scorecard.", keywords: ["order flow", "llt", "fund flow", "holds", "scorecard", "wcash"] },
  { id: "paper", path: "/desk/paper-trades", label: "Paper Trades", title: "Paper Trades", icon: "NotebookPen",
    description: "The 10-lot paper ledger of every strategy, net of costs.", keywords: ["ledger", "paper", "pnl", "net", "trades"] },
  { id: "csp", path: "/desk/cash-puts", label: "Cash Puts", title: "Cash-Secured Puts", icon: "HandCoins",
    description: "Equity F&O put-selling scanner with surveillance and news checks.", keywords: ["csp", "puts", "scanner", "assignment", "yield"] },
];

/** The desk tab shown at `pathname`, or null when it is not a desk page. */
export function deskTabFromPath(pathname: string): DeskTab | null {
  const p = pathname.replace(/\/+$/, "");
  return DESK_TABS.find((t) => t.path === p) ?? null;
}

/** The dashboard in embed mode: no header or tab bar, the given tab, a theme matching this terminal. */
export function deskFrameUrl(tab: string, dark: boolean, base: string = DESK_URL): string {
  const q = new URLSearchParams({ embed: "1", tab, theme: dark ? "hub-dark" : "hub-light", skin: "fintech" });
  return `${base}/?${q.toString()}`;
}

/** "₹6,52,869" / "−₹28,00,056" (Indian grouping, real minus sign); "—" when not a number. */
export function formatInr(v: unknown): string {
  const n = typeof v === "number" ? v : Number.NaN;
  if (!Number.isFinite(n)) return "—";
  const s = `₹${Math.round(Math.abs(n)).toLocaleString("en-IN")}`;
  return n < 0 ? `−${s}` : s;
}

/** Once-a-day Kite login bounce (the dashboard's old auto-login), used by the toolbar account chip. */
const AUTOLOGIN_KEY = "hz_autologin_day";

const todayLocal = () => new Date().toLocaleDateString("en-CA");

/** True when this call started the once-a-day login bounce. */
export function maybeAutoLogin(
  storage: Pick<Storage, "getItem" | "setItem"> | null,
  go: (url: string) => void,
  framed: boolean = typeof window !== "undefined" && window.top !== window.self,
  base: string = DESK_URL,
): boolean {
  if (!base || framed || !storage) return false;
  try {
    if (storage.getItem(AUTOLOGIN_KEY) === todayLocal()) return false;
    storage.setItem(AUTOLOGIN_KEY, todayLocal());
  } catch {
    return false;
  }
  go(`${base}/login`);
  return true;
}

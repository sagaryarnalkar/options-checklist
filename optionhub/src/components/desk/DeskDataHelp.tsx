/**
 * options-checklist: in the dashboard's build there is no "local proxy" to start — data comes through the
 * dashboard's server. This replaces the upstream "Start proxy" help wherever sample data is labelled.
 */
import { useState } from "react";
import { HelpCircle, LogIn, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { probeProxy } from "@/lib/api/proxyClient";
import { DESK_URL } from "@/lib/desk";
import { cn } from "@/lib/utils";

export function DeskDataHelp({ variant = "link", onRecovered }: { variant?: "link" | "button"; onRecovered?: () => void }) {
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<"" | "ok" | "failed">("");
  const check = async () => {
    setChecking(true);
    setResult("");
    const ok = await probeProxy({ force: true }).catch(() => false);
    setChecking(false);
    setResult(ok ? "ok" : "failed");
    if (ok) onRecovered?.();
  };
  const trigger =
    variant === "button" ? (
      <Button type="button" variant="outline" className="shrink-0">
        <HelpCircle className="!size-3.5" aria-hidden="true" />
        Why sample?
      </Button>
    ) : (
      <button
        type="button"
        className="inline-flex h-6 shrink-0 items-center gap-1 rounded-capsule px-2 font-semibold text-primary-ink underline-offset-2 transition-colors duration-fast hover:underline focus-visible:outline-none"
      >
        <HelpCircle className="h-3 w-3" aria-hidden="true" />
        Why sample?
      </button>
    );
  return (
    <Popover>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)] space-y-3 p-3.5 text-body-sm">
        <div className="space-y-1.5">
          <p className="font-semibold text-foreground">Where the data comes from</p>
          <p className="leading-relaxed text-muted-foreground">
            Through this terminal's server: option chains and F&amp;O stock quotes from your Kite login, session
            history from the dashboard's minute-by-minute recorder, indices and FII/DII from NSE.
          </p>
          <p className="leading-relaxed text-muted-foreground">
            A "Sample" tag means that source could not answer: the dashboard is not logged in to Kite today, NSE did
            not respond, or nothing has been recorded for that symbol yet.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {DESK_URL && (
            <Button asChild type="button" variant="outline">
              <a href={`${DESK_URL}/login`} target="_top">
                <LogIn className="!size-3.5" aria-hidden="true" />
                Log in to Kite
              </a>
            </Button>
          )}
          <Button type="button" variant="outline" onClick={() => void check()} disabled={checking}>
            <RefreshCw className={cn("!size-3.5", checking && "motion-safe:animate-spin")} aria-hidden="true" />
            {checking ? "Checking…" : "Check again"}
          </Button>
          <span role="status" aria-live="polite" className={result === "ok" ? "text-bullish" : "text-muted-foreground"}>
            {result === "ok" ? "Connected." : result === "failed" ? "Still not answering." : ""}
          </span>
        </div>
      </PopoverContent>
    </Popover>
  );
}

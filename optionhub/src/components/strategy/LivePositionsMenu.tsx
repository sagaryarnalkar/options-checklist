/**
 * options-checklist: "Live positions" in the Strategy Builder — the dashboard's live Kite book, one item per
 * underlying; picking one loads its option and futures legs (entry = Kite average price).
 */
import { useState } from "react";
import { Briefcase, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { formatINR } from "@/lib/options";
import { fetchLiveBook, positionLegs, type LiveBook, type LivePositionGroup } from "@/lib/livePositions";
import { cn } from "@/lib/utils";

export function LivePositionsMenu({ onPick }: { onPick: (group: LivePositionGroup, asOf: string) => void }) {
  const [book, setBook] = useState<LiveBook | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      setBook(await fetchLiveBook());
    } catch (e) {
      setBook({ ok: false, reason: `Could not reach the dashboard: ${(e as Error).message}` });
    } finally {
      setLoading(false);
    }
  };

  const groups = (book?.groups ?? []).filter((g) => positionLegs(g).legs.length > 0);
  return (
    <DropdownMenu onOpenChange={(o) => o && void load()}>
      <DropdownMenuTrigger asChild>
        <Button type="button" size="sm" variant="outline" title="Open a live Kite position here to analyse and modify it">
          <Briefcase /> Live positions
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-[260px]">
        <DropdownMenuLabel>Your Kite positions{book?.as_of ? ` · ${String(book.as_of).slice(11, 16)} IST` : ""}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        {loading ? (
          <DropdownMenuItem disabled>
            <Loader2 className="animate-spin" /> Loading…
          </DropdownMenuItem>
        ) : !book?.ok ? (
          <DropdownMenuItem disabled className="whitespace-normal">
            {book?.reason || "No positions"}
          </DropdownMenuItem>
        ) : !groups.length ? (
          <DropdownMenuItem disabled>No open option or futures positions.</DropdownMenuItem>
        ) : (
          groups.map((g) => {
            const n = positionLegs(g).legs.length;
            return (
              <DropdownMenuItem key={g.underlying} onSelect={() => onPick(g, book.as_of ?? new Date().toISOString())} className="flex items-baseline justify-between gap-4">
                <span>
                  <span className="font-semibold">{g.underlying}</span>
                  <span className="text-muted-foreground"> · {n} leg{n === 1 ? "" : "s"}</span>
                </span>
                {typeof g.pnl === "number" ? (
                  <span className={cn("font-num tabular-nums", g.pnl < 0 ? "text-bearish" : "text-bullish")}>{formatINR(g.pnl, { decimals: 0 })}</span>
                ) : null}
              </DropdownMenuItem>
            );
          })
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

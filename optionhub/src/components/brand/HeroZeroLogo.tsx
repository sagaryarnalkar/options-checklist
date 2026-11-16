/**
 * HeroZero mark and lockup (the options-checklist dashboard's logo): a crosshair in a square. Colours follow
 * the theme: the square takes the text colour, the ring and side ticks the window colour, and the centre and
 * vertical ticks the accent (orange here; the dashboard's own header uses its theme accent).
 */
import { cn } from "@/lib/utils";

interface MarkProps {
  size?: number;
  className?: string;
  /** Hide from assistive tech when a visible label sits next to it. */
  decorative?: boolean;
  title?: string;
}

export function HeroZeroMark({ size = 24, className, decorative = false, title = "HeroZero" }: MarkProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 38 38"
      className={cn("shrink-0", className)}
      role={decorative ? undefined : "img"}
      aria-hidden={decorative ? true : undefined}
      aria-label={decorative ? undefined : title}
      focusable="false"
    >
      <rect width="38" height="38" rx="3" fill="hsl(var(--foreground))" />
      <circle cx="19" cy="19" r="10.5" fill="none" stroke="hsl(var(--window))" strokeWidth="2.4" />
      <circle cx="19" cy="19" r="4" fill="hsl(var(--primary))" />
      <line x1="19" y1="2.5" x2="19" y2="7.5" stroke="hsl(var(--primary))" strokeWidth="2.2" />
      <line x1="19" y1="30.5" x2="19" y2="35.5" stroke="hsl(var(--primary))" strokeWidth="2.2" />
      <line x1="2.5" y1="19" x2="7.5" y2="19" stroke="hsl(var(--window))" strokeWidth="2.2" />
      <line x1="30.5" y1="19" x2="35.5" y2="19" stroke="hsl(var(--window))" strokeWidth="2.2" />
    </svg>
  );
}

/** Mark + "HEROZERO / TRADER · TERMINAL" wordmark. */
export function HeroZeroLogo({ size = 30, className }: { size?: number; className?: string }) {
  return (
    <span className={cn("flex min-w-0 items-center gap-2.5", className)}>
      <HeroZeroMark size={size} decorative />
      <span className="flex min-w-0 flex-col leading-none">
        <span className="truncate text-[17px] font-extrabold tracking-[-0.01em] text-foreground">
          HERO<span className="text-primary">ZERO</span>
        </span>
        <span className="mt-[5px] truncate text-[9px] font-semibold uppercase tracking-[0.3em] text-muted-foreground">
          Trader · Terminal
        </span>
      </span>
    </span>
  );
}

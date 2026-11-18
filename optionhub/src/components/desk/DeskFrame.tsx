/**
 * The dashboard's own tabs inside this terminal: ONE iframe of the dashboard in embed mode, kept alive once
 * opened (hidden while another page shows), so switching desk tabs or visiting other pages and coming back
 * keeps its state. Tab changes are a postMessage ({type: "hz:tab"}); only a theme change reloads it, because
 * the dashboard's charts resolve colours when they are built.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTheme } from "@/hooks/useTheme";
import { DESK_URL, deskFrameUrl, type DeskTab } from "@/lib/desk";
import { cn } from "@/lib/utils";

const DESK_ORIGIN = (() => {
  try {
    return DESK_URL ? new URL(DESK_URL).origin : "";
  } catch {
    return "";
  }
})();

/** Terminal pages the dashboard frame may open (#126: "⇢ Strategy Builder" on a Positions-tab group). */
const NAVIGABLE = /^\/strategy-builder\?positions=[A-Za-z0-9&_%-]{1,40}$/;

export function DeskFrame({ tab }: { tab: DeskTab | null }) {
  const { isDark } = useTheme();
  const navigate = useNavigate();
  const active = !!tab;
  const frame = useRef<HTMLIFrameElement>(null);
  const current = useRef<string>(tab?.id ?? "dashboard");
  if (tab) current.current = tab.id;
  // Built when first shown, and again (with the tab showing at that moment) when the theme flips.
  const [src, setSrc] = useState<string | null>(() => (tab && DESK_URL ? deskFrameUrl(tab.id, isDark) : null));
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    if (!DESK_URL || (!active && src === null)) return;
    const next = deskFrameUrl(current.current, isDark);
    setSrc((prev) => {
      if (prev === null) return next;
      // only the theme forces a reload; a tab change alone is a message
      const sameTheme = new URL(prev).searchParams.get("theme") === new URL(next).searchParams.get("theme");
      return sameTheme ? prev : next;
    });
  }, [active, isDark, src]);

  useEffect(() => setLoaded(false), [src]);

  // The dashboard asks the terminal to open one of its own pages: only from the dashboard's origin, only this
  // frame, only whitelisted paths.
  useEffect(() => {
    if (!DESK_ORIGIN) return;
    const onMessage = (e: MessageEvent) => {
      if (e.origin !== DESK_ORIGIN || e.source !== frame.current?.contentWindow) return;
      const d = e.data as { type?: unknown; path?: unknown } | null;
      if (d?.type === "hz:navigate" && typeof d.path === "string" && NAVIGABLE.test(d.path)) navigate(d.path);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [navigate]);

  useEffect(() => {
    if (!active || !loaded || !tab) return;
    frame.current?.contentWindow?.postMessage({ type: "hz:tab", tab: tab.id }, DESK_ORIGIN || "*");
  }, [active, loaded, tab]);

  if (!DESK_URL || src === null) return null;
  return (
    <div className={cn("relative min-h-0 flex-1", !active && "hidden")} aria-hidden={!active || undefined}>
      {!loaded && (
        <div className="absolute inset-0 grid place-items-center text-body-sm text-muted-foreground" role="status">
          Loading {tab?.label ?? "the dashboard"}…
        </div>
      )}
      <iframe
        ref={frame}
        src={src}
        title={tab ? `HeroZero ${tab.label}` : "HeroZero dashboard"}
        className={cn("absolute inset-0 h-full w-full border-0 bg-window", !loaded && "opacity-0")}
        onLoad={() => setLoaded(true)}
        allow="clipboard-write"
      />
    </div>
  );
}

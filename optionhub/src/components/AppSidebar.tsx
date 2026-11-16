import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { Monitor, Moon, Sun } from "lucide-react";
import { HeroZeroLogo, HeroZeroMark } from "@/components/brand/HeroZeroLogo";
import { NavLink } from "@/components/NavLink";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar";
import { useTheme, type ThemeMode } from "@/hooks/useTheme";
import { routesByGroup, type RouteDef } from "@/lib/routes";
import { routeIcon } from "@/lib/routeIcons";
import { cn } from "@/lib/utils";
import { PaletteToggle } from "@/components/PaletteToggle";
import { modKeyLabel } from "@/hooks/useKeyboardShortcuts";

const NAV_GROUPS = routesByGroup();

const THEME_OPTIONS: { mode: ThemeMode; label: string; icon: typeof Sun }[] = [
  { mode: "light", label: "Light", icon: Sun },
  { mode: "system", label: "System", icon: Monitor },
  { mode: "dark", label: "Dark", icon: Moon },
];

/**
 * "New" badges expire once the user has opened the page, so the badge keeps
 * meaning "you have not looked at this yet" instead of decorating most of the
 * nav. "Beta" badges are a status, not novelty, and never expire.
 */
const SEEN_KEY = "mc-nav-seen-v1";

function readSeen(): Set<string> {
  try {
    const raw = localStorage.getItem(SEEN_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function writeSeen(seen: Set<string>) {
  try {
    localStorage.setItem(SEEN_KEY, JSON.stringify([...seen]));
  } catch {
    /* storage blocked: badges simply stay visible */
  }
}

/** Active source-list row; on the accent pill the icon and text switch to the selection ink. */
const SELECTED_ROW =
  "bg-selection-unfocused font-medium text-foreground group-focus-within/source:bg-selection group-focus-within/source:text-selection-foreground group-focus-within/source:[&_svg]:text-selection-foreground";

/** "g c" → "G C" for display. */
const shortcutLabel = (s: string) => s.toUpperCase();

function NavItem({ route, collapsed, seen }: { route: RouteDef; collapsed: boolean; seen: boolean }) {
  const badge = route.badge === "New" && seen ? undefined : route.badge;
  const Icon = routeIcon(route.icon);
  const tooltip = route.shortcut ? `${route.navLabel}  (${shortcutLabel(route.shortcut)})` : route.navLabel;
  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild tooltip={collapsed ? tooltip : undefined} className={collapsed ? "!size-9 !p-0" : undefined}>
        <NavLink
          to={route.path}
          end={route.path === "/"}
          aria-label={collapsed ? route.navLabel : undefined}
          className={cn("group", collapsed && "justify-center")}
          // Grey pill while focus is elsewhere; accent pill while the sidebar itself has focus.
          activeClassName={SELECTED_ROW}
        >
          <Icon className="h-4 w-4 shrink-0 text-primary-ink" strokeWidth={1.75} aria-hidden="true" />
          {!collapsed && (
            <>
              <span className="min-w-0 flex-1 truncate">{route.navLabel}</span>
              {badge && (
                <span
                  className={cn(
                    "rounded-chip px-1.5 py-px text-micro font-semibold leading-[13px]",
                    badge === "Beta" ? "bg-wash-warning text-warning" : "bg-wash-brand text-[hsl(var(--orange-900))] dark:bg-primary/15 dark:text-primary-ink",
                    "group-focus-within/source:group-aria-[current=page]:bg-selection-foreground/15 group-focus-within/source:group-aria-[current=page]:text-selection-foreground",
                  )}
                >
                  {badge}
                </span>
              )}
              {route.shortcut && (
                <kbd className="hidden font-sans text-micro text-muted-foreground-subtle opacity-0 transition-opacity group-hover:opacity-100 group-aria-[current=page]:text-foreground/75 group-aria-[current=page]:opacity-100 group-focus-within/source:group-aria-[current=page]:text-selection-foreground xl:inline">
                  {shortcutLabel(route.shortcut)}
                </kbd>
              )}
            </>
          )}
        </NavLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );
}

function ThemeSwitch({ collapsed }: { collapsed: boolean }) {
  const { mode, setMode, isDark, toggle } = useTheme();
  if (collapsed) {
    return (
      <button
        type="button"
        onClick={toggle}
        className="flex h-9 w-9 items-center justify-center rounded-pill text-sidebar-foreground transition-colors hover:bg-control-track focus-visible:shadow-focus focus-visible:outline-none"
        title={isDark ? "Switch to light mode" : "Switch to dark mode"}
        aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      >
        {isDark ? <Sun className="h-4 w-4" strokeWidth={1.75} /> : <Moon className="h-4 w-4" strokeWidth={1.75} />}
      </button>
    );
  }
  return (
    // Small macOS segmented control.
    <div role="radiogroup" aria-label="Colour theme" className="grid h-7 grid-cols-3 rounded-pill bg-control-track p-0.5">
      {THEME_OPTIONS.map(({ mode: m, label, icon: Icon }) => {
        const active = mode === m;
        return (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => setMode(m)}
            className={cn(
              "flex items-center justify-center gap-1 rounded-[5px] text-body-sm font-medium transition-[background-color,color,box-shadow] duration-base ease-spring focus-visible:shadow-focus focus-visible:outline-none",
              active ? "bg-control text-foreground shadow-control" : "text-foreground/75 hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5" strokeWidth={1.75} aria-hidden="true" />
            {label}
          </button>
        );
      })}
    </div>
  );
}

export function AppSidebar() {
  const { state, isMobile, setOpenMobile } = useSidebar();
  const collapsed = state === "collapsed" && !isMobile;
  const { pathname } = useLocation();

  // Close the mobile drawer after navigating.
  useEffect(() => {
    setOpenMobile(false);
  }, [pathname, setOpenMobile]);

  // Mark the current section as seen so its "New" badge expires.
  const [seen, setSeen] = useState<Set<string>>(readSeen);
  useEffect(() => {
    const match = NAV_GROUPS.flatMap((g) => g.routes).find((r) =>
      r.path === "/" ? pathname === "/" : pathname === r.path || pathname.startsWith(`${r.path}/`),
    );
    if (!match) return;
    setSeen((prev) => {
      if (prev.has(match.path)) return prev;
      const next = new Set(prev).add(match.path);
      writeSeen(next);
      return next;
    });
  }, [pathname]);

  return (
    <Sidebar collapsible="icon">
      <SidebarHeader className={cn("shrink-0 px-4 pb-2 pt-4", collapsed && "items-center px-0 pt-3")}>
        <Link
          to="/"
          // Starts with the visible lockup text so the accessible name matches what sighted
          // voice-control users read (WCAG 2.5.3).
          aria-label="HeroZero Trader Terminal, go to dashboard"
          title={collapsed ? "HeroZero" : undefined}
          className={cn(
            "flex min-w-0 items-center rounded-pill text-foreground focus-visible:shadow-focus focus-visible:outline-none",
            collapsed ? "h-9 w-9 justify-center" : "-mx-1 px-1 py-0.5",
          )}
        >
          {collapsed ? <HeroZeroMark size={24} decorative /> : <HeroZeroLogo size={30} />}
        </Link>
      </SidebarHeader>

      {/*
        SidebarContent is the only scroll container: header and footer are
        shrink-0 flex siblings, so the footer can never sit on top of the nav.
        The bottom padding lets the last item scroll clear of the fade mask,
        and the mask itself hints that more items sit below the fold.
      */}
      <SidebarContent
        className={cn(
          "min-h-0 flex-1 gap-0 overflow-y-auto overscroll-contain px-2.5 pb-5 pt-0",
          "[mask-image:linear-gradient(to_bottom,black_calc(100%-1rem),transparent)]",
          "group-data-[collapsible=icon]:overflow-y-auto",
          collapsed && "items-center px-0 [scrollbar-width:none]",
        )}
      >
        <nav aria-label="Main" className={cn("flex flex-col", collapsed && "items-center")}>
          {NAV_GROUPS.map(({ group, routes }, i) => (
            <SidebarGroup key={group} className={cn("p-0", collapsed ? "items-center" : i > 0 && "mt-2.5")}>
              {collapsed ? (
                i > 0 && <div className="my-1.5 h-[var(--hairline)] w-6 bg-separator" aria-hidden="true" />
              ) : (
                <SidebarGroupLabel>{group}</SidebarGroupLabel>
              )}
              <SidebarGroupContent>
                <SidebarMenu className={cn(collapsed ? "items-center gap-1" : "gap-px")}>
                  {routes.map((route) => (
                    <NavItem key={route.path} route={route} collapsed={collapsed} seen={seen.has(route.path)} />
                  ))}
                </SidebarMenu>
              </SidebarGroupContent>
            </SidebarGroup>
          ))}
        </nav>
      </SidebarContent>

      <SidebarFooter className={cn("hairline-t shrink-0 gap-2 px-2.5 py-2.5", collapsed && "items-center px-0")}>
        <ThemeSwitch collapsed={collapsed} />
        <PaletteToggle collapsed={collapsed} />
        {/* Keyboard hints are noise on touch screens; hide them on mobile and when collapsed. */}
        {!collapsed && !isMobile && (
          <p className="px-1 text-micro text-muted-foreground">
            Press <kbd className="rounded-chip bg-control-track px-1 font-sans">?</kbd> for shortcuts ·{" "}
            <kbd className="rounded-chip bg-control-track px-1 font-sans">{modKeyLabel("K")}</kbd> to search
          </p>
        )}
      </SidebarFooter>
    </Sidebar>
  );
}

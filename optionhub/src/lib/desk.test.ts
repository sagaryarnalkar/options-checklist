/**
 * HeroZero desk (options-checklist): the dashboard's tabs as /desk/* routes, first in the sidebar, only when
 * VITE_DASHBOARD_URL is set. lib/desk.ts and lib/routes.ts read it at import, so each case imports fresh.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

async function load(url: string | undefined) {
  vi.resetModules();
  if (url === undefined) vi.unstubAllEnvs();
  else vi.stubEnv("VITE_DASHBOARD_URL", url);
  const desk = await import("./desk");
  const routes = await import("./routes");
  return { desk, routes };
}

afterEach(() => vi.unstubAllEnvs());

describe("desk routes", () => {
  it("adds the six dashboard tabs as the first sidebar group when configured", async () => {
    const { desk, routes } = await load("https://sagy-options.duckdns.org/");
    expect(desk.DESK_URL).toBe("https://sagy-options.duckdns.org");
    const groups = routes.routesByGroup();
    expect(groups[0].group).toBe("HeroZero");
    expect(groups[0].routes.map((r) => r.navLabel)).toEqual(["Dashboard", "Positions", "By Strategy", "OI Flow", "Paper Trades", "Cash Puts"]);
    expect(groups[0].routes.every((r) => r.path.startsWith("/desk/") && r.page === "Desk")).toBe(true);
    const labels = routes.ROUTES.filter((r) => !r.hidden).map((r) => r.navLabel);
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("is absent without a dashboard URL (or with a bad one)", async () => {
    for (const v of [undefined, "", "not a url", "https://x.in/path"]) {
      const { desk, routes } = await load(v);
      expect(desk.DESK_URL).toBe("");
      expect(routes.ROUTES.some((r) => r.path.startsWith("/desk/"))).toBe(false);
      expect(routes.routesByGroup().some((g) => g.group === "HeroZero")).toBe(false);
    }
  });
});

describe("desk helpers", () => {
  it("maps paths to the dashboard's tab keys", async () => {
    const { desk } = await load("https://d.example");
    expect(desk.deskTabFromPath("/desk/oi-flow")?.id).toBe("oiflow");
    expect(desk.deskTabFromPath("/desk/cash-puts/")?.id).toBe("csp");
    expect(desk.deskTabFromPath("/option-chain")).toBeNull();
    expect(desk.DESK_TABS.map((t) => t.id)).toEqual(["dashboard", "positions", "strategy", "oiflow", "paper", "csp"]);
  });

  it("builds the embed URL with the terminal's light or dark theme", async () => {
    const { desk } = await load("https://d.example");
    const u = new URL(desk.deskFrameUrl("positions", true));
    expect(u.origin).toBe("https://d.example");
    expect(Object.fromEntries(u.searchParams)).toEqual({ embed: "1", tab: "positions", theme: "hub-dark", skin: "fintech" });
    expect(new URL(desk.deskFrameUrl("paper", false)).searchParams.get("theme")).toBe("hub-light");
  });

  it("formats rupees with Indian grouping and a real minus sign", async () => {
    const { desk } = await load("https://d.example");
    expect(desk.formatInr(652869)).toBe("₹6,52,869");
    expect(desk.formatInr(-2800056.4)).toBe("−₹28,00,056");
    expect(desk.formatInr(null)).toBe("—");
    expect(desk.formatInr(Number.NaN)).toBe("—");
  });
});

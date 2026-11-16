// @vitest-environment jsdom
/**
 * options-checklist: built for the dashboard (VITE_DASHBOARD_KITE=1), Zerodha is a built-in saved
 * broker backed by the dashboard's Kite login — listed and selectable, never written to storage.
 * The flag is read at import, so each test imports a fresh module after stubbing it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

type Mod = typeof import("@/lib/brokerConfig");
const dhan = { brokerId: "dhan", values: { clientId: "1000001", accessToken: `tok_${"x".repeat(24)}` }, addedAt: "2026-11-16T00:00:00.000Z" };

async function load(flag: string | undefined): Promise<Mod> {
  vi.resetModules();
  if (flag === undefined) vi.unstubAllEnvs();
  else vi.stubEnv("VITE_DASHBOARD_KITE", flag);
  return import("@/lib/brokerConfig");
}

function stored(): string {
  const dump = (s: Storage) => Array.from({ length: s.length }, (_, i) => `${s.key(i)}=${s.getItem(s.key(i) as string)}`);
  return [...dump(localStorage), ...dump(sessionStorage)].join("\n");
}

describe("built-in dashboard Kite broker", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });
  afterEach(() => vi.unstubAllEnvs());

  it("is listed and active with nothing saved, and nothing is written", async () => {
    const m = await load("1");
    const list = m.getSavedBrokers();
    expect(list).toHaveLength(1);
    expect(list[0]).toMatchObject({ brokerId: "zerodha", isActive: true, values: m.DASHBOARD_KITE_VALUES });
    expect(m.getActiveBroker()?.brokerId).toBe("zerodha");
    expect(stored()).not.toMatch(/dashboard-feed/);
  });

  it("stays listed beside a saved broker and can be switched to and from, never stored", async () => {
    const m = await load("1");
    await m.saveBrokerCredentials(dhan);
    expect(m.getSavedBrokers().map((b) => b.brokerId).sort()).toEqual(["dhan", "zerodha"]);
    expect(m.getActiveBroker()?.brokerId).toBe("zerodha"); // the first broker stays active
    await m.setActiveBroker("dhan");
    expect(m.getActiveBroker()?.brokerId).toBe("dhan");
    expect(m.getSavedBrokers().find((b) => b.brokerId === "zerodha")?.isActive).toBe(false);
    await m.setActiveBroker("zerodha");
    expect(m.getActiveBroker()?.brokerId).toBe("zerodha");
    expect(stored()).not.toMatch(/dashboard-feed/);
    expect(stored()).toMatch(/1000001/);
  });

  it("removing it just brings it back (built in); a saved Zerodha entry replaces it", async () => {
    const m = await load("1");
    await m.saveBrokerCredentials(dhan);
    await m.removeBrokerCredentials("zerodha");
    expect(m.getSavedBrokers().some((b) => b.brokerId === "zerodha")).toBe(true);
    await m.saveBrokerCredentials({ brokerId: "zerodha", values: { apiKey: "own", apiSecret: "s", accessToken: "t" }, addedAt: "" });
    const z = m.getSavedBrokers().filter((b) => b.brokerId === "zerodha");
    expect(z).toHaveLength(1);
    expect(z[0].values.apiKey).toBe("own");
    expect(z[0].addedAt).not.toBe("");
    expect(stored()).toMatch(/"own"/);
  });

  it("is absent from an upstream build", async () => {
    const m = await load(undefined);
    expect(m.isDashboardKiteBuild()).toBe(false);
    expect(m.getSavedBrokers()).toEqual([]);
    expect(m.getActiveBroker()).toBeNull();
  });
});

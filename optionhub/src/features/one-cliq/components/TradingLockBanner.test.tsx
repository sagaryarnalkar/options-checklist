/**
 * options-checklist: in our build a fresh install's "lock file missing" engine-fault lock can be cleared from the
 * banner (there is no Dhan risk engine it could be about); every other engine fault still needs an operator, and
 * upstream builds are unchanged.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

const fault = (reason: string) => ({ state: "locked-by-engine-fault" as const, since: null, meta: null, reason });

async function banner(dashboard: boolean) {
  vi.resetModules();
  if (dashboard) vi.stubEnv("VITE_DASHBOARD_KITE", "1");
  return (await import("./TradingLockBanner")).TradingLockBanner;
}

afterEach(() => vi.unstubAllEnvs());

describe("TradingLockBanner first-run unlock", () => {
  const props = { isLossTriggered: false, busy: false, unlockError: null, onUnlockDirect: () => {}, onRequestUnlock: async () => true };

  it("offers the unlock for a missing lock file in our build", async () => {
    const Banner = await banner(true);
    render(<Banner lock={fault("Lock file missing or unreadable.")} {...props} />);
    expect(screen.getByRole("button", { name: /Unlock \(first run: no lock file yet\)/ })).toBeInTheDocument();
  });

  it("not for any other engine fault", async () => {
    const Banner = await banner(true);
    render(<Banner lock={fault("Lock file names an unknown state.")} {...props} />);
    expect(screen.queryByRole("button", { name: /Unlock/ })).toBeNull();
  });

  it("not in an upstream build", async () => {
    const Banner = await banner(false);
    render(<Banner lock={fault("Lock file missing or unreadable.")} {...props} />);
    expect(screen.queryByRole("button", { name: /Unlock/ })).toBeNull();
  });
});

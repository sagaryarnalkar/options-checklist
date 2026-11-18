/**
 * options-checklist (#126): the dashboard frame may ask the terminal to open the Strategy Builder on a live
 * position ("⇢ Strategy Builder" on a Positions-tab group). Only from the dashboard's origin, only from the frame,
 * only that page.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

let where = "";
function Probe() {
  const loc = useLocation();
  where = `${loc.pathname}${loc.search}`;
  return null;
}

async function mount() {
  vi.resetModules();
  vi.stubEnv("VITE_DASHBOARD_URL", "https://d.example");
  const { DeskFrame } = await import("./DeskFrame");
  const { DESK_TABS } = await import("@/lib/desk");
  const view = render(
    <MemoryRouter initialEntries={["/desk/positions"]}>
      <DeskFrame tab={DESK_TABS.find((t) => t.id === "positions") ?? null} />
      <Probe />
    </MemoryRouter>,
  );
  const frame = view.container.querySelector("iframe") as HTMLIFrameElement;
  return frame;
}

function post(origin: string, source: MessageEventSource | null, data: unknown) {
  act(() => {
    window.dispatchEvent(new MessageEvent("message", { origin, source, data }));
  });
}

afterEach(() => vi.unstubAllEnvs());

describe("DeskFrame navigate messages", () => {
  it("opens the Strategy Builder on a live position when the dashboard frame asks", async () => {
    const frame = await mount();
    post("https://d.example", frame.contentWindow, { type: "hz:navigate", path: "/strategy-builder?positions=NIFTY" });
    expect(where).toBe("/strategy-builder?positions=NIFTY");
  });

  it("ignores other origins, other windows and other paths", async () => {
    const frame = await mount();
    post("https://evil.example", frame.contentWindow, { type: "hz:navigate", path: "/strategy-builder?positions=NIFTY" });
    post("https://d.example", window, { type: "hz:navigate", path: "/strategy-builder?positions=NIFTY" });
    post("https://d.example", frame.contentWindow, { type: "hz:navigate", path: "/live-trading" });
    post("https://d.example", frame.contentWindow, { type: "hz:navigate", path: "/strategy-builder?positions=NIFTY&legs=x" });
    expect(where).toBe("/desk/positions");
  });
});

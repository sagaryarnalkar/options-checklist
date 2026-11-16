// HeroZero branding (options-checklist): the lockup, cross-links switched off, the export watermark.
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { drawBrandMark, EXPORT_WATERMARK } from "@/components/strategy/exportPng";
import { HeroZeroLogo, HeroZeroMark } from "./HeroZeroLogo";
import { BrandCrossLink, ProductCrossLink } from "./ProductCrossLink";
import { OutboundLink } from "./OutboundLink";
import { SocialIcon } from "./SocialIcon";

afterEach(cleanup);

describe("HeroZeroMark", () => {
  it("is a titled image", () => {
    render(<HeroZeroMark size={20} />);
    const svg = screen.getByRole("img", { name: "HeroZero" });
    expect(svg).toHaveAttribute("width", "20");
    expect(svg.querySelectorAll("circle")).toHaveLength(2);
  });

  it("hides itself from assistive tech when decorative", () => {
    const { container } = render(<HeroZeroMark decorative />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("HeroZeroLogo", () => {
  it("renders the HEROZERO wordmark and the Trader · Terminal line", () => {
    const { container } = render(<HeroZeroLogo />);
    expect(container.textContent).toContain("HEROZERO");
    expect(screen.getByText("Trader · Terminal")).toBeInTheDocument();
  });
});

describe("OutboundLink", () => {
  it("tags the URL, opens a new tab and announces it", () => {
    render(
      <OutboundLink url="https://tradebook.mrchartist.com" campaign="journal" arrow>
        TradeBook
      </OutboundLink>,
    );
    const a = screen.getByRole("link", { name: /TradeBook \(opens in a new tab\)/ });
    expect(a).toHaveAttribute("target", "_blank");
    expect(a).toHaveAttribute("rel", "noopener noreferrer");
    expect(a.getAttribute("href")).toContain("utm_campaign=journal");
  });

  it("uses the label as the accessible name for icon-only links", () => {
    render(
      <OutboundLink url="https://x.com/Mr_Chartist" campaign="sidebar" label="Mr. Chartist on X">
        <SocialIcon id="x" />
      </OutboundLink>,
    );
    expect(screen.getByRole("link", { name: "Mr. Chartist on X (opens in a new tab)" })).toBeInTheDocument();
  });
});

describe("cross-links", () => {
  it("render nothing in HeroZero", () => {
    const { container } = render(<ProductCrossLink id="fiiDii" campaign="participants" blurb="Flows." />);
    expect(container.innerHTML).toBe("");
    const { container: c2 } = render(<BrandCrossLink name="Learn options" url="https://example.com" campaign="learn-hub" lead={null} />);
    expect(c2.innerHTML).toBe("");
  });
});

describe("SocialIcon", () => {
  it("renders every social id", () => {
    for (const id of ["x", "youtube", "telegram", "whatsapp", "instagram", "linkedin", "tradingview", "github"] as const) {
      const { container, unmount } = render(<SocialIcon id={id} />);
      expect(container.querySelector("svg")).not.toBeNull();
      unmount();
    }
  });
});

describe("PNG export watermark", () => {
  it("names HeroZero", () => {
    expect(EXPORT_WATERMARK).toMatch(/^HeroZero/);
    expect(EXPORT_WATERMARK).not.toMatch(/Chartist/);
  });

  it("draws the crosshair mark with canvas primitives", () => {
    const ctx = {
      save: vi.fn(), restore: vi.fn(), translate: vi.fn(), scale: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(),
      arc: vi.fn(), stroke: vi.fn(), fill: vi.fn(), fillStyle: "", strokeStyle: "", lineWidth: 0,
    } as unknown as CanvasRenderingContext2D;
    expect(drawBrandMark(ctx, 4, 4, 14, "black", "white", "orange")).toBe(true);
    expect(ctx.fillRect).toHaveBeenCalledWith(0, 0, 38, 38);
    expect(ctx.arc).toHaveBeenCalledTimes(2);
    expect(ctx.restore).toHaveBeenCalled();
  });
});

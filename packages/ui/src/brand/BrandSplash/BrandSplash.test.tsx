import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { BrandSplash } from "./BrandSplash";

afterEach(cleanup);

describe("BrandSplash", () => {
  it("defaults to the caption with a standalone decorative mark and no wordmark", () => {
    render(<BrandSplash />);
    expect(screen.getByRole("status")).toHaveTextContent("Starting Databench…");
    const mark = document.querySelector("[data-brand-mark]");
    expect(mark?.closest("svg")).toHaveAttribute("height", "40");
    expect(mark?.closest("svg")).toHaveAttribute("aria-hidden", "true");
    expect(document.querySelector("[data-brand-wordmark]")).toBeNull();
    // No fraction → caption only, no bar.
    expect(document.querySelector(".alk-meter")).toBeNull();
    expect(document.querySelector(".alk-splash__percent")).toBeNull();
  });

  it("renders a custom caption in the live region", () => {
    render(<BrandSplash caption="Extracting the runtime…" />);
    expect(screen.getByRole("status")).toHaveTextContent("Extracting the runtime…");
    expect(document.querySelector(".alk-meter")).toBeNull();
  });

  it("renders a determinate progressbar + percent while downloading", () => {
    render(<BrandSplash caption="Downloading the runtime…" progress={0.42} />);
    const bar = document.querySelector<HTMLElement>(".alk-meter");
    expect(bar).not.toBeNull();
    // The reused Meter exposes the standard progressbar semantics.
    expect(bar).toHaveAttribute("role", "progressbar");
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    // A static bar name — the phase caption is announced by the status region,
    // not duplicated onto the progressbar.
    expect(bar).toHaveAttribute("aria-label", "Download progress");
    expect(bar!.style.getPropertyValue("--alk-meter-v")).toBe("0.42");
    expect(document.querySelector(".alk-splash__percent")).toHaveTextContent("42%");
  });

  it("clamps an out-of-range fraction to a full/empty bar", () => {
    const { rerender } = render(<BrandSplash progress={1.8} />);
    expect(document.querySelector(".alk-meter")).toHaveAttribute("aria-valuenow", "100");
    expect(document.querySelector(".alk-splash__percent")).toHaveTextContent("100%");
    rerender(<BrandSplash progress={-1} />);
    expect(document.querySelector(".alk-meter")).toHaveAttribute("aria-valuenow", "0");
    expect(document.querySelector(".alk-splash__percent")).toHaveTextContent("0%");
  });

  it("keeps the percent out of the live region so screen readers aren't spammed per tick", () => {
    render(<BrandSplash caption="Downloading the runtime…" progress={0.5} />);
    // The status live region announces the phase; the % is aria-hidden (the bar
    // carries the numeric reading via aria-valuenow instead).
    expect(screen.getByRole("status")).toHaveTextContent("Downloading the runtime…");
    expect(screen.getByRole("status")).not.toHaveTextContent("50%");
    expect(document.querySelector(".alk-splash__percent")).toHaveAttribute("aria-hidden", "true");
  });
});

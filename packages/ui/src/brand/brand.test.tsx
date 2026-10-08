import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BrandLogoProps, WebBrand } from "./brand";

// The BRAND point is module state that freezes on first read, so every case loads a fresh copy.
async function freshBrand() {
  vi.resetModules();
  return import("./brand");
}

function TestLogo({ title, wordmark }: BrandLogoProps) {
  return <span data-testid="test-logo">{`${title ?? ""}|${wordmark ? "lockup" : "mark"}`}</span>;
}

const ACME: WebBrand = { key: "acme", productName: "Acme", attribution: "Acme", Logo: TestLogo };

beforeEach(() => vi.resetModules());
afterEach(cleanup);

describe("brand seam", () => {
  it("shows Databench when no product brand is registered", async () => {
    const { BrandLogo, currentBrand } = await freshBrand();
    expect(currentBrand().productName).toBe("Databench");
    render(<BrandLogo wordmark size={20} />);
    const logo = screen.getByRole("img", { name: "Databench" });
    expect(logo).toHaveAttribute("data-brand-logo", "databench");
    expect(logo.querySelector("[data-brand-wordmark]")).not.toBeNull();
  });

  it("renders the mark alone without the lockup letters", async () => {
    const { BrandLogo } = await freshBrand();
    render(<BrandLogo size={20} />);
    expect(screen.getByRole("img", { name: "Databench" }).querySelector("[data-brand-wordmark]")).toBeNull();
  });

  it("shows the registered product brand everywhere instead", async () => {
    const { BRAND, BrandLogo, currentBrand } = await freshBrand();
    BRAND.register(ACME);
    expect(currentBrand().productName).toBe("Acme");
    render(<BrandLogo wordmark />);
    expect(screen.getByTestId("test-logo")).toHaveTextContent("Acme|lockup");
  });

  it("refuses a second product brand", async () => {
    const { BRAND, currentBrand } = await freshBrand();
    BRAND.register(ACME);
    BRAND.register({ ...ACME, key: "other" });
    expect(() => currentBrand()).toThrow(/only one brand/);
  });

  it("refuses a brand registered after the app has read it", async () => {
    const { BRAND, currentBrand } = await freshBrand();
    currentBrand();
    expect(() => BRAND.register(ACME)).toThrow(/already read/);
  });

  it("keeps an empty title decorative", async () => {
    const { BrandLogo } = await freshBrand();
    const { container } = render(<BrandLogo title="" />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(screen.queryByRole("img")).toBeNull();
  });
});

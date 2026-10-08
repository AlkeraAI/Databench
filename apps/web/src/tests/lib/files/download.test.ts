// @vitest-environment jsdom
//
// A download saves the bytes under the name the reader saw, through the content
// route asking for an attachment, and leaves nothing behind in the document.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { contentUrl, downloadItem } from "@/lib/files/download";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("contentUrl", () => {
  it("escapes both ids into the content route and asks for the bytes", () => {
    expect(contentUrl("dr/1", "nd 2")).toBe("/api/v1/files/drives/dr%2F1/items/nd%202/content?download=1");
  });
});

describe("downloadItem", () => {
  function clicked(): Array<{ href: string; download: string; attached: boolean }> {
    const seen: Array<{ href: string; download: string; attached: boolean }> = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      seen.push({ href: this.getAttribute("href") ?? "", download: this.download, attached: this.isConnected });
    });
    return seen;
  }

  it.each([
    ["a url with a query gains the disposition as another parameter", "/c?download=1", "/c?download=1&disposition=attachment"],
    ["a bare url gains it as the query", "/c", "/c?disposition=attachment"],
  ])("%s", (_label, url, href) => {
    const seen = clicked();
    downloadItem(url, { name: "raw.csv", nameDisplay: "Q3.csv" } as Item);
    expect(seen).toEqual([{ href, download: "Q3.csv", attached: true }]);
    expect(document.querySelectorAll("a[download]")).toHaveLength(0);
  });

  it("falls back to the stored name when the display name is empty", () => {
    const seen = clicked();
    downloadItem("/c", { name: "raw.csv", nameDisplay: "" } as Item);
    expect(seen[0]?.download).toBe("raw.csv");
  });
});

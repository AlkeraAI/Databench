// @vitest-environment jsdom
//
// "Open this outside the preview" — which door a type goes through.
//
// A document that renders itself (a page, a PDF) has to open on the content
// origin under its own grant, or the browser tab would be looking at the app's
// origin with the file's markup inside it. Everything else opens on the app's
// own inline route, which is cookie-authorized and needs no grant at all. Both
// doors open with `noopener` so the new tab can never reach back.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { ContentGrant, Item, MintGrantVars } from "@/api/files";
import { openExternal } from "@/pages/workspace/files/preview/openExternal";

const PAGE_URL = "http://files.localhost:8000/c/p/bm9uY2U.Y2xhaW0.c2ln/report.html";

function item(name: string, mime: string): Item {
  return {
    id: "n1",
    driveId: "d1",
    kind: "file",
    name,
    nameDisplay: name,
    etag: "e1",
    file: { mime_type: mime, size: 10, content_hash: "sha256-test", scan_state: "clean" },
  } as Item;
}

function mintSpy() {
  const calls: MintGrantVars[] = [];
  const mint = vi.fn(async (vars: MintGrantVars): Promise<ContentGrant> => {
    calls.push(vars);
    return { url: PAGE_URL, expiresAt: "2026-09-16T00:15:00Z", kind: vars.kind, etag: "e1" };
  });
  return { mint, calls };
}

describe("openExternal", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it.each([
    ["report.html", "text/html"],
    ["q3.pdf", "application/pdf"],
    ["mixed.html", "text/html; charset=utf-8"],
  ])("opens %s on the content origin under a page grant", async (name, mime) => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const { mint, calls } = mintSpy();

    await openExternal(item(name, mime), mint);

    expect(calls).toEqual([{ driveId: "d1", itemId: "n1", kind: "page" }]);
    expect(open).toHaveBeenCalledWith(PAGE_URL, "_blank", "noopener,noreferrer");
  });

  it.each([
    ["chart.png", "image/png"],
    ["rows.csv", "text/csv"],
    ["blob.bin", "application/octet-stream"],
  ])("sends %s to the app's own inline route, minting nothing", async (name, mime) => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const { mint, calls } = mintSpy();

    await openExternal(item(name, mime), mint);

    expect(calls).toEqual([]);
    const [url, target, features] = open.mock.calls[0] as [string, string, string];
    expect(url).toBe("/api/v1/files/drives/d1/items/n1/content?disposition=inline");
    expect(url).not.toContain("/c/");
    expect(target).toBe("_blank");
    expect(features).toBe("noopener,noreferrer");
  });

  it("opens nothing when the page grant is refused", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const mint = vi.fn(async () => {
      throw new Error("refused");
    });

    await expect(openExternal(item("report.html", "text/html"), mint)).rejects.toThrow("refused");
    expect(open).not.toHaveBeenCalled();
  });

  it("opens nothing for a row that is not a file", async () => {
    const open = vi.fn();
    vi.stubGlobal("open", open);
    const { mint } = mintSpy();

    await openExternal({ id: "n1", driveId: "d1", kind: "folder", name: "reports" } as Item, mint);

    expect(open).not.toHaveBeenCalled();
  });
});

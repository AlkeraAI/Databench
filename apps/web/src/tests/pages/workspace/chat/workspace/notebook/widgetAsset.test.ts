// Widget code reaches an output frame only by content hash, from the
// notebook's own asset route; any other name is refused before a request.

import { describe, expect, it } from "vitest";

import { widgetAsset } from "@/pages/workspace/chat/workspace/notebook/NotebookTab";

const SHA = "a".repeat(64);

describe("widget assets", () => {
  it("fetches a hashed asset from the notebook's asset route", async () => {
    const urls: string[] = [];
    const fetchImpl = (async (url: string) => {
      urls.push(String(url));
      return new Response("define([], () => ({}))", { status: 200 });
    }) as unknown as typeof fetch;
    await expect(widgetAsset("d 1", "i1", `alkera-asset:sha256:${SHA}`, fetchImpl)).resolves.toBe("define([], () => ({}))");
    expect(urls).toHaveLength(1);
    expect(urls[0]).toMatch(new RegExp(`/api/v1/notebooks/d%201/i1/widget-assets/${SHA}$`));
  });

  it.each([
    ["a module by name", "bqplot"],
    ["a short hash", "alkera-asset:sha256:abc"],
    ["an upper-case hash", `alkera-asset:sha256:${"A".repeat(64)}`],
    ["a path", `alkera-asset:sha256:../${"a".repeat(61)}`],
  ])("refuses %s without asking the server", async (_name, name) => {
    let asked = false;
    const fetchImpl = (async () => {
      asked = true;
      return new Response("");
    }) as unknown as typeof fetch;
    await expect(widgetAsset("d", "i", name, fetchImpl)).rejects.toThrow();
    expect(asked).toBe(false);
  });

  it("refuses an asset the server will not serve", async () => {
    const fetchImpl = (async () => new Response("", { status: 404 })) as unknown as typeof fetch;
    await expect(widgetAsset("d", "i", `alkera-asset:sha256:${SHA}`, fetchImpl)).rejects.toThrow(/404/);
  });
});

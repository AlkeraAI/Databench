// Which artifacts the browser opens, and where it sends the reader for them.
//
// The list of renderable types is the SERVER's — the content route decides from
// what it sniffed, and a browser that guessed wider would open a tab on a
// download. So the web copy is pinned against the Python one here: whichever
// side changes, this fails until the other follows.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { Item } from "@/api/files";
import { VIEWABLE_MIME_TYPES, viewableUrl } from "@/pages/workspace/files/viewer";

// vitest runs with `apps/web` as its working directory.
const SERVER_CONTENT = resolve(
  process.cwd(),
  "../../packages/api-core/alkera_core/files/content.py",
);

/** `INLINE_MIME_TYPES` as the server spells it. */
function serverInlineTypes(): Set<string> {
  const source = readFileSync(SERVER_CONTENT, "utf-8");
  const block = /INLINE_MIME_TYPES: Final\[frozenset\[str\]\] = frozenset\(\s*\{([^}]*)\}/.exec(
    source,
  );
  if (block === null)
    throw new Error("INLINE_MIME_TYPES not found in alkera_core/files/content.py");
  return new Set([...block[1]!.matchAll(/"([^"]+)"/g)].map((match) => match[1]!));
}

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    driveId: "dr_1",
    kind: "file",
    name: "brief.pdf",
    nameDisplay: "brief.pdf",
    ...over,
  } as Item;
}

const withMime = (mime: string, over: Partial<Item> = {}): Item =>
  item({ file: { mime_type: mime }, ...over } as unknown as Partial<Item>);

describe("the renderable types", () => {
  it("are exactly the ones the content route serves inline", () => {
    expect([...VIEWABLE_MIME_TYPES].sort()).toEqual([...serverInlineTypes()].sort());
  });

  it("include text/html, which the content route serves sandboxed", () => {
    expect(VIEWABLE_MIME_TYPES.has("text/html")).toBe(true);
  });

  it.each(["image/svg+xml", "video/mp4", "video/webm", "audio/mpeg", "audio/wav"])(
    "include %s, which the server now sniffs and serves",
    (mime) => {
      expect(VIEWABLE_MIME_TYPES.has(mime)).toBe(true);
      expect(viewableUrl(withMime(mime))).not.toBeNull();
    },
  );
});

describe("the URL a double-click opens", () => {
  it("asks the content route to render rather than to save", () => {
    expect(viewableUrl(withMime("application/pdf"))).toBe(
      "/api/v1/files/drives/dr_1/items/nd_1/content?disposition=inline",
    );
    expect(viewableUrl(withMime("application/pdf"))).not.toContain("attachment");
    expect(viewableUrl(withMime("application/pdf"))).not.toContain("download=1");
  });

  it("reads the sniffed type, parameters and casing included", () => {
    expect(viewableUrl(withMime("Text/Plain; charset=utf-8"))).not.toBeNull();
  });

  it("is nothing for a type the server would send back as an attachment anyway", () => {
    expect(viewableUrl(withMime("application/zip"))).toBeNull();
    expect(viewableUrl(withMime("application/vnd.ms-excel"))).toBeNull();
  });

  it("ignores the name's extension — only what the server sniffed counts", () => {
    expect(viewableUrl(withMime("application/zip", { name: "brief.pdf" }))).toBeNull();
    expect(viewableUrl(item({ name: "brief.pdf" }))).toBeNull();
  });

  it("is nothing for a folder, whatever facet it carries", () => {
    expect(viewableUrl(withMime("application/pdf", { kind: "folder" }))).toBeNull();
  });
});

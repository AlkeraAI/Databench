// @vitest-environment jsdom
//
// What "get a link to this" means for a row, and what copying one reports.
//
// A chat is two things at once — a page and a folder of files — so a link to it
// is ambiguous unless the surface offers both. Every other row is one thing, and
// falls to the plain Files link. The registry is what keeps a new object type
// from having to edit the share dialog; the default is what keeps an object type
// nobody registered from offering nothing at all.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { enterOrg, forgetActiveOrg } from "@/api/activeOrg";
import {
  copyLinkTo,
  copyText,
  linkFor,
  linkTargetsFor,
  registerLinkTargets,
} from "@/lib/files/links";

const ORIGIN = "https://app.example.com";
const NODE = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
const SCRATCH = "9d8c7b6a-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

function item(over: Partial<Item> = {}): Item {
  return {
    id: NODE,
    driveId: "d1",
    kind: "file",
    name: "q3.csv",
    nameDisplay: "q3.csv",
    ...over,
  } as Item;
}

function chatRow(over: Record<string, unknown> = {}): Item {
  return item({
    kind: "object",
    name: "Q3 review.alkerachat",
    object: {
      type: "chat",
      id: "obj-1",
      title: "Q3 review",
      web_url: "https://app.example.com/chat/obj-1",
      ...over,
    },
  } as Partial<Item>);
}

describe("linkTargetsFor", () => {
  it("a plain file offers the one link there is", () => {
    const targets = linkTargetsFor(item(), ORIGIN);
    expect(targets).toEqual([
      { id: "files", label: "Link to file", href: `${ORIGIN}/files/${NODE}` },
    ]);
    expect(linkFor(item(), ORIGIN)).toBe(`${ORIGIN}/files/${NODE}`);
  });

  it("a folder is named as a folder", () => {
    const targets = linkTargetsFor(item({ kind: "folder", name: "reports" }), ORIGIN);
    expect(targets[0].label).toBe("Link to folder");
    expect(targets[0].href).toBe(`${ORIGIN}/files/${NODE}`);
  });

  it("a chat offers the conversation first and its files second", () => {
    // The page is what a person means by "the chat"; the files are the second
    // thing the same row is. Order is the contract — the share dialog copies the
    // first target by default.
    const targets = linkTargetsFor(
      chatRow({ metadata: { files_node_id: SCRATCH } }) as Item,
      ORIGIN,
    );
    expect(targets.map((t) => t.id)).toEqual(["page", "files"]);
    expect(targets[0]).toEqual({
      id: "page",
      label: "Link to chat",
      href: "https://app.example.com/chat/obj-1",
    });
    expect(targets[1]).toEqual({
      id: "files",
      label: "Link to files",
      href: `${ORIGIN}/files/${SCRATCH}`,
    });
    expect(linkFor(chatRow({ metadata: { files_node_id: SCRATCH } }) as Item, ORIGIN)).toBe(
      "https://app.example.com/chat/obj-1",
    );
  });

  it("a chat from before it had a working directory links to the chat folder itself", () => {
    const targets = linkTargetsFor(chatRow(), ORIGIN);
    expect(targets[1].href).toBe(`${ORIGIN}/files/${NODE}`);
  });

  it("a chat the server named no page for offers only its files", () => {
    // `web_url` is the server's, and a deployment that has not set a frontend
    // base URL sends none. A target with no href would copy an empty string.
    const targets = linkTargetsFor(chatRow({ web_url: null }) as Item, ORIGIN);
    expect(targets.map((t) => t.id)).toEqual(["files"]);
  });

  it("an object type nobody registered still gets a link", () => {
    // The registry is open: a type whose link targets are not registered
    // yet must not leave a row with an empty Share menu.
    const targets = linkTargetsFor(
      item({
        kind: "object",
        object: { type: "sculpture", id: "o1", title: "T" },
      } as Partial<Item>),
      ORIGIN,
    );
    expect(targets).toHaveLength(1);
    expect(targets[0].id).toBe("files");
  });

  it("a registered type replaces the default, and an empty answer falls back to it", () => {
    registerLinkTargets("sculpture", (row, origin) => [
      { id: "page", label: "Link to sculpture", href: `${origin}/art/${row.id}` },
    ]);
    const row = item({
      kind: "object",
      object: { type: "sculpture", id: "o1", title: "T" },
    } as Partial<Item>);
    expect(linkTargetsFor(row, ORIGIN)).toEqual([
      { id: "page", label: "Link to sculpture", href: `${ORIGIN}/art/${NODE}` },
    ]);

    registerLinkTargets("sculpture", () => []);
    expect(linkTargetsFor(row, ORIGIN)).toEqual([
      { id: "files", label: "Link to file", href: `${ORIGIN}/files/${NODE}` },
    ]);
  });
});

describe("copyText", () => {
  const clipboard = (writeText: unknown): void => {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: writeText === undefined ? undefined : { writeText },
      configurable: true,
    });
  };

  beforeEach(() => {
    clipboard(undefined);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reports copied only once the write has actually resolved", async () => {
    // A clipboard write is a permission-gated promise. Reporting "Copied" on
    // the call rather than on its resolution is how a share dialog tells
    // someone their link is on the clipboard when it is not.
    let settle: (() => void) | undefined;
    const writeText = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    clipboard(writeText);

    const pending = copyText("https://example.test/x");
    let outcome: string | undefined;
    void pending.then((value) => {
      outcome = value;
    });
    await Promise.resolve();
    expect(outcome).toBeUndefined();

    settle?.();
    expect(await pending).toBe("copied");
    expect(writeText).toHaveBeenCalledWith("https://example.test/x");
  });

  it("reports failed when the write is refused", async () => {
    clipboard(vi.fn().mockRejectedValue(new Error("denied")));
    expect(await copyText("x")).toBe("failed");
  });

  it("reports failed when there is no clipboard at all", async () => {
    clipboard(undefined);
    expect(await copyText("x")).toBe("failed");
  });
});

describe("copyLinkTo", () => {
  let written: string[];

  beforeEach(() => {
    written = [];
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: {
        writeText: (text: string) => {
          written.push(text);
          return Promise.resolve();
        },
      },
      configurable: true,
    });
    enterOrg("org_a", "Acme");
  });

  afterEach(() => {
    forgetActiveOrg();
  });

  const here = window.location.origin;

  it.each([
    ["a file row copies its Files link", () => item(), `${here}/files/${NODE}?org=org_a`],
    [
      "a chat row copies the conversation, its first target",
      () => chatRow(),
      "https://app.example.com/chat/obj-1?org=org_a",
    ],
    [
      "an href the share dialog picked is copied as given",
      () => `${ORIGIN}/files/${SCRATCH}`,
      `${ORIGIN}/files/${SCRATCH}?org=org_a`,
    ],
  ])("%s, naming the org it was copied in", async (_label, subject, expected) => {
    expect(await copyLinkTo(subject())).toBe("copied");
    expect(written).toEqual([expected]);
  });

  it("copies the bare link before the tab knows its org", async () => {
    forgetActiveOrg();
    expect(await copyLinkTo(item())).toBe("copied");
    expect(written).toEqual([`${here}/files/${NODE}`]);
  });

  it("reports failed when the browser refuses the write", async () => {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("denied")) },
      configurable: true,
    });
    expect(await copyLinkTo(item())).toBe("failed");
  });
});

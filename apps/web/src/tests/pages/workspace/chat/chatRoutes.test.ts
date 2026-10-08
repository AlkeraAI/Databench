// Every place the shared chat chrome can send a reader is routed by the shell
// that renders it.
//
// `chatRoutes()` keeps the chat's navigation targets behind the host, which only
// helps if BOTH tables stay in step with the route tables they name. So this
// reads the browser's real route table out of its source and checks every
// target the chat can produce against it: a path added to one table and
// forgotten in the other fails here. The editor's route table is held to the same
// check by the product's own tests.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import type { BlobReference } from "@alkera/chat-model";

const host = { kind: "vscode" as "vscode" | "browser" };
vi.mock("@/pages/workspace/chat/data", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/pages/workspace/chat/data")>()),
  chatHost: () => host,
}));

const { chatRoutes, performChromeAction } = await import("@/pages/workspace/chat/chatRoutes");

const REFERENCE: BlobReference = {
  handle: "blob:abc",
  name: "orders.csv",
  refType: "table",
  mime: "text/csv",
};

/** Every `path="…"` a route table declares, as matchers. */
function declaredRoutes(relative: string): RegExp[] {
  const source = readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
  const paths = [...source.matchAll(/path="([^"]+)"/gu)].map((match) => match[1]);
  return paths
    .filter((path) => path !== "*")
    .map(
      (path) =>
        new RegExp(
          `^${path
            .split("/")
            .map((segment) =>
              segment.startsWith(":") ? (segment.endsWith("?") ? "[^/]*" : "[^/]+") : segment,
            )
            .join("/")}$`,
          "u",
        ),
    );
}

/** Every target the chat chrome can navigate to, in the current shell. */
function everyTarget(): string[] {
  const routes = chatRoutes();
  return [
    routes.home,
    routes.subagent("child-1"),
    routes.compaction("c1", "part-1"),
    routes.plan("c1", "part-1"),
    routes.results("c1"),
    routes.blob("c1", REFERENCE),
  ];
}

const withoutQuery = (target: string): string => target.split("?")[0] ?? target;

describe("every chat navigation target is routed by its shell", () => {
  it.each([
    ["browser" as const, "../../../../App.tsx"],
  ])("%s", (kind, routeTable) => {
    host.kind = kind;
    const declared = declaredRoutes(routeTable);
    expect(declared.length).toBeGreaterThan(0);
    for (const target of everyTarget()) {
      const path = withoutQuery(target);
      expect(
        declared.some((route) => route.test(path)),
        `${kind} has no route for ${path}`,
      ).toBe(true);
    }
  });

  it("names the portal's own paths in a browser, never the editor's", () => {
    host.kind = "browser";
    for (const target of everyTarget()) {
      expect(target.startsWith("/editor/")).toBe(false);
      expect(target).not.toBe("/sidecar");
    }
  });

  it("names each shell's own chat home, where a deleted chat lands", () => {
    // Neither shell renders a Back key any more, but both still need the home:
    // it is where a deleted chat lands, and where the editor's New chat leads.
    host.kind = "vscode";
    expect(chatRoutes().home).toBe("/sidecar");

    host.kind = "browser";
    expect(chatRoutes().home).toBe("/chat/new");
  });

  it("opens Share in place in a tab, and offers no key at all in the editor", () => {
    // The same rule the lineage key follows: a shell with no such surface
    // renders no key at all, rather than one that lands on the catch-all. The
    // extension has no Files view, so it gets nothing.
    host.kind = "vscode";
    expect(chatRoutes().share).toBeNull();

    host.kind = "browser";
    // NOT a route: sharing a chat is the Files dialog over the chat's own node,
    // and a navigation to /files was a drive listing with no roster in it.
    expect(chatRoutes().share).toEqual({ kind: "share-dialog" });
  });

  it("reports the sharing dialog as something the router cannot perform", () => {
    host.kind = "browser";
    const share = chatRoutes().share;
    const navigate = vi.fn();
    expect(share && performChromeAction(share, navigate)).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("carries a result's descriptor so the page names itself before the first fetch", () => {
    host.kind = "browser";
    const query = new URLSearchParams(chatRoutes().blob("c1", REFERENCE).split("?")[1]);
    expect(query.get("name")).toBe("orders.csv");
    expect(query.get("type")).toBe("table");
    expect(query.get("mime")).toBe("text/csv");
  });
});

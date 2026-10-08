import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Item } from "@/api/files";
import { useCrumbTrail } from "@/pages/workspace/files/useCrumbTrail";

/** The trail is the path walked, never a history of every place visited: a
 *  child of the last crumb extends it, a folder already on it truncates back,
 *  and a sibling reached from the rail (or a search hit) starts it over. Live,
 *  Home → Teams → Shared once read `Home / Teams / Shared` for three siblings. */
function folder(id: string, parentId: string, name: string): Item {
  return { id, parentId, name, nameDisplay: name, kind: "folder" } as unknown as Item;
}

describe("useCrumbTrail", () => {
  const root = "root";
  const home = folder("home", root, "Home");
  const teams = folder("teams", root, "Teams");
  const shared = folder("shared", root, "Shared");
  const projects = folder("projects", "home", "Projects");
  const q3 = folder("q3", "projects", "Q3");

  it("does not string sibling places together", () => {
    const { result, rerender } = renderHook(({ current }) => useCrumbTrail(current), {
      initialProps: { current: home as Item | undefined },
    });
    rerender({ current: teams });
    rerender({ current: shared });
    expect(result.current.map((c) => c.name)).toEqual(["Shared"]);
  });

  it("extends the trail only when descending, and truncates back to a crumb", () => {
    const { result, rerender } = renderHook(({ current }) => useCrumbTrail(current), {
      initialProps: { current: home as Item | undefined },
    });
    rerender({ current: projects });
    rerender({ current: q3 });
    expect(result.current.map((c) => c.name)).toEqual(["Home", "Projects", "Q3"]);
    rerender({ current: home });
    expect(result.current.map((c) => c.name)).toEqual(["Home"]);
  });

  it("starts over on a node reached from somewhere else in the tree", () => {
    const { result, rerender } = renderHook(({ current }) => useCrumbTrail(current), {
      initialProps: { current: q3 as Item | undefined },
    });
    rerender({ current: teams });
    expect(result.current.map((c) => c.name)).toEqual(["Teams"]);
  });
});

/** A chat is browsed at its working directory, one node below the chat folder.
 *  The trail has to read as the conversation — its title and its glyph — while
 *  the segment still addresses the directory the listing is showing, and the
 *  chat folder itself never stands as a segment of its own. */
describe("useCrumbTrail over a chat", () => {
  const home = folder("home", "root", "Home");
  const chatNode = {
    ...folder("chat", "home", "9f2c.alkerachat"),
    object: { type: "chat", title: "Quarterly numbers" },
  } as unknown as Item;
  const working = folder("scratch", "chat", "scratch");

  it("names the working directory after the conversation, keeping the walk behind it", () => {
    const { result, rerender } = renderHook(
      ({ current, dressedAs }: { current: Item | undefined; dressedAs: Item | undefined }) =>
        useCrumbTrail(current, dressedAs),
      {
        initialProps: {
          current: home as Item | undefined,
          dressedAs: undefined as Item | undefined,
        },
      },
    );
    // The listing lands on the working directory before the read that says it
    // belongs to a chat has answered, so the first draw shows the directory.
    rerender({ current: working, dressedAs: undefined });
    expect(result.current.map((c) => c.name)).toEqual(["scratch"]);

    rerender({ current: working, dressedAs: chatNode });
    expect(result.current.map((c) => c.name)).toEqual(["Home", "Quarterly numbers"]);
    // The segment addresses the node on screen, not the chat folder around it.
    expect(result.current.map((c) => c.id)).toEqual(["home", "scratch"]);
    expect(result.current[result.current.length - 1]?.icon).toBe("chats");
  });

  it("drops the chat folder when a deep link drew it before redirecting inside", () => {
    const { result, rerender } = renderHook(
      ({ current, dressedAs }: { current: Item | undefined; dressedAs: Item | undefined }) =>
        useCrumbTrail(current, dressedAs),
      {
        initialProps: {
          current: home as Item | undefined,
          dressedAs: undefined as Item | undefined,
        },
      },
    );
    rerender({ current: chatNode, dressedAs: undefined });
    expect(result.current.map((c) => c.id)).toEqual(["home", "chat"]);

    rerender({ current: working, dressedAs: chatNode });
    expect(result.current.map((c) => c.id)).toEqual(["home", "scratch"]);
    expect(result.current.map((c) => c.name)).toEqual(["Home", "Quarterly numbers"]);
  });
});

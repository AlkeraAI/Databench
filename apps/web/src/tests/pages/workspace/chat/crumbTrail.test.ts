// The stacked-page trail is read straight off the route: every drill-in carries
// its origin (backTo included) in `?backTo=`, so one location encodes the whole
// ancestry. These tests pin the walk itself: the chain resolves root-first, the
// two implicit levels (home, the owning chat a detail page names in its path)
// are filled in, a level the module cannot name drops out without taking its
// ancestors with it, and a corrupt or hand-edited chain stays bounded.

import { describe, expect, it } from "vitest";

import { stackedTarget } from "@/pages/workspace/chat/controller"; // same-author-ok: mechanical import-path update for the controller/ split
import { crumbSteps, stepLabel, stepOf, type CrumbStep } from "@/pages/workspace/chat/crumbTrail";

describe("stepOf", () => {
  it.each([
    ["/sidecar", "home", null],
    ["/chat/a", "chat", "a"],
    ["/chat", "chat", null],
    ["/editor/chat/c1", "chat", "c1"],
    ["/editor/plan/c1/p1", "plan", "c1"],
    ["/editor/compaction/c1/p2", "compaction", "c1"],
    ["/editor/blobs/c1", "results", "c1"],
    ["/editor/blob/c1/h1", "result", "c1"],
    ["/editor/activity/c1", "activity", "c1"],
    // The query rides along untouched; the id is decoded out of the path.
    ["/editor/blobs/c1?backTo=%2Fchat%2Fa", "results", "c1"],
    ["/editor/blobs/c%201", "results", "c 1"],
  ])("names %s as a %s level", (target, kind, chatId) => {
    expect(stepOf(target)).toEqual({ target, kind, chatId });
  });

  it.each(["/", "/settings", "/editor", "/editor/graph/g1", "/lineage/urn"])(
    "cannot name %s",
    (target) => {
      expect(stepOf(target)).toBeNull();
    },
  );
});

describe("crumbSteps", () => {
  it("walks a nested backTo chain root-first", () => {
    const chatA = "/chat/a";
    const chatB = stackedTarget("/editor/chat/b", chatA);
    const results = stackedTarget("/editor/blobs/b", chatB);
    expect(crumbSteps(results)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: chatA, kind: "chat", chatId: "a" },
      { target: chatB, kind: "chat", chatId: "b" },
      { target: results, kind: "results", chatId: "b" },
    ]);
  });

  it("keeps the trail's own home level, never a second one", () => {
    const location = stackedTarget("/editor/blobs/c1", "/sidecar");
    const steps = crumbSteps(location);
    expect(steps.filter((step) => step.kind === "home")).toHaveLength(1);
    expect(steps[0]).toEqual({ target: "/sidecar", kind: "home", chatId: null });
  });

  it("inserts home and the owning chat for a deep link with no trail", () => {
    expect(crumbSteps("/editor/plan/c1/p1")).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/chat/c1", kind: "chat", chatId: "c1" },
      { target: "/editor/plan/c1/p1", kind: "plan", chatId: "c1" },
    ]);
  });

  it("re-encodes the owning chat's id in the inserted crumb target", () => {
    const steps = crumbSteps("/editor/blobs/c%201");
    expect(steps[1]).toEqual({ target: "/chat/c%201", kind: "chat", chatId: "c 1" });
  });

  it("skips the owning chat when the trail already passes through it", () => {
    const location = stackedTarget("/editor/blobs/c1", "/editor/chat/c1");
    expect(crumbSteps(location)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/editor/chat/c1", kind: "chat", chatId: "c1" },
      { target: location, kind: "results", chatId: "c1" },
    ]);
  });

  it("inserts the owning chat when the trail arrives from a different chat", () => {
    const location = stackedTarget("/editor/blob/c2/h1", "/chat/a");
    expect(crumbSteps(location)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/chat/a", kind: "chat", chatId: "a" },
      { target: "/chat/c2", kind: "chat", chatId: "c2" },
      { target: location, kind: "result", chatId: "c2" },
    ]);
  });

  it("drops a level it cannot name while keeping that level's ancestors", () => {
    const location = stackedTarget("/editor/plan/c1/p1", stackedTarget("/lineage", "/chat/a"));
    expect(crumbSteps(location)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/chat/a", kind: "chat", chatId: "a" },
      { target: "/chat/c1", kind: "chat", chatId: "c1" },
      { target: location, kind: "plan", chatId: "c1" },
    ]);
  });

  it("stops the walk at a backTo that is not an in-app path", () => {
    const location = `/editor/plan/c1/p1?backTo=${encodeURIComponent("https://evil.example")}`;
    expect(crumbSteps(location)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/chat/c1", kind: "chat", chatId: "c1" },
      { target: location, kind: "plan", chatId: "c1" },
    ]);
  });

  it("caps a hand-edited chain at the eight nearest levels", () => {
    let location = "/chat/c1";
    for (let level = 2; level <= 12; level += 1) {
      location = stackedTarget(`/editor/chat/c${level}`, location);
    }
    const steps = crumbSteps(location);
    // The four oldest ancestors fall off; the implicit home still roots the trail.
    expect(steps.map((step) => step.chatId)).toEqual([
      null, "c5", "c6", "c7", "c8", "c9", "c10", "c11", "c12",
    ]);
    expect(steps[0].kind).toBe("home");
  });

  it("terminates on a self-referential route instead of looping", () => {
    // Only reachable by hand-editing the URL. The walk must end, and today it
    // yields the level twice (once as its own origin).
    const location = "/chat/a?backTo=/chat/a";
    expect(crumbSteps(location)).toEqual<CrumbStep[]>([
      { target: "/sidecar", kind: "home", chatId: null },
      { target: "/chat/a", kind: "chat", chatId: "a" },
      { target: location, kind: "chat", chatId: "a" },
    ]);
  });
});

describe("stepLabel", () => {
  const titles = new Map([["c1", "Data audit"]]);

  it.each([
    ["home", "Chats"],
    ["plan", "Plan"],
    ["compaction", "Compaction"],
    ["results", "Results"],
    ["result", "Result"],
    ["activity", "Chat activity"],
  ] as const)("labels a %s level %s", (kind, label) => {
    expect(stepLabel({ target: "/x", kind, chatId: "c1" }, titles)).toBe(label);
  });

  // A chat's label comes from the cached list, with fallbacks for a session the
  // list does not carry (a subagent) and for the id-less new-chat route.
  it.each([
    ["c1", "Data audit"],
    ["sub", "Chat"],
    [null, "New chat"],
  ])("labels the %s chat level", (chatId, label) => {
    expect(stepLabel({ target: "/chat", kind: "chat", chatId }, titles)).toBe(label);
  });
});

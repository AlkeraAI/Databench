import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { landingState, type LandingReads, type LandingState } from "@/pages/workspace/files/landing";

// What a `/files/<id>` link lands on, decided from the two reads the page makes.
//
// The case the rule exists for is the third one: a file shared on its own, with
// the folder above it closed to the reader. Getting that wrong is not a cosmetic
// bug — the page would list a folder it was refused, or say "this isn't here"
// about a file the reader was deliberately given. So every branch is pinned,
// including the ones that must NOT become `file-solo`.

const refusal = (status: number) => new ApiError(status, { code: "files.denied", message: "no" });
const FOLDER = { kind: "folder", parentId: "nd_parent" };
const FILE = { kind: "file", parentId: "nd_parent" };

describe("landingState", () => {
  it.each<[string, LandingReads, LandingState]>([
    ["nothing read yet", { node: undefined }, "pending"],
    ["a folder", { node: FOLDER }, "folder"],
    [
      "a kind this release does not know is listed rather than previewed",
      { node: { kind: "portal", parentId: "nd_parent" } },
      "folder",
    ],
    ["a node with no kind at all", { node: { parentId: "nd_parent" } }, "folder"],
    ["a file whose folder answered", { node: FILE, parent: FOLDER }, "file-in-folder"],
    ["a file whose folder is still in flight", { node: FILE, parent: undefined }, "pending"],
    [
      "a file whose folder is not shared with the reader",
      { node: FILE, parentError: refusal(403) },
      "file-solo",
    ],
    ["a file whose folder is gone", { node: FILE, parentError: refusal(404) }, "file-solo"],
    ["a file with no folder named at all", { node: { kind: "file", parentId: null } }, "file-solo"],
    ["a file whose parent id is the empty string", { node: { kind: "file", parentId: "" } }, "file-solo"],
    [
      "a file whose folder read broke is still a file in a folder, so the listing can say so and be retried",
      { node: FILE, parentError: refusal(500) },
      "file-in-folder",
    ],
    ["an id that is gone", { node: undefined, nodeError: refusal(404) }, "not-here"],
    ["an id that is not the reader's", { node: undefined, nodeError: refusal(403) }, "not-here"],
    // The validator's answer is an answer: an id it cannot read names nothing and
    // never will, so the page says so instead of waiting for a node that is not coming.
    ["an id the drive cannot even parse", { node: undefined, nodeError: refusal(422) }, "not-here"],
    ["an id the drive rejected outright", { node: undefined, nodeError: refusal(400) }, "not-here"],
    ["a read that broke", { node: undefined, nodeError: refusal(500) }, "error"],
    [
      "a read that never reached the server",
      { node: undefined, nodeError: new Error("network down") },
      "error",
    ],
  ])("reads %s as %s", (_label, reads, expected) => {
    expect(landingState(reads)).toBe(expected);
  });

  it("tells a missing node and an unreadable one apart nowhere: both are the same state", () => {
    // The two are deliberately indistinguishable. A page that rendered them
    // differently would answer "does this id exist?" for anyone holding a guess.
    expect(landingState({ node: undefined, nodeError: refusal(404) })).toBe(
      landingState({ node: undefined, nodeError: refusal(403) }),
    );
  });

  it("prefers the node's own refusal over anything the folder said", () => {
    // Both reads failed. What the reader is told is about the thing they asked
    // for, not about the folder the page went looking for on their behalf.
    expect(
      landingState({ node: undefined, nodeError: refusal(404), parentError: refusal(500) }),
    ).toBe("not-here");
  });

  it("never reports a folder as solo, however its parent read went", () => {
    for (const parentError of [undefined, refusal(403), refusal(404), refusal(500)]) {
      expect(landingState({ node: FOLDER, parentError })).toBe("folder");
    }
  });
});

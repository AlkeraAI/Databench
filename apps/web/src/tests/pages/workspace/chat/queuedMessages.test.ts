// A chat's queued messages are written down for one person in one org.
//
// The same person can switch orgs in this browser; a message typed in one org
// must never come back as a queued message in another.

import { afterEach, describe, expect, it } from "vitest";

import { readQueued, writeQueued } from "@/pages/workspace/chat/queuedMessages";

const IN_A = { userId: "usr_dana", orgId: "org_a" };
const IN_B = { userId: "usr_dana", orgId: "org_b" };
const SAM = { userId: "usr_sam", orgId: "org_a" };

afterEach(() => {
  localStorage.clear();
});

describe("queued messages", () => {
  it("come back for the person and org that wrote them, marked restored", () => {
    writeQueued(IN_A, "c1", [{ id: "q1", text: "and then deploy it" }]);

    expect(readQueued(IN_A, "c1")).toEqual([{ id: "q1", text: "and then deploy it", restored: true }]);
  });

  it("are not read in another org, nor by another person, nor for another chat", () => {
    writeQueued(IN_A, "c1", [{ id: "q1", text: "org a only" }]);

    expect(readQueued(IN_B, "c1")).toEqual([]);
    expect(readQueued(SAM, "c1")).toEqual([]);
    expect(readQueued(IN_A, "c2")).toEqual([]);
  });

  it("emptied in one org leave the other org's queue where it was", () => {
    writeQueued(IN_A, "c1", [{ id: "q1", text: "org a" }]);
    writeQueued(IN_B, "c1", [{ id: "q2", text: "org b" }]);

    writeQueued(IN_B, "c1", []);

    expect(readQueued(IN_B, "c1")).toEqual([]);
    expect(readQueued(IN_A, "c1").map((held) => held.text)).toEqual(["org a"]);
  });

  it("are not written down at all when the shell names nobody", () => {
    writeQueued(null, "c1", [{ id: "q1", text: "hi" }]);

    expect(localStorage.length).toBe(0);
    expect(readQueued(null, "c1")).toEqual([]);
  });
});

// A link copied for sharing names the org it was copied in, and touches
// nothing else about the address.

import { describe, expect, it } from "vitest";

import { withOrg } from "@/lib/orgLink";

describe("withOrg", () => {
  it.each([
    ["a bare path", "https://app.example.com/files/nd_1", "https://app.example.com/files/nd_1?org=org_a"],
    ["a relative path", "/files/nd_1", "/files/nd_1?org=org_a"],
    [
      "an existing query, kept in order and spelling",
      "https://app.example.com/chat/c1?turn=t%202&view=a+b",
      "https://app.example.com/chat/c1?turn=t%202&view=a+b&org=org_a",
    ],
    [
      "a fragment, which stays last",
      "https://app.example.com/chat/c1?turn=t2#m-5",
      "https://app.example.com/chat/c1?turn=t2&org=org_a#m-5",
    ],
    ["a fragment with no query", "/files/nd_1#top", "/files/nd_1?org=org_a#top"],
    ["a dangling question mark", "/files/nd_1?", "/files/nd_1?org=org_a"],
    ["a question mark inside the fragment", "/files/nd_1#a?b", "/files/nd_1?org=org_a#a?b"],
  ])("appends the org to %s", (_case, url, expected) => {
    expect(withOrg(url, "org_a")).toBe(expected);
  });

  it("replaces an org the link already named, rather than naming two", () => {
    expect(withOrg("/files/nd_1?org=org_old&x=1", "org_a")).toBe("/files/nd_1?x=1&org=org_a");
    expect(withOrg("/files/nd_1?%6Frg=org_old", "org_a")).toBe("/files/nd_1?org=org_a");
  });

  it("keeps a parameter whose name only starts with org", () => {
    expect(withOrg("/files/nd_1?organic=1", "org_a")).toBe("/files/nd_1?organic=1&org=org_a");
  });

  it("encodes the org id", () => {
    expect(withOrg("/files/nd_1", "a&b=c")).toBe("/files/nd_1?org=a%26b%3Dc");
  });

  it.each([null, undefined, ""])("leaves the link as it is with no org (%s)", (org) => {
    expect(withOrg("/files/nd_1?x=1#y", org)).toBe("/files/nd_1?x=1#y");
  });
});

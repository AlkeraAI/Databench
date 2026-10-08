import { describe, expect, it } from "vitest";

import { inviteTokenFrom } from "@/api/orgs";

// What a pasted invitation reduces to on the no-organization landing.

describe("inviteTokenFrom", () => {
  it.each([
    ["a signup link", "https://app.example.com/signup?invite=abc", "abc"],
    ["a sign-in link with other params", "https://app.example.com/login?plan=team&invite=abc", "abc"],
    ["a relative link", "/signup?invite=abc", "abc"],
    ["an escaped token", "https://app.example.com/signup?invite=a%2Bb", "a+b"],
    ["a bare token with spaces around it", "  abc  ", "abc"],
  ])("reads %s", (_label, input, token) => {
    expect(inviteTokenFrom(input)).toBe(token);
  });

  it.each([
    ["nothing", ""],
    ["whitespace", "   "],
    ["a link without an invitation", "https://app.example.com/signup"],
    ["a link with an empty invitation", "https://app.example.com/signup?invite="],
  ])("finds no token in %s", (_label, input) => {
    expect(inviteTokenFrom(input)).toBeNull();
  });
});

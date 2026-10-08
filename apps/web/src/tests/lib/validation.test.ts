import { describe, expect, it } from "vitest";

import { DISPLAY_NAME_MAX, PASSWORD_MIN, isDisplayName, isPassword } from "@/lib/validation";

/**
 * The client mirror of the server policies. It never replaces the server check —
 * these tests pin that the mirror agrees with it on the cases that matter, so a
 * user is not told "fine" by the form and "no" by the API.
 */
describe("isDisplayName", () => {
  // The literal payloads from the external assessment and the emailed reports.
  it.each([
    [
      "pentest 01 — invitation phish",
      "Security Compliance Portal (Please verify your account at https://malicious.evil/login before accepting)",
    ],
    ["reported org name", "http://attacker.com/"],
    ["reported custom scheme", "strawberry://settings/team"],
    ["pentest 07 — markup", "Team <h1>HTML_tags</h1>"],
    ["www prefix", "www.evil.example"],
    ["host with path", "evil.example/login"],
    ["email address", "billing@alkera-support.example"],
  ])("refuses %s", (_label, payload) => {
    expect(isDisplayName()(payload)).not.toBeNull();
  });

  it("names the reason rather than saying 'invalid'", () => {
    expect(isDisplayName()("http://attacker.com/")).toMatch(/web address/);
    expect(isDisplayName()("Team <h1>x</h1>")).toMatch(/< or >/);
  });

  it.each([
    ["a bare dotted company name", "Acme.io"],
    ["non-ascii letters", "Müller GmbH"],
    ["an ampersand and apostrophe", "Ben & Jerry's"],
    ["parentheses", "R&D (EMEA)"],
    ["an em dash", "Data Platform — Core"],
  ])("accepts %s", (_label, name) => {
    expect(isDisplayName()(name)).toBeNull();
  });

  it("leaves emptiness to required()", () => {
    // Two validators, two jobs — otherwise an empty field reports two errors and
    // the form has to pick one.
    expect(isDisplayName()("")).toBeNull();
    expect(isDisplayName()("   ")).toBeNull();
  });

  it("refuses a name past the server's length cap", () => {
    expect(isDisplayName()("x".repeat(DISPLAY_NAME_MAX))).toBeNull();
    expect(isDisplayName()("x".repeat(DISPLAY_NAME_MAX + 1))).toMatch(/at most/);
  });
});

describe("isPassword", () => {
  it("mirrors the server's minimum length", () => {
    expect(PASSWORD_MIN).toBe(12);
    expect(isPassword()("x".repeat(PASSWORD_MIN - 1))).toMatch(/at least 12/);
    expect(isPassword()("x".repeat(PASSWORD_MIN))).toBeNull();
  });

  it("does not try to reproduce the identity checks", () => {
    // "your password is your email address" needs the account's identity and the
    // server's word list. Guessing at it here would only produce a message that
    // disagrees with the one the API returns.
    expect(isPassword()("someone@example.com!!")).toBeNull();
  });
});

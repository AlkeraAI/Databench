// The sessions list is the surface a person uses to spot a session that isn't theirs and revoke it.
// Before these helpers every browser row read "Browser session · Last used 3 minutes ago", so the
// decision was a coin flip even though the API had been returning the user agent and the coarse
// network all along. These cases pin that each row now says which device it is, where it was last
// seen, and that its Revoke control is named distinctly.

import { describe, expect, it } from "vitest";

import type { Session } from "@/api/account";
import {
  parseUserAgent,
  sessionClient,
  sessionIcon,
  sessionMeta,
  sessionRevokeLabel,
  sessionTimes,
  sessionTitle,
} from "@/pages/organization/settings/data";

const NOW = Date.parse("2026-07-01T12:00:00Z");

const session = (overrides: Partial<Session> = {}): Session =>
  ({
    jti: "jti",
    token_type: "web",
    issued_at: "2026-06-01T09:30:00Z",
    expires_at: "2026-08-01T00:00:00Z",
    last_used_at: "2026-07-01T11:00:00Z",
    label: null,
    client: null,
    current: false,
    ...overrides,
  }) as Session;

const CHROME_MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";
const SAFARI_IOS =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1";
const EDGE_WIN =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0";
const FIREFOX_LINUX = "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0";

describe("parseUserAgent", () => {
  it.each([
    // Every Chromium browser also claims "Chrome" and "Safari"; Safari claims neither. A naive
    // scan reports Edge as Chrome and Chrome as Safari, which is the whole reason for the order.
    ["chrome on macOS", CHROME_MAC, "Chrome", "macOS", false],
    ["safari on iOS", SAFARI_IOS, "Safari", "iOS", true],
    ["edge on Windows, not Chrome", EDGE_WIN, "Edge", "Windows", false],
    ["firefox on Linux", FIREFOX_LINUX, "Firefox", "Linux", false],
    [
      "opera, not Chrome",
      "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0",
      "Opera",
      "Windows",
      false,
    ],
    [
      "chrome on Android is mobile",
      "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
      "Chrome",
      "Android",
      true,
    ],
    [
      "chrome on iOS reports CriOS",
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) CriOS/126.0.0.0 Mobile/15E148 Safari/604.1",
      "Chrome",
      "iOS",
      true,
    ],
    ["curl", "curl/8.7.1", "curl", null, false],
  ])("reads %s", (_name, ua, browser, os, mobile) => {
    expect(parseUserAgent(ua)).toEqual({ browser, os, mobile });
  });

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["empty", ""],
    ["unrecognised", "SomeRobot/1.0"],
  ])("reports nothing rather than guessing for %s", (_name, ua) => {
    // A wrong device name on a security surface is worse than none: it would make the user revoke
    // the wrong session, or trust the wrong one.
    expect(parseUserAgent(ua)).toEqual({ browser: null, os: null, mobile: false });
  });
});

describe("sessionClient", () => {
  it.each([
    ["both halves", `${CHROME_MAC} · 203.0.113.0/24`, CHROME_MAC, "203.0.113.0/24"],
    ["agent only", CHROME_MAC, CHROME_MAC, null],
    ["network only", "203.0.113.0/24", null, "203.0.113.0/24"],
    ["an IPv6 prefix", "curl/8.7.1 · 2001:db8::/48", "curl/8.7.1", "2001:db8::/48"],
    ["nothing", null, null, null],
  ])("splits %s", (_name, client, userAgent, network) => {
    expect(sessionClient(session({ client }))).toEqual({ userAgent, network });
  });
});

describe("sessionTitle", () => {
  it("names the device a browser session came from", () => {
    expect(sessionTitle(session({ client: `${CHROME_MAC} · 203.0.113.0/24` }))).toBe(
      "Chrome on macOS",
    );
  });

  it("distinguishes two sessions of the same person", () => {
    const a = sessionTitle(session({ client: CHROME_MAC }));
    const b = sessionTitle(session({ client: SAFARI_IOS }));
    expect(a).not.toBe(b);
  });

  it("prefers a server label when there is one", () => {
    expect(sessionTitle(session({ label: "Work laptop", client: CHROME_MAC }))).toBe("Work laptop");
  });

  it("falls back to the generic name only when the agent says nothing", () => {
    expect(sessionTitle(session({ client: "SomeRobot/1.0" }))).toBe("Browser session");
    expect(sessionTitle(session({ client: null }))).toBe("Browser session");
  });

  it("still calls a CLI token a CLI token", () => {
    expect(sessionTitle(session({ token_type: "cli", client: CHROME_MAC }))).toBe("CLI token");
  });
});

describe("sessionMeta", () => {
  it("leads with the coarse network the API returns", () => {
    expect(sessionMeta(session({ client: `${CHROME_MAC} · 203.0.113.0/24` }), NOW)).toBe(
      "203.0.113.0/24 · Last used 1 hour ago",
    );
  });

  it("omits the network when the API has none rather than printing a gap", () => {
    expect(sessionMeta(session({ client: CHROME_MAC }), NOW)).toBe(
      "Last used 1 hour ago",
    );
  });

  it("says so when a session has never been used", () => {
    expect(sessionMeta(session({ last_used_at: null }), NOW)).toContain("Not used yet");
  });
});

describe("sessionTimes", () => {
  it("carries the moments the session began and ends", () => {
    expect(sessionTimes(session())).toBe(
      `Signed in ${new Date("2026-06-01T09:30:00Z").toLocaleString()} · Expires ${new Date("2026-08-01T00:00:00Z").toLocaleString()}`,
    );
  });
});

describe("a session row states each fact once", () => {
  // Each fact appears on one line only, so two rows with the same expiry never read it in two
  // different formats.
  it.each([
    ["last use", /last used/i],
    ["expiry", /expires/i],
    ["sign-in", /signed in/i],
  ])("names the %s on exactly one of the two lines", (_fact, pattern) => {
    const lines = [sessionMeta(session({ client: CHROME_MAC }), NOW), sessionTimes(session())];
    expect(lines.filter((line) => pattern.test(line))).toHaveLength(1);
  });
});

describe("sessionRevokeLabel", () => {
  it("gives two same-device sessions different accessible names", () => {
    const a = sessionRevokeLabel(
      session({ client: `${CHROME_MAC} · 203.0.113.0/24`, last_used_at: "2026-07-01T11:00:00Z" }),
    );
    const b = sessionRevokeLabel(
      session({ client: `${CHROME_MAC} · 198.51.100.0/24`, last_used_at: "2026-06-25T11:00:00Z" }),
    );
    expect(a).not.toBe(b);
    expect(a).toContain("Revoke Chrome on macOS");
    // The network is what tells two sessions on the SAME browser apart.
    expect(a).toContain("203.0.113.0/24");
    expect(b).toContain("198.51.100.0/24");
  });

  it("names the control by the absolute moments, not the relative ones", () => {
    // The button is where the decision is taken, and "3 minutes ago" on nine rows was the
    // ambiguity this surface exists to remove.
    const label = sessionRevokeLabel(session({ client: CHROME_MAC }));
    expect(label).toContain(new Date("2026-06-01T09:30:00Z").toLocaleString());
    expect(label).not.toMatch(/ago|expires/);
  });
});

describe("sessionIcon", () => {
  it.each([
    ["a phone", session({ client: SAFARI_IOS }), "device"],
    ["a desktop", session({ client: CHROME_MAC }), "monitor"],
    ["a CLI token", session({ token_type: "cli", client: SAFARI_IOS }), "code"],
    ["an unknown agent", session({ client: null }), "monitor"],
  ])("marks %s", (_name, s, icon) => {
    expect(sessionIcon(s)).toBe(icon);
  });
});

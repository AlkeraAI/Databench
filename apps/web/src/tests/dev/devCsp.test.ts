// @vitest-environment node
import { describe, expect, it, vi } from "vitest";

import { DEV_CONTENT_ORIGIN_FALLBACK, devServerHeaders } from "@/dev/devCsp";

import { devServerConfig } from "./devServerConfig";

// A file preview is framed from the content origin — a different host from the
// SPA, so stored bytes can never execute in the app's origin. Production says so
// in its CSP; the dev server says the same thing, or a frame source production
// blocks works on a developer's machine and fails only once deployed.

const CONTENT_ORIGIN = "http://files.localhost:8123";

function frameSrc(base: string | undefined): string {
  const value = devServerHeaders(base)["Content-Security-Policy"];
  const match = /(?:^|;)\s*frame-src\s+([^;]*)/.exec(value);
  if (!match) throw new Error(`no frame-src in ${value}`);
  return match[1].trim();
}

describe("the dev server's Content-Security-Policy", () => {
  it("frames the content origin the API mints grants against", () => {
    expect(frameSrc(`${CONTENT_ORIGIN}`)).toBe(CONTENT_ORIGIN);
  });

  it("keeps only the origin of a content base URL that carries a path", () => {
    expect(frameSrc(`${CONTENT_ORIGIN}/c/p/`)).toBe(CONTENT_ORIGIN);
  });

  it.each([
    ["unset", undefined],
    ["empty", ""],
    ["blank", "   "],
    ["unparseable", "files.localhost:8123"],
  ])("falls back to the classic dev origin rather than widening when %s", (_name, value) => {
    expect(frameSrc(value)).toBe(DEV_CONTENT_ORIGIN_FALLBACK);
  });

  it.each(["*", "'self'", "data:", "blob:", "https:"])(
    "never admits %s — a frame from anywhere else runs beside the app",
    (source) => {
      expect(frameSrc(CONTENT_ORIGIN).split(/\s+/)).not.toContain(source);
    },
  );

  it("sends frame-src and nothing else, so HMR and module loading stay unrestricted", () => {
    const value = devServerHeaders(CONTENT_ORIGIN)["Content-Security-Policy"];
    expect(value.split(";").filter((part) => part.trim()).length).toBe(1);
    expect(value).not.toContain("default-src");
    expect(Object.keys(devServerHeaders(CONTENT_ORIGIN))).toEqual(["Content-Security-Policy"]);
  });

  it("is what the dev server actually sends", () => {
    vi.stubEnv("FILES_CONTENT_BASE_URL", CONTENT_ORIGIN);
    try {
      expect(devServerConfig().headers).toEqual(devServerHeaders(CONTENT_ORIGIN));
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";

// Telemetry is something a product registers, never something the open portal starts on
// its own: the open build ships no third-party sink, and a client error reaches a sink
// only when one was registered.

beforeEach(() => {
  vi.resetModules();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } })),
  );
});

describe("the portal's telemetry sinks", () => {
  it("are none in the open composition", async () => {
    const { installExtensions } = await import("@alkera/ui/extensions");
    const { PORTAL_EXTENSIONS } = await import("@/open/portal");
    const { PORTAL_TELEMETRY } = await import("@/app/extensions/portal");
    installExtensions(PORTAL_EXTENSIONS);
    expect(PORTAL_TELEMETRY.items()).toEqual([]);
  });

  it("receive the client errors the reporter sends once registered", async () => {
    const { PORTAL_TELEMETRY } = await import("@/app/extensions/portal");
    const captured: unknown[] = [];
    PORTAL_TELEMETRY.register({
      key: "probe",
      start: async () => undefined,
      captureException: (error) => captured.push(error),
    });
    const { reportClientError } = await import("@/app/boot/reportClientError");
    const error = new Error("render crashed");
    await reportClientError(error);
    expect(captured).toEqual([error]);
  });
});

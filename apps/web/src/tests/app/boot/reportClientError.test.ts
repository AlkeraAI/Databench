import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __resetReportingState,
  dedupeSignatureCount,
  reportClientError,
  submitCrashReport,
} from "@/app/boot/reportClientError";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function fetchReturning(body: unknown, status = 200): ReturnType<typeof vi.fn> {
  return vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit): Promise<Response> =>
      jsonResponse(body, status),
  );
}

// openapi-fetch passes a Request to fetch (with a body) but a (url, init) pair
// elsewhere — handle both so the assertions don't depend on that detail.
async function callInfo(
  call: [RequestInfo | URL, RequestInit?],
): Promise<{ url: string; method?: string; body: Record<string, unknown> }> {
  const [input, init] = call;
  if (input instanceof Request) {
    const text = await input.clone().text();
    return { url: input.url, method: input.method, body: text ? JSON.parse(text) : {} };
  }
  const bodyStr = init?.body;
  return {
    url: String(input),
    method: init?.method,
    body: typeof bodyStr === "string" && bodyStr ? JSON.parse(bodyStr) : {},
  };
}

describe("reportClientError", () => {
  beforeEach(() => {
    __resetReportingState();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs client errors to the events endpoint", async () => {
    const fetchMock = fetchReturning({ received: true, trace_id: "t" });
    vi.stubGlobal("fetch", fetchMock);

    await reportClientError(new TypeError("boom"), { foo: "bar" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    expect(info.url).toContain("/api/v1/errors/events");
    expect(info.method).toBe("POST");
    expect(info.body.component).toBe("web");
    expect(info.body.message).toBe("boom");
    expect(info.body.error_type).toBe("TypeError");
  });

  it("dedupes identical errors within the window", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);

    await reportClientError(new Error("same"));
    await reportClientError(new Error("same"));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("never throws when the endpoint fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (): Promise<Response> => {
        throw new Error("network down");
      }),
    );
    await expect(reportClientError(new Error("x"))).resolves.toBeUndefined();
  });

  // `main.tsx` disarms Sentry for exactly these pages, so this endpoint is the
  // one a live credential actually reaches. Nothing that leaves may carry one.
  it.each([
    ["a reset token in the path", "https://app.example.com/reset-password/LIVE-TOKEN"],
    ["an invite in the query", "https://app.example.com/signup?invite=LIVE-TOKEN"],
    ["an OAuth response in the fragment", "https://app.example.com/#access_token=LIVE-TOKEN"],
  ])("keeps %s out of the report", async (_label, href) => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);
    const location = new URL(href);
    vi.spyOn(window, "location", "get").mockReturnValue(location as unknown as Location);

    await reportClientError(new Error("render failed"));

    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    expect(String(info.body.url)).not.toContain("LIVE-TOKEN");
    expect(String(info.body.url)).toContain("app.example.com");
  });

  it("scrubs a credential out of the message and the stack", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);
    const error = new Error(
      "the request to https://app.example.com/api/v1/auth/reset?token=LIVE-ONE failed (404)",
    );
    error.stack = "at post (https://app.example.com/app.js?oauth_ticket=LIVE-TWO:1:2)";

    await reportClientError(error);

    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    expect(String(info.body.message)).not.toContain("LIVE-ONE");
    // The route is what makes the report useful and has to survive.
    expect(String(info.body.message)).toContain("/api/v1/auth/reset");
    expect(String(info.body.stack)).not.toContain("LIVE-TWO");
    expect(String(info.body.stack)).toContain("at post");
  });

  it("dedupes failures that differ only by an id, and bounds what it remembers", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);

    for (let i = 0; i < 300; i += 1) {
      await reportClientError(
        new Error(
          `the request to /api/v1/chats/0000000a-0000-4000-8000-${String(i).padStart(12, "0")}/messages failed (404)`,
        ),
      );
    }

    // One failure, not three hundred: the id is what differs, not the bug.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(dedupeSignatureCount()).toBe(1);
  });

  it("keeps genuinely different failures apart", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);

    await reportClientError(new TypeError("cannot read 'name' of undefined"));
    await reportClientError(new TypeError("cannot read 'id' of undefined"));
    await reportClientError(new RangeError("cannot read 'name' of undefined"));

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("stops remembering signatures once the session cap is reached", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);

    for (let i = 0; i < 400; i += 1) {
      await reportClientError(
        new Error(
          `failure number ${String.fromCharCode(97 + (i % 26))}${i > 25 ? "x".repeat(Math.floor(i / 26)) : ""}`,
        ),
      );
    }

    // The cap holds, and past it nothing new is written down — a map that kept
    // growing after reporting stopped is a leak with no purpose.
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(50);
    expect(dedupeSignatureCount()).toBeLessThanOrEqual(50);
  });

  it("sweeps the href for token shapes the route list has not heard of", async () => {
    const fetchMock = fetchReturning({});
    vi.stubGlobal("fetch", fetchMock);
    const location = new URL(
      "https://app.example.com/x/eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIgNaTuRe123",
    );
    vi.spyOn(window, "location", "get").mockReturnValue(location as unknown as Location);

    await reportClientError(new Error("render failed"));

    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    // A JWT in a path segment is redacted inside a message; the href is the
    // same string and must not be the one place it survives.
    expect(String(info.body.url)).not.toContain("sIgNaTuRe123");
    expect(String(info.body.url)).toContain("app.example.com");
  });

  it("submitCrashReport returns the report id", async () => {
    vi.stubGlobal("fetch", fetchReturning({ id: "r-1" }, 201));
    const id = await submitCrashReport({ message: "boom", comment: "note" });
    expect(id).toBe("r-1");
  });

  // The button that sends this promises "no file contents or secrets", and the
  // endpoint behind it PERSISTS a row and re-sends the message to Sentry from
  // the server — where the client-side disarm that protects /errors/events has
  // no reach at all. It is the same error object the boundary already reported.
  it("scrubs a crash report the same way, and keeps the comment the reader wrote", async () => {
    const fetchMock = fetchReturning({ id: "r-2" }, 201);
    vi.stubGlobal("fetch", fetchMock);

    await submitCrashReport({
      message: "the request to https://app.example.com/api/v1/auth/reset?token=LIVE-ONE failed (404)",
      stacktrace:
        "at post (app.js:1:2)\n  Authorization: Bearer LIVE-TWO-abcdefgh\n  eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.LIVE-THREE",
      comment: "I pressed Save and it went blank",
    });

    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    expect(String(info.body.message)).not.toContain("LIVE-ONE");
    expect(String(info.body.stacktrace)).not.toContain("LIVE-TWO");
    expect(String(info.body.stacktrace)).not.toContain("LIVE-THREE");
    // What makes the report worth filing survives.
    expect(String(info.body.message)).toContain("/api/v1/auth/reset");
    expect(String(info.body.stacktrace)).toContain("at post (app.js:1:2)");
    expect(info.body.comment).toBe("I pressed Save and it went blank");
  });

  it("sends a crash report with no stack as no stack, not as an empty one", async () => {
    const fetchMock = fetchReturning({ id: "r-3" }, 201);
    vi.stubGlobal("fetch", fetchMock);

    await submitCrashReport({ message: "boom" });

    const info = await callInfo(fetchMock.mock.calls[0] as [RequestInfo | URL, RequestInit?]);
    expect(info.body.stacktrace).toBeNull();
    expect(info.body.message).toBe("boom");
  });
});

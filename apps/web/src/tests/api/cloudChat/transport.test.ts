// The one cloud-chat call whose success carries no body.
//
// `POST /chats/{id}/answer` acknowledges with 202 and nothing else — the
// resolution reaches the reader as a transcript event when the harness settles
// it. Reading that response as JSON throws, and the throw would reach the card
// as "the answer failed" while the answer was in fact on its way to the
// machine. So the call has its own path, and this pins both halves of it.

import { afterEach, describe, expect, it, vi } from "vitest";

import { answerInterrupt } from "@/api/cloudChat/transport";
import { ApiError } from "@/api/errors";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A `fetch` that records its call and answers with `response`. */
function stubFetch(response: Response) {
  const fetchMock = vi.fn(async () => response);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("answering an ask over REST", () => {
  it("posts the relay the machine validates and reads no body back", async () => {
    const fetchMock = stubFetch(new Response(null, { status: 202 }));

    await expect(
      answerInterrupt("c1", { interrupt_id: "perm-1", option_id: "allow_once", reject: false }),
    ).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(String(url)).toContain("/api/v1/chats/c1/answer");
    expect(init.method).toBe("POST");
    // The session cookie authenticates it; nothing rides in the URL.
    expect(init.credentials).toBe("include");
    expect(String(url)).not.toMatch(/token|key|secret/i);
    expect(JSON.parse(String(init.body))).toEqual({
      interrupt_id: "perm-1",
      option_id: "allow_once",
      reject: false,
    });
  });

  it("escapes a chat id into the path rather than trusting it", async () => {
    const fetchMock = stubFetch(new Response(null, { status: 202 }));
    await answerInterrupt("../objects", { interrupt_id: "p", reject: true, reason: null });
    expect(String((fetchMock.mock.calls[0] as unknown as [string])[0])).toContain(
      "/api/v1/chats/..%2Fobjects/answer",
    );
  });

  it("raises the server's refusal rather than reporting the answer as sent", async () => {
    stubFetch(
      new Response(JSON.stringify({ error: { code: "not_found" } }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(
      answerInterrupt("c1", { interrupt_id: "p", option_id: "allow_once", reject: false }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("still raises when the refusal carries no readable body", async () => {
    stubFetch(new Response(null, { status: 500 }));
    await expect(
      answerInterrupt("c1", { interrupt_id: "p", option_id: "allow_once", reject: false }),
    ).rejects.toMatchObject({ status: 500 });
  });
});

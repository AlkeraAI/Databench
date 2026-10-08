// The stance the home chip showed has to be in the bytes `POST /api/v1/chats`
// receives — not merely in what the data source hands its transport.
//
// The chip promised "Default"; the create body carried no stance; the server
// fell back to the reader's saved default, which for a reader who never saved
// one is the read-only floor; the box opened the session in read-only and the
// agent said so. The drop was between the data source and the transport: the
// source spelled the stance under a key the transport did not read, and a test
// that mocked the transport could not see it. This one drives the real
// transport and reads the request off `fetch`.

import { afterEach, describe, expect, it, vi } from "vitest";

import type { DocHandle } from "@/api/realtime/docSync";
import { CloudDataSource } from "@/pages/workspace/chat/data/CloudDataSource";

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A `fetch` that answers the two calls a create makes and records both. */
function stubApi() {
  const fetchMock = vi.fn(async (url: string | URL, init?: RequestInit) => {
    const path = new URL(String(url)).pathname;
    if (path === "/api/v1/chats" && init?.method === "POST") {
      return Response.json(
        {
          id: "chat-new",
          title: "Churn by segment",
          owner_user_id: "u-1",
          machine_id: "m-1",
          machine_status: "ready",
          created_at: "2026-09-15T00:00:00Z",
          updated_at: "2026-09-15T00:00:00Z",
          last_seq: 0,
          permission_mode: "default",
        },
        { status: 201 },
      );
    }
    if (path === "/api/v1/chats/chat-new/messages" && init?.method === "POST") {
      return Response.json(
        { id: "m1", seq: 1, kind: "user_message", text: "x", created_at: "2026-09-15T00:00:00Z" },
        { status: 201 },
      );
    }
    throw new Error(`unexpected ${init?.method ?? "GET"} ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function source() {
  return new CloudDataSource({
    openDoc: () => ({ subscribe: () => () => {}, close: () => {} }) as unknown as DocHandle<never>,
    acquire: () => () => {},
    clientId: () => "client-1",
  });
}

function createBody(fetchMock: ReturnType<typeof stubApi>): Record<string, unknown> {
  const call = fetchMock.mock.calls.find(
    ([url, init]) => new URL(String(url)).pathname === "/api/v1/chats" && init?.method === "POST",
  );
  if (!call) throw new Error("no create reached the API");
  return JSON.parse(String(call[1]?.body)) as Record<string, unknown>;
}

describe("the stance a create puts on the wire", () => {
  it.each(["default", "plan", "read_only"] as const)(
    "names %s in the body POST /api/v1/chats receives",
    async (mode) => {
      const fetchMock = stubApi();

      await source().createChat("churn by segment", { mode });

      expect(createBody(fetchMock)).toMatchObject({ permission_mode: mode });
    },
  );

  it("still carries the model and effort beside the stance", async () => {
    const fetchMock = stubApi();

    await source().createChat("churn by segment", {
      mode: "default",
      model: {
        id: "claude-haiku-4.5",
        displayName: "Claude Haiku 4.5",
        wire: "anthropic",
        efforts: ["low", "high"],
        defaultEffort: "low",
      },
      effort: "high",
    });

    expect(createBody(fetchMock)).toEqual({
      title: "Churn by segment",
      model: "claude-haiku-4.5",
      effort: "high",
      permission_mode: "default",
      claim_spare: true,
    });
  });

  it("names no stance when the composer offered none, so the server's saved default decides", async () => {
    const fetchMock = stubApi();

    await source().createChat("churn by segment");

    expect(Object.keys(createBody(fetchMock))).not.toContain("permission_mode");
  });
});

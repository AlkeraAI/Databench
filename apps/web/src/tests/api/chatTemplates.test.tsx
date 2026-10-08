// The chat-template hooks (api/chatTemplates.ts): what each one asks the server
// for, what it does with the answer, and how a refusal reaches the surface that
// asked.
//
// `fetch` is stubbed so every assertion is on the REQUEST the hook built (path,
// method, body, headers) and on what react-query then exposes — never on a mock
// echoing its own reply back. The one thing a template write must never do is
// happen twice: a save copies a chat's files, so the attempt carries an
// idempotency key and two deliberate saves carry two different ones.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import {
  useChatTemplate,
  useChatTemplates,
  useDeleteChatTemplate,
  useRenameChatTemplate,
  useSaveAsTemplate,
  useUpdateChatTemplate,
  type ChatTemplateRead,
} from "@/api/chatTemplates";
import { ApiError } from "@/api/errors";
import { createQueryClient } from "@/api/queryClient";

const TEMPLATE = "77777777-7777-7777-7777-777777777777";
const CHAT = "88888888-8888-8888-8888-888888888888";

function template(overrides: Partial<ChatTemplateRead> = {}): ChatTemplateRead {
  return {
    id: TEMPLATE,
    title: "Monthly revenue",
    version: 3,
    owner_user_id: "99999999-9999-9999-9999-999999999999",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    files_node_id: "nd_scratch",
    brief: "Pull revenue by region.",
    permission_mode: "read_only",
    source_chat_id: CHAT,
    saved_from_seq: 12,
    ...overrides,
  } as ChatTemplateRead;
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const wrapperFor = (qc: QueryClient) =>
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };

/** What the nth call actually sent. A module may hand `fetch` a `Request` or a
 *  (url, init) pair; read whichever came. */
async function sentAt(call: number): Promise<{ url: string; method: string; body: unknown }> {
  const args = fetchMock.mock.calls[call] ?? [];
  if (args[0] instanceof Request) {
    const req = args[0] as Request;
    const text = await req.clone().text();
    return { url: req.url, method: req.method, body: text === "" ? undefined : JSON.parse(text) };
  }
  const init = (args[1] ?? {}) as RequestInit;
  const raw = typeof init.body === "string" ? init.body : "";
  return {
    url: String(args[0]),
    method: init.method ?? "GET",
    body: raw === "" ? undefined : JSON.parse(raw),
  };
}

function headersOf(call: number): Record<string, string> {
  const args = fetchMock.mock.calls[call] ?? [];
  const source =
    args[0] instanceof Request ? args[0].headers : (args[1] as RequestInit | undefined)?.headers;
  const out: Record<string, string> = {};
  new Headers(source).forEach((value, key) => {
    out[key] = value;
  });
  return out;
}

const queryOf = (url: string): URLSearchParams => new URL(url).searchParams;

describe("reading templates", () => {
  it("lists a page, flattens it, and follows the cursor the server named", async () => {
    const first = template({ id: "tpl_1", title: "Newest" });
    const second = template({ id: "tpl_2", title: "Older" });
    const third = template({ id: "tpl_3", title: "Oldest" });
    fetchMock
      .mockImplementationOnce(async () =>
        jsonResponse(200, { items: [first, second], next_cursor: "cur_2" }),
      )
      .mockImplementationOnce(async () => jsonResponse(200, { items: [third], next_cursor: null }));

    const { result } = renderHook(() => useChatTemplates(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data).toHaveLength(2));
    expect(result.current.hasNextPage).toBe(true);

    await result.current.fetchNextPage();

    await waitFor(() => expect(result.current.data).toHaveLength(3));
    // The rows arrive in the server's order — newest first — across pages.
    expect(result.current.data?.map((row) => row.id)).toEqual(["tpl_1", "tpl_2", "tpl_3"]);
    // ...and the listing ends, rather than re-reading the final page for ever.
    expect(result.current.hasNextPage).toBe(false);

    const opening = await sentAt(0);
    expect(opening.method).toBe("GET");
    expect(opening.url).toContain("/api/v1/chat-templates");
    expect(queryOf(opening.url).get("cursor")).toBeNull();
    expect(queryOf(opening.url).get("limit")).toMatch(/^\d+$/);
    expect(queryOf((await sentAt(1)).url).get("cursor")).toBe("cur_2");
  });

  it("reads one template by id", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, template()));

    const { result } = renderHook(() => useChatTemplate(TEMPLATE), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data?.title).toBe("Monthly revenue"));

    const sent = await sentAt(0);
    expect(sent.method).toBe("GET");
    expect(new URL(sent.url).pathname).toBe(`/api/v1/chat-templates/${TEMPLATE}`);
  });

  it("asks for nothing until the id is known", async () => {
    const { result } = renderHook(() => useChatTemplate(undefined), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["gone", 404],
    ["not this reader's", 403],
  ])("a template that is %s settles at once instead of laddering", async (_label, status) => {
    fetchMock.mockImplementation(async () => jsonResponse(status, { code: "not_found", message: "" }));

    // The app's own retry ladder, not a test override: a read that keeps the
    // default would spend three more requests and two backoff waits before the
    // page could say the template is unavailable.
    const { result } = renderHook(() => useChatTemplate(TEMPLATE), {
      wrapper: wrapperFor(createQueryClient({ retryDelay: 0 })),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect((result.current.error as ApiError).status).toBe(status);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a 500 is a flake and is retried", async () => {
    fetchMock
      .mockImplementationOnce(async () => jsonResponse(500, { code: "internal", message: "" }))
      .mockImplementation(async () => jsonResponse(200, template()));

    const { result } = renderHook(() => useChatTemplate(TEMPLATE), {
      wrapper: wrapperFor(createQueryClient({ retryDelay: 0 })),
    });
    await waitFor(() => expect(result.current.data?.id).toBe(TEMPLATE));
  });
});

describe("saving a chat as a template", () => {
  it("posts the chat and nothing the person did not fill in", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(201, template()));

    const { result } = renderHook(() => useSaveAsTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({ source_chat_id: CHAT });

    const sent = await sentAt(0);
    expect(sent.method).toBe("POST");
    expect(new URL(sent.url).pathname).toBe("/api/v1/chat-templates");
    // A title or brief left out is "use the chat's own", which is not the same
    // fact as an empty string.
    expect(sent.body).toEqual({ source_chat_id: CHAT });
  });

  it("posts the title, the brief and the folder when the caller named them", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(201, template()));

    const { result } = renderHook(() => useSaveAsTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({
      source_chat_id: CHAT,
      title: "Monthly revenue",
      brief: "Ask for the month first.",
      destination_id: "nd_folder",
    });

    expect((await sentAt(0)).body).toEqual({
      source_chat_id: CHAT,
      title: "Monthly revenue",
      brief: "Ask for the month first.",
      destination_id: "nd_folder",
    });
  });

  it("gives each save its own idempotency key, so two saves are two templates", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(201, template()));

    const { result } = renderHook(() => useSaveAsTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({ source_chat_id: CHAT });
    await result.current.mutateAsync({ source_chat_id: CHAT });

    const first = headersOf(0)["idempotency-key"];
    const second = headersOf(1)["idempotency-key"];
    expect(first).toMatch(/\S/);
    expect(second).toMatch(/\S/);
    expect(second).not.toBe(first);
  });

  it.each([
    ["the chat has no files", 422, "chat.no_working_directory"],
    ["the copy failed after the row existed", 502, "chat_template.copy_failed"],
    ["the drive is full", 507, "files.quota_bytes"],
    ["the source may not be re-shared", 403, "files.copy_refused"],
  ])("surfaces the refusal when %s", async (_label, status, code) => {
    fetchMock.mockImplementation(async () => jsonResponse(status, { code, message: "no" }));

    const { result } = renderHook(() => useSaveAsTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await expect(result.current.mutateAsync({ source_chat_id: CHAT })).rejects.toBeInstanceOf(
      ApiError,
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(status);
    expect(result.current.error?.code).toBe(code);
  });
});

describe("editing a template", () => {
  it("sends the title, the brief and the version it read", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, template({ version: 4 })));

    const { result } = renderHook(() => useUpdateChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({
      templateId: TEMPLATE,
      title: "Quarterly revenue",
      brief: "Ask for the quarter first.",
      expectedVersion: 3,
    });

    const sent = await sentAt(0);
    expect(sent.method).toBe("PUT");
    expect(new URL(sent.url).pathname).toBe(`/api/v1/chat-templates/${TEMPLATE}`);
    expect(sent.body).toEqual({
      title: "Quarterly revenue",
      brief: "Ask for the quarter first.",
      expected_version: 3,
    });
  });

  it("an edit of the brief alone leaves the title out, so the server keeps it", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, template()));

    const { result } = renderHook(() => useUpdateChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({
      templateId: TEMPLATE,
      brief: "Ask for the quarter first.",
      expectedVersion: 3,
    });

    expect((await sentAt(0)).body).toEqual({
      brief: "Ask for the quarter first.",
      expected_version: 3,
    });
  });

  it("a rename sends the title alone, fenced on the version the row was read at", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(200, template({ title: "Quarterly revenue" })));

    const { result } = renderHook(() => useRenameChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({
      templateId: TEMPLATE,
      title: "Quarterly revenue",
      expectedVersion: 3,
    });

    const sent = await sentAt(0);
    expect(sent.method).toBe("PUT");
    expect(sent.body).toEqual({ title: "Quarterly revenue", expected_version: 3 });
  });

  it("a version conflict reaches the caller with its code", async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse(409, { code: "version_conflict", message: "read it again" }),
    );

    const { result } = renderHook(() => useUpdateChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await expect(
      result.current.mutateAsync({ templateId: TEMPLATE, title: "Nope", expectedVersion: 1 }),
    ).rejects.toBeInstanceOf(ApiError);

    await waitFor(() => expect(result.current.error?.code).toBe("version_conflict"));
    expect(result.current.error?.status).toBe(409);
  });
});

describe("deleting a template", () => {
  it("deletes by id and settles on the server's empty answer", async () => {
    fetchMock.mockImplementation(async () => jsonResponse(204, null));

    const { result } = renderHook(() => useDeleteChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({ templateId: TEMPLATE });

    const sent = await sentAt(0);
    expect(sent.method).toBe("DELETE");
    expect(new URL(sent.url).pathname).toBe(`/api/v1/chat-templates/${TEMPLATE}`);
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
  });

  it("a refused delete rejects rather than reporting the template gone", async () => {
    fetchMock.mockImplementation(async () =>
      jsonResponse(403, { code: "chat_template.delete_owner_required", message: "not yours" }),
    );

    const { result } = renderHook(() => useDeleteChatTemplate(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await expect(result.current.mutateAsync({ templateId: TEMPLATE })).rejects.toBeInstanceOf(
      ApiError,
    );

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.status).toBe(403);
  });
});

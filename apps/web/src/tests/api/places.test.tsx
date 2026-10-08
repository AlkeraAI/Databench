// The places hooks (api/files.ts): where a member's chats and chat templates
// are filed.
//
// The two readings are deliberately different requests, and the difference is
// the whole point: the bare read answers `null` for a folder nobody has needed
// yet, so drawing a page never puts a folder in somebody's drive; `ensure` is
// what a surface about to file something there sends, and it MAKES the folders
// it names. A client that derived either id from a folder NAME would break the
// moment a member renamed it, so the ids come from here.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

import { ApiError } from "@/api/errors";
import { useEnsurePlaces, usePlaces } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";

const DRIVE = "8a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d";
const HOME = "11111111-1111-1111-1111-111111111111";
const CHATS = "22222222-2222-2222-2222-222222222222";
const TEMPLATES = "33333333-3333-3333-3333-333333333333";

const ANSWER = { homeId: HOME, chatsId: CHATS, chatTemplatesId: TEMPLATES };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
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

/** The URL the nth call was made against. The generated client may hand `fetch`
 *  a `Request` or a (url, init) pair; read whichever came. */
function urlAt(call: number): URL {
  const args = fetchMock.mock.calls[call] ?? [];
  return new URL(args[0] instanceof Request ? (args[0] as Request).url : String(args[0]));
}

describe("reading the places", () => {
  it("reads the drive's places without making any", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER));

    const { result } = renderHook(() => usePlaces(DRIVE), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.data?.chatTemplatesId).toBe(TEMPLATES));

    const url = urlAt(0);
    expect(url.pathname).toBe(`/api/v1/files/drives/${DRIVE}/places`);
    // No `ensure`: drawing a page must not create a folder the member never
    // asked for.
    expect(url.searchParams.get("ensure")).toBeNull();
    expect(result.current.data?.homeId).toBe(HOME);
  });

  it("answers the absent folder as absent, not as an error", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { homeId: HOME, chatsId: CHATS, chatTemplatesId: null }),
    );

    const { result } = renderHook(() => usePlaces(DRIVE), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.chatTemplatesId).toBeNull();
  });

  it("asks for nothing until the drive is known", async () => {
    const { result } = renderHook(() => usePlaces(undefined), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await waitFor(() => expect(result.current.fetchStatus).toBe("idle"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("making the places", () => {
  it("names the places it wants made and answers their ids", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER));

    const { result } = renderHook(() => useEnsurePlaces(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    const made = await result.current.mutateAsync({
      driveId: DRIVE,
      places: ["chats", "chatTemplates"],
    });

    expect(urlAt(0).searchParams.get("ensure")).toBe("chats,chatTemplates");
    expect(made.chatTemplatesId).toBe(TEMPLATES);
  });

  it("ensures one place alone when that is all the caller needs", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER));

    const { result } = renderHook(() => useEnsurePlaces(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await result.current.mutateAsync({ driveId: DRIVE, places: ["chatTemplates"] });

    expect(urlAt(0).searchParams.get("ensure")).toBe("chatTemplates");
  });

  it("leaves the answer in the cache, so the surface that navigates does not re-read", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER));
    const client = createQueryClient({ retry: false });

    const { result } = renderHook(() => useEnsurePlaces(), { wrapper: wrapperFor(client) });
    await result.current.mutateAsync({ driveId: DRIVE, places: ["chatTemplates"] });

    await waitFor(() =>
      expect(client.getQueryData(keys.files.places(DRIVE))).toEqual(ANSWER),
    );
    // ...under THIS drive's entry, not some other drive's.
    expect(client.getQueryData(keys.files.places("other"))).toBeUndefined();
  });

  it("surfaces a refused place rather than reporting it absent", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(400, { code: "files.unknown_place", message: "There is no such place: inbox." }),
    );

    const { result } = renderHook(() => useEnsurePlaces(), {
      wrapper: wrapperFor(createQueryClient({ retry: false })),
    });
    await expect(
      result.current.mutateAsync({ driveId: DRIVE, places: ["chatTemplates"] }),
    ).rejects.toBeInstanceOf(ApiError);

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error?.code).toBe("files.unknown_place");
    expect(result.current.error?.status).toBe(400);
  });
});

// A node that is gone is an ANSWER, not a flake.
//
// `useItem` is what a reopened chat tab, a shared link and the preview modal all
// read to find out whether the file they remember still exists. Under the client's
// default retry ladder a 404 costs three requests and two backoff waits before the
// caller learns anything — so a workspace restoring eight closed tabs onto deleted
// files spends twenty-four requests discovering it. A 404 on a Files read settles
// after one; a 500 is still a flake and still retries.

import { createElement, type ReactNode } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useItem } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";

const DRIVE = "8a1b2c3d-4e5f-4a6b-8c9d-0e1f2a3b4c5d";
const NODE = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";

const problem = (status: number, code: string): Response =>
  new Response(JSON.stringify({ error: { code, message: "no" } }), {
    status,
    headers: { "content-type": "application/json" },
  });

/** The app's real client: the retry ladder under test is the one it installs, so
 *  a bare QueryClient would prove nothing. Retries are left at their defaults
 *  and the backoff is collapsed so a failing read settles inside the test. */
function harness() {
  const queryClient = createQueryClient({ retryDelay: 0 });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return { queryClient, wrapper };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useItem settles a 404 after one request", () => {
  it("asks once for a node that is gone", async () => {
    const fetchMock = vi.fn().mockResolvedValue(problem(404, "files.not_found"));
    vi.stubGlobal("fetch", fetchMock);
    const { wrapper } = harness();

    const { result } = renderHook(() => useItem(DRIVE, NODE), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a caller that opts out of the settled reading keeps the retry ladder", async () => {
    // The flag is a per-read decision, not a client-wide one: a surface that
    // would rather wait out a transient 404 can still say so.
    const fetchMock = vi.fn().mockResolvedValue(problem(404, "files.not_found"));
    vi.stubGlobal("fetch", fetchMock);
    const { wrapper } = harness();

    const { result } = renderHook(() => useItem(DRIVE, NODE, { settled404: false }), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5_000 });

    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("a server error is still a flake and is still retried", async () => {
    // The whole point of settling a 404 is that it is a decision the server has
    // made. A 500 is not; collapsing it too would turn one bad second into a
    // permanently empty pane.
    const fetchMock = vi.fn().mockResolvedValue(problem(500, "internal_error"));
    vi.stubGlobal("fetch", fetchMock);
    const { wrapper } = harness();

    const { result } = renderHook(() => useItem(DRIVE, NODE), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true), { timeout: 5_000 });

    expect(fetchMock.mock.calls.length).toBeGreaterThan(1);
  });

  it("a node that is there is still read", async () => {
    const item = { id: NODE, driveId: DRIVE, kind: "file", name: "q3.csv" };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(item), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { wrapper } = harness();

    const { result } = renderHook(() => useItem(DRIVE, NODE), { wrapper });
    await waitFor(() => expect(result.current.data?.id).toBe(NODE));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

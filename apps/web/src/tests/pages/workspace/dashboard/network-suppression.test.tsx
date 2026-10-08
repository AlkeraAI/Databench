import { useState } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { DashboardDataProvider, LiveDashboardProvider, resetDashboardStore, useDashboardData } from "@/pages/workspace/dashboard/provider";
import type { DashboardState } from "@/pages/workspace/dashboard/model";

// Proven against real behavior (NOT a mock of the hooks): the four live React Query hooks live ONLY
// in the live writer, so the injected/preview path (DashboardDataProvider seeds the store) mounts no
// queries and issues zero network requests, while the live path (LiveDashboardProvider) mounts four
// enabled queries that each fetch. We assert the observable contract — `fetch` called or not — with
// the REAL hooks and a REAL QueryClient, stubbing only fetch (the boundary).

let fetchSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  resetDashboardStore();
  fetchSpy = vi.fn(() =>
    Promise.resolve(new Response(JSON.stringify({}), { status: 200, headers: { "content-type": "application/json" } })),
  );
  vi.stubGlobal("fetch", fetchSpy);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// Reads the seam so useDashboardData() actually subscribes to the live hooks (which is where the
// enabled flag is threaded). Renders nothing visible — the assertion is on fetch.
function Consumer() {
  const { status } = useDashboardData();
  return <span data-testid="status">{status}</span>;
}

function withClient(node: React.ReactNode) {
  const Wrapper = () => {
    const [qc] = useState(createQueryClient);
    return <QueryClientProvider client={qc}>{node}</QueryClientProvider>;
  };
  return <Wrapper />;
}

describe("seam network suppression", () => {
  it("issues zero fetches when a fixture is seeded through the seam (no live writer mounted)", async () => {
    const injected: DashboardState = { status: "ready", data: null, errorMessage: null, retry: () => {} };
    await act(async () => {
      render(
        withClient(
          <DashboardDataProvider value={injected}>
            <Consumer />
          </DashboardDataProvider>,
        ),
      );
    });
    // Let any (incorrectly) mounted query microtask settle before asserting.
    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("DOES fetch on the live path (LiveDashboardProvider mounted) — proving the suppression is conditional", async () => {
    render(
      withClient(
        <LiveDashboardProvider>
          <Consumer />
        </LiveDashboardProvider>,
      ),
    );
    // The live writer mounts four enabled queries; each one fetches. Wait for the dispatch rather than
    // a single microtask — React Query schedules the fetch across ticks, so a fixed wait flakes. (If
    // this never fires, the suppression test above proves nothing — it would pass for the wrong reason.)
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
  });
});

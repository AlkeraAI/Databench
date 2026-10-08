// @vitest-environment jsdom
//
// The tab's title is written by one component that READS the query cache, and
// the cache emits from inside other components' render phase — react-query
// builds a query entry while its caller is still on the render stack. Taken
// synchronously, that notification schedules an update on the title writer from
// inside somebody else's render: "Cannot update a component (`TitleWriter`)
// while rendering a different component (`ChatPresence`)", which React logged on
// every chat created.

import { QueryClientProvider, type QueryClient } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { DocumentTitle } from "@/app/documentTitle";

const CHAT = "c1";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** A page that fills the query cache DURING ITS RENDER, the way every `useQuery`
 *  under it does: react-query builds the query entry, and the cache notifies its
 *  subscribers, while the component that mounted the query is still rendering.
 *  The write is on a LATER render, because the writer only holds a subscription
 *  once its own effects have run. */
function CacheFillingPage({
  client,
  bump,
}: {
  client: QueryClient;
  bump: (fn: () => void) => void;
}) {
  const [rendered, setRendered] = useState(0);
  bump(() => setRendered((n) => n + 1));
  if (rendered > 0) {
    client.setQueryData(keys.chats.one(CHAT), {
      id: CHAT,
      title: "Quarterly plan",
      owner_user_id: "u_bo",
    });
  }
  return null;
}

describe("the tab title and a page that fills the cache under it", () => {
  it("takes a cache write made inside another component's render without updating mid-render", async () => {
    const errors: string[] = [];
    vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      errors.push(args.map((one) => String(one)).join(" "));
    });
    const client = createQueryClient({ retry: false });
    let rerender: (() => void) | null = null;

    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/chat/${CHAT}`]}>
          <DocumentTitle>
            <CacheFillingPage
              client={client}
              bump={(fn) => {
                rerender = fn;
              }}
            />
          </DocumentTitle>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    act(() => rerender?.());

    // The title still lands — this must not pass by the writer simply never
    // reading the cache again.
    await waitFor(() => expect(document.title).toContain("Quarterly plan"));
    expect(errors.filter((line) => line.includes("Cannot update a component"))).toEqual([]);
  });
});

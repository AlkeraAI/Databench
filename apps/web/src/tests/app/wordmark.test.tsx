import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { AppLayout } from "@/app/AppLayout";

// The wordmark top-left is the doorway home: a link to the overview that a
// person can click from any page, never a piece of text they could select.

vi.mock("@/api/auth", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/auth")>();
  return {
    ...actual,
    useCurrentUser: () => ({
      data: { id: "u1", email: "a@b.c", first_name: "A", last_name: "B", org_team_id: "t1", platform_role: null },
      isLoading: false,
    }),
  };
});

afterEach(() => vi.restoreAllMocks());

describe("the sidebar wordmark", () => {
  it("is a link to the overview", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200, headers: { "content-type": "application/json" } })));
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter initialEntries={["/chat"]}>
          <Routes>
            <Route element={<AppLayout />}>
              <Route path="/chat" element={<div>chat</div>} />
              <Route path="/" element={<div>overview</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The nav also carries an Overview leaf; the wordmark is the link that wears the mark.
    const home = document.querySelector("a.alk-wordmark");
    expect(home).not.toBeNull();
    expect(home).toHaveAttribute("href", "/");
    expect(home).toHaveAttribute("aria-label", "Overview");
    expect(screen.getAllByRole("link", { name: "Overview" }).length).toBeGreaterThanOrEqual(2);
  });
});

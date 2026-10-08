import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "@alkera/sdk";

import { createQueryClient } from "@/api/queryClient";
import { AdminOrgsPage } from "@/pages/platform/admin/orgs/AdminOrgsPage";
import { RAW_FAILURES, expectNoRawFailureText } from "@/tests/fixtures/rawFailures";
import { TopbarSlotsContext } from "@/app/Topbar";

// The orgs register, driven through its REAL hook with the org list seeded into the query cache.
// These pin what a user reaches: the search reconciles by name OR id (the case-insensitive substring
// match), a no-match query states it, and an empty register states the next move. Behaviour, never
// class names.
//
// The search input is a controlled `<input type="search">`; jsdom + userEvent.type drops keystrokes
// on a controlled search box, so the value is set with fireEvent.change (the input a user would type).

type Org = components["schemas"]["OrgRead"];
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const org = (name: string, id: string, member_count: number): Org => ({
  name,
  id,
  parent_team_id: null,
  is_root: true,
  created_at: "2026-01-01T00:00:00Z",
  member_count,
});

const ORGS: Org[] = [
  org("Tideline Analytics", "a1b2c3d4-aaaa", 42),
  org("Meridian Labs", "b2c3d4e5-bbbb", 18),
];

function renderOrgs(orgs: Org[]) {
  // staleTime Infinity makes the seeded cache authoritative: no background refetch (which would
  // "fetch failed" in jsdom and flip the query to error) races the assertions.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  qc.setQueryData(["admin", "orgs"], orgs);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return render(<AdminOrgsPage />, { wrapper });
}

const search = (value: string) => fireEvent.change(screen.getByRole("searchbox", { name: /search organizations/i }), { target: { value } });

describe("AdminOrgsPage", () => {
  it("subtitles the register with what it lists", async () => {
    // It read "Houses on the platform" — a metaphor that names neither the rows
    // nor what an operator does with them. The subtitle rides a topbar portal, so
    // the test supplies the slot the shell would.
    const slot = document.createElement("div");
    document.body.append(slot);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    qc.setQueryData(["admin", "orgs"], ORGS);
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <TopbarSlotsContext.Provider
            value={{ subtitle: slot, actions: null, framed: true, setTitleHidden: () => {}, setTopbarHidden: () => {} }}
          >
            <AdminOrgsPage />
          </TopbarSlotsContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(await within(slot).findByText("Organizations on the platform")).toBeInTheDocument();
    expect(within(slot).queryByText(/Houses/)).not.toBeInTheDocument();
    slot.remove();
  });


  it("lists every org with its member count and a link into its detail", async () => {
    renderOrgs(ORGS);
    const tideline = await screen.findByRole("link", { name: "Tideline Analytics" });
    expect(tideline).toHaveAttribute("href", "/admin/orgs/a1b2c3d4-aaaa");
    expect(screen.getByRole("link", { name: "Meridian Labs" })).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
  });

  it("filters by name, case-insensitively, hiding the non-matches", async () => {
    renderOrgs(ORGS);
    await screen.findByRole("link", { name: "Tideline Analytics" });
    search("MERIDIAN");
    await waitFor(() => expect(screen.queryByRole("link", { name: "Tideline Analytics" })).not.toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Meridian Labs" })).toBeInTheDocument();
  });

  it("filters by id substring, not only name", async () => {
    renderOrgs(ORGS);
    await screen.findByRole("link", { name: "Tideline Analytics" });
    search("b2c3d4e5");
    await waitFor(() => expect(screen.queryByRole("link", { name: "Tideline Analytics" })).not.toBeInTheDocument());
    expect(screen.getByRole("link", { name: "Meridian Labs" })).toBeInTheDocument();
  });

  it("states the empty result when nothing matches", async () => {
    renderOrgs(ORGS);
    await screen.findByRole("link", { name: "Tideline Analytics" });
    search("zzz-no-such-org");
    expect(await screen.findByText(/no matches/i)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Meridian Labs" })).not.toBeInTheDocument();
  });

  it("states the one next move when the register is empty", async () => {
    renderOrgs([]);
    expect(await screen.findByText(/no organizations yet/i)).toBeInTheDocument();
    // The empty state offers the create action, the same one the masthead carries.
    expect(screen.getAllByRole("button", { name: /new org/i }).length).toBeGreaterThan(0);
  });

  it("opens the create-org modal from the empty state and keeps Create disabled until the form is complete", async () => {
    renderOrgs([]);
    await screen.findByText(/no organizations yet/i);
    fireEvent.click(screen.getAllByRole("button", { name: /new org/i })[0]);
    const dialog = await screen.findByRole("dialog");
    const nameField = within(dialog).getByLabelText(/org name/i);
    fireEvent.change(nameField, { target: { value: "Acme Survey" } });
    expect(nameField).toHaveValue("Acme Survey");
    // The primary stays disabled until every required field is filled (name alone isn't enough).
    expect(within(dialog).getByRole("button", { name: "Create" })).toBeDisabled();
  });

  it("shows the error register with a working retry when the orgs load fails", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        Promise.resolve(
          new Response(JSON.stringify({ error: { message: "boom" } }), {
            status: 500,
            headers: { "content-type": "application/json" },
          }),
        ),
      ),
    );
    render(<AdminOrgsPage />, {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={qc}>
          <MemoryRouter>{children}</MemoryRouter>
        </QueryClientProvider>
      ),
    });
    expect(await screen.findByText(/we couldn.t load organizations/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("AdminOrgsPage when the register cannot be read", () => {
  function renderFailing(answer: () => Response) {
    vi.stubGlobal("fetch", vi.fn(async () => answer()));
    render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <AdminOrgsPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it.each(RAW_FAILURES)("says %s as the headline alone, never in its own words", async (_label, answer) => {
    renderFailing(answer);
    expect(await screen.findByText("We couldn’t load organizations")).toBeInTheDocument();
    expect(screen.getByText("Try again.")).toBeInTheDocument();
    expectNoRawFailureText();
  });

  it("keeps a refusal the server explained, under the headline", async () => {
    const reason = "Only platform staff can list organizations.";
    renderFailing(
      () =>
        new Response(JSON.stringify({ error: { code: "forbidden", message: reason, trace_id: "t" } }), {
          status: 403,
          headers: { "content-type": "application/json" },
        }),
    );
    expect(await screen.findByText("We couldn’t load organizations")).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });
});

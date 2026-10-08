import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OAuthOptions } from "@/pages/auth/ui/OAuthOptions";

// The federated sign-in options, driven through the REAL useOAuthProviders query against a
// REAL QueryClient — only `fetch` and `window.location` are stubbed. The buttons were dead
// before (no handler); the contract this pins is that they (a) render one per CONFIGURED
// provider and nothing when none are configured, and (b) start a full-page OAuth redirect to
// the backend with the right intent + provider in the URL.

let fetchSpy: ReturnType<typeof vi.fn>;
const assignedHref: { value: string } = { value: "" };

beforeEach(() => {
  fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
  assignedHref.value = "";
  // window.location.href is read+written by the redirect; capture the assignment.
  Object.defineProperty(window, "location", {
    configurable: true,
    value: {
      get href() {
        return assignedHref.value;
      },
      set href(v: string) {
        assignedHref.value = v;
      },
      origin: "https://app.example.com",
    },
  });
});
afterEach(() => vi.unstubAllGlobals());

function providersResponse(providers: string[]): Response {
  return new Response(JSON.stringify({ providers }), { status: 200, headers: { "content-type": "application/json" } });
}

function renderOptions(intent: "in" | "up" = "in") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <OAuthOptions intent={intent} />
    </QueryClientProvider>,
  );
}

describe("OAuthOptions", () => {
  it("renders a button per configured provider", async () => {
    fetchSpy.mockResolvedValue(providersResponse(["google", "github"]));
    renderOptions("in");
    expect(await screen.findByRole("button", { name: /sign in with google/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in with github/i })).toBeInTheDocument();
  });

  it("renders nothing when no providers are configured (no empty divider)", async () => {
    fetchSpy.mockResolvedValue(providersResponse([]));
    const { container } = renderOptions("in");
    // Give the query a tick to settle, then assert the component contributed no DOM.
    await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
    expect(container.firstChild).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("starts the backend OAuth handshake with the provider + login intent on click", async () => {
    const user = userEvent.setup();
    fetchSpy.mockResolvedValue(providersResponse(["google"]));
    renderOptions("in");

    await user.click(await screen.findByRole("button", { name: /sign in with google/i }));
    expect(assignedHref.value).toContain("/api/v1/auth/oauth/google/start");
    expect(assignedHref.value).toContain("intent=login");
  });

  it("uses the signup intent when the page is signup", async () => {
    const user = userEvent.setup();
    fetchSpy.mockResolvedValue(providersResponse(["github"]));
    renderOptions("up");

    await user.click(await screen.findByRole("button", { name: /sign up with github/i }));
    expect(assignedHref.value).toContain("/api/v1/auth/oauth/github/start");
    expect(assignedHref.value).toContain("intent=signup");
  });

  it("renders an unknown provider (e.g. the dev mock) with its name rather than dropping it", async () => {
    fetchSpy.mockResolvedValue(providersResponse(["mock"]));
    renderOptions("in");
    expect(await screen.findByRole("button", { name: /sign in with mock/i })).toBeInTheDocument();
  });
});

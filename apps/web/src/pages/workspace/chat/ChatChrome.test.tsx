// The chat header's wiring to the host. The chrome itself is presentation; what
// this file pins is where each of its controls actually goes -- a settings row
// that ran the wrong editor command, or a project link that reported the wrong
// surface, would look correct and land the reader somewhere else.
//
// Rows are addressed by the package's action id rather than their wording: the
// wording belongs to the chrome, and rewording a row is not a routing change.

vi.mock("./data", () => ({ chatHost: () => (hostMock) }));
import { cleanup, render, screen, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChromeLink } from "@alkera/ui";

import { createQueryClient } from "@/api/queryClient";

const { hostMock, cached } = vi.hoisted(() => {
  // What the shell currently says about the signed-in account. A test sets it
  // before rendering; the chrome reads it through the host port.
  const cached = { account: { email: null as string | null, webAppUrl: null as string | null } };
  const hostMock = {
    kind: "vscode",
    runCommand: vi.fn(async () => {}),
    openFile: vi.fn(async () => {}),
    workspacePath: () => null,
    account: () => cached.account,
    onAccountChange: () => () => {},
    subscribe: () => () => {},
    auth: {
      openBrowser: vi.fn(async () => {}),
    },
  };
  return { hostMock, cached };
});


import { ChatChrome } from "./ChatChrome";

const SIGNED_IN_EMAIL = "dana@example.com";
const WEB_APP_URL = "https://app.example.test";

/** The account the shell reports, the one the chrome paints from. */
function signedIn(over: { email?: string | null; frontendUrl?: string | null } = {}): void {
  cached.account = {
    email: over.email === undefined ? SIGNED_IN_EMAIL : over.email,
    webAppUrl: over.frontendUrl === undefined ? WEB_APP_URL : over.frontendUrl,
  };
}

const LINKS: ChromeLink[] = [
  { id: "knowledge", label: "Knowledge", count: 3 },
  { id: "lineage", label: "Lineage", count: 12 },
  { id: "artifacts", label: "Artifacts" },
];

/** The chrome as a shell mounts it: inside the app's router and query client.
 *  Its settings menu now performs the shell's own actions — an editor command
 *  here, the portal's logout in a browser tab — so both providers are part of
 *  the surface rather than scaffolding. */
function renderChrome(onOpenLink = vi.fn()): { onOpenLink: ReturnType<typeof vi.fn> } {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/chat/c1"]}>
        <ChatChrome title="Data audit" links={LINKS} onOpenLink={onOpenLink} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { onOpenLink };
}

/** The settings menu, open, with its rows keyed by the action they perform. */
async function openSettings(): Promise<Record<string, HTMLElement>> {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /account and settings/i }));
  const rows: Record<string, HTMLElement> = {};
  for (const row of screen.getAllByRole("menuitem")) {
    rows[row.getAttribute("data-menu-id") ?? ""] = row;
  }
  return rows;
}

afterEach(() => {
  cleanup();
  cached.account = { email: null, webAppUrl: null };
  vi.clearAllMocks();
});

describe("the chrome's settings menu", () => {
  it("offers every action the extension knows how to run", async () => {
    signedIn();
    renderChrome();
    expect(Object.keys(await openSettings()).sort()).toEqual(["jobs", "logout", "plugins", "preferences"]);
  });

  it.each([
    ["plugins", "alkera.openPlugins"],
    ["jobs", "alkera.openScheduler"],
    ["preferences", "alkera.openPreferences"],
    ["logout", "alkera.logout"],
  ])("runs %s through the editor command %s", async (action, command) => {
    signedIn();
    renderChrome();
    const rows = await openSettings();
    await userEvent.setup().click(rows[action]);

    expect(hostMock.runCommand).toHaveBeenCalledTimes(1);
    expect(hostMock.runCommand).toHaveBeenCalledWith({ command });
  });

  it("dismisses itself as it acts, so the chat is readable again", async () => {
    signedIn();
    renderChrome();
    const rows = await openSettings();
    await userEvent.setup().click(rows.preferences);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("names the signed-in account above the actions", async () => {
    signedIn();
    renderChrome();
    await openSettings();
    expect(within(screen.getByRole("menu")).getByText(SIGNED_IN_EMAIL)).toBeInTheDocument();
  });

  it("opens straight onto the actions while the host reports no account", async () => {
    signedIn({ email: null });
    renderChrome();
    const rows = await openSettings();
    expect(Object.keys(rows)).toHaveLength(4);
    expect(within(screen.getByRole("menu")).queryByText(SIGNED_IN_EMAIL)).toBeNull();
  });
});

describe("the chrome's door to the web app", () => {
  // pins-source: the label is the reader's only handle on this control, so the
  // query is the assertion -- a shared constant would test nothing.
  const webAppKey = () => screen.queryByRole("button", { name: /open web app/i });

  it("opens the address the host gave for the web app", async () => {
    signedIn();
    renderChrome();
    const key = webAppKey();
    expect(key).not.toBeNull();
    await userEvent.setup().click(key as HTMLElement);
    expect(hostMock.auth.openBrowser).toHaveBeenCalledWith(WEB_APP_URL);
  });

  it("shows no door while the host has not said where the web app is", () => {
    signedIn({ frontendUrl: null });
    renderChrome();
    expect(webAppKey()).toBeNull();
  });
});

describe("the chrome's project links", () => {
  it.each([["Knowledge", "knowledge"], ["Lineage", "lineage"], ["Artifacts", "artifacts"]])(
    "reports %s as the %s surface to open",
    async (label, id) => {
      signedIn();
      const { onOpenLink } = renderChrome();
      await userEvent.setup().click(screen.getByRole("button", { name: label }));
      expect(onOpenLink).toHaveBeenCalledTimes(1);
      expect(onOpenLink).toHaveBeenCalledWith(id);
    },
  );

  it("reports the same surface from the folded menu as from the rail", async () => {
    signedIn();
    const { onOpenLink } = renderChrome();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /^more$/i }));
    // The folded row appends its count to the label, so match on the label it leads with.
    await user.click(screen.getByRole("menuitem", { name: /^Lineage/ }));
    expect(onOpenLink).toHaveBeenCalledWith("lineage");
  });
});

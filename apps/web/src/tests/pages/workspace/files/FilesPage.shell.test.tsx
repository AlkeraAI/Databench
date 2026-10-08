import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";

import { createQueryClient } from "@/api/queryClient";
import { FILES_NARROW_QUERY, FilesHomeProvider, FilesPage } from "@/pages/workspace/files/FilesPage";

// The shell, driven through the real routes. The home redirect is the page's one navigation
// decision, so it is asserted by where the router ends up -- not by a spy on navigate.

/** Pin `matchMedia` to a width: `narrow` decides whether the details pane is a column or a sheet. */
function stubViewport(narrow: boolean): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: query === FILES_NARROW_QUERY ? narrow : false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function Here() {
  const location = useLocation();
  return <span data-testid="here">{location.pathname}</span>;
}

/** The three routes as App.tsx declares them, mounted without the app shell. */
function mount(options: { at: string; home?: string | null; pane?: ReactNode; places?: Record<string, string> }) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[options.at]}>
        <Here />
        <Routes>
          <Route
            path="/files"
            element={
              <FilesHomeProvider nodeId={options.home}>
                <FilesPage pane={options.pane} placeNodeIds={options.places} />
              </FilesHomeProvider>
            }
          />
          <Route path="/files/trash" element={<FilesPage trash />} />
          <Route
            path="/files/:nodeId"
            element={<FilesPage browser={<div data-testid="browser">listing</div>} pane={options.pane} />}
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => stubViewport(false));
afterEach(() => vi.unstubAllGlobals());

describe("the Files shell routes", () => {
  it("mounts the folder route at /files/:nodeId and shows its listing", () => {
    mount({ at: "/files/n-42" });
    expect(screen.getByTestId("here")).toHaveTextContent("/files/n-42");
    expect(screen.getByTestId("browser")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Files" })).toBeInTheDocument();
    // The app shell draws the page's one landmark `<main>`; the listing is a region inside it.
    expect(screen.queryAllByRole("main")).toEqual([]);
  });

  it("mounts the trash route rather than reading `trash` as a node id", () => {
    mount({ at: "/files/trash" });
    expect(screen.getByTestId("here")).toHaveTextContent("/files/trash");
    expect(screen.getByRole("region", { name: "Trash" })).toBeInTheDocument();
    expect(screen.queryByTestId("browser")).not.toBeInTheDocument();
  });
});

describe("the home redirect", () => {
  it("opens the caller's home once the root listing resolves it", () => {
    mount({ at: "/files", home: "home-of-me" });
    expect(screen.getByTestId("here")).toHaveTextContent("/files/home-of-me");
    expect(screen.getByTestId("browser")).toBeInTheDocument();
  });

  it("waits on /files while the listing has not resolved a home", () => {
    mount({ at: "/files", home: undefined });
    expect(screen.getByTestId("here")).toHaveTextContent("/files");
    expect(screen.getByText("Opening your files…")).toBeInTheDocument();
    // One loading sentence on the screen: the places rail does not add a second one.
    expect(screen.queryByText("Waiting for your drive to load.")).toBeNull();
    expect(screen.getByRole("navigation", { name: "Places" })).toHaveAttribute("aria-busy", "true");
  });

  it("says so, and does not redirect, when the caller has no home folder", () => {
    mount({ at: "/files", home: null });
    expect(screen.getByTestId("here")).toHaveTextContent("/files");
    expect(screen.getByText(/do not have a home folder/i)).toBeInTheDocument();
  });

  it("never redirects away from a node route, even with a home resolved", () => {
    mount({ at: "/files/n-7", home: "home-of-me" });
    expect(screen.getByTestId("here")).toHaveTextContent("/files/n-7");
  });
});

describe("the places rail", () => {
  // Starred is retired from the rail.
  const LABELS = ["Home", "Recent", "Shared with me", "My leases", "Trash"];

  it("offers the drive's roots, the saved views and Trash, in order", () => {
    mount({ at: "/files/n-1" });
    const rail = screen.getByRole("navigation", { name: "Places" });
    expect(within(rail).getAllByRole("button").concat(within(rail).getAllByRole("link")).length).toBe(LABELS.length);
    for (const label of LABELS) expect(within(rail).getByText(label)).toBeInTheDocument();
  });

  it("marks Trash current on the trash route and links to it elsewhere", () => {
    const { unmount } = mount({ at: "/files/trash" });
    expect(screen.getByRole("link", { name: /Trash/ })).toHaveAttribute("aria-current", "page");
    unmount();
    mount({ at: "/files/n-1" });
    expect(screen.getByRole("link", { name: /Trash/ })).not.toHaveAttribute("aria-current");
  });

  it("disables a node-addressed place until the listing knows where it goes", async () => {
    // Home is the one node-addressed place on the rail now (Shared and Teams are off it).
    const unresolved = mount({ at: "/files", home: null, places: {} });
    expect(screen.getByRole("button", { name: /Home/ })).toBeDisabled();
    unresolved.unmount();

    mount({ at: "/files", home: null, places: { home: "home-of-me" } });
    expect(screen.getByRole("button", { name: /Home/ })).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: /Home/ }));
    expect(screen.getByTestId("here")).toHaveTextContent("/files/home-of-me");
  });
});

describe("the narrow layout", () => {
  it("keeps the details pane a column when there is room", () => {
    mount({ at: "/files/n-1", pane: <p>details</p> });
    const pane = screen.getByRole("complementary", { name: "Details" });
    expect(pane).toBeVisible();
    expect(pane).not.toHaveAttribute("data-sheet");
    expect(screen.queryByRole("button", { name: "Details" })).not.toBeInTheDocument();
  });

  it("turns the pane into a sheet the user opens below the breakpoint", async () => {
    stubViewport(true);
    mount({ at: "/files/n-1", pane: <p>details</p> });
    // A closed sheet is out of the accessibility tree entirely, so it is queried with `hidden`.
    expect(screen.getByRole("dialog", { hidden: true })).not.toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(screen.getByRole("dialog", { name: "Details" })).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Hide details" }));
    expect(screen.getByRole("dialog", { hidden: true })).not.toBeVisible();
  });

  it("collapses the third column entirely when there is no pane", () => {
    stubViewport(true);
    mount({ at: "/files/n-1" });
    expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Details" })).not.toBeInTheDocument();
  });
});

describe("the page head", () => {
  it("names the listing and carries the breadcrumb and toolbar slots", () => {
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={["/files/n-1"]}>
          <Routes>
            <Route
              path="/files/:nodeId"
              element={<FilesPage breadcrumb={<span>/home/me</span>} toolbar={<button type="button">New</button>} />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    // The masthead is the page's one <h1>; the page adds no second one under it.
    expect(screen.queryAllByRole("heading", { level: 1 })).toEqual([]);
    expect(screen.getByRole("region", { name: "Files" })).toBeInTheDocument();
    expect(screen.getByText("/home/me")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New" })).toBeInTheDocument();
  });
});

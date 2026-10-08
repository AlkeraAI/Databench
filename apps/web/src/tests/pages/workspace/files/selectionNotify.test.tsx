// Telling the page about a selection is a side effect, not part of computing one.
//
// Calling `onSelectionChange` from inside the `setSelection` updater, which React
// runs during the render phase, would update the PAGE while the BROWSER renders,
// and React logs "Cannot update a component while rendering a different
// component". The page would still get its ids, so nothing would look broken.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";

const DRIVE = "drv_1";
const PARENT = "nd_parent";

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: PARENT,
    path: null,
    etag: "e1",
    ctag: "c1",
    attrs: null,
    file: null,
    symlink: null,
    object: null,
    lease: null,
    stale: false,
    trust: null,
    locked: false,
    held: false,
    capabilities: {},
    shared: false,
    trashed: false,
    ...overrides,
  } as unknown as Item;
}

const ROWS: Item[] = [
  item({ id: "nd_a", name: "alpha.txt", file: { size: 1200 } as Item["file"] }),
  item({ id: "nd_b", name: "bravo.md", file: { size: 40 } as Item["file"] }),
];

/** The page as `FilesPage` wires it: a parent holding the selected ids in its own state. */
function Page({ onIds }: { onIds: (ids: readonly string[]) => void }) {
  const [selectedIds, setSelectedIds] = useState<readonly string[]>([]);
  onIds(selectedIds);
  return (
    <>
      <output data-testid="count">{selectedIds.length}</output>
      <FilesBrowser
        driveId={DRIVE}
        parentId={PARENT}
        platform="other"
        onSelectionChange={setSelectedIds}
      />
    </>
  );
}

let errors: string[] = [];

function mount() {
  const client = createQueryClient({ retry: false });
  const seen: string[][] = [];
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Page onIds={(ids) => seen.push([...ids])} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return seen;
}

beforeEach(() => {
  errors = [];
  vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(" "));
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ value: ROWS, nextMarker: null }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

/** React's render-phase-update warning, whatever wording the version uses. */
function renderPhaseWarnings(): string[] {
  return errors.filter((message) => /while rendering a different component/i.test(message));
}

describe("telling the page about a selection", () => {
  // React logs the render-phase warning once per component pair per module
  // registry, so the whole interaction sequence lives in ONE test — a second
  // test asserting the same thing would pass vacuously on the deduped warning.
  it("logs no render-phase update across a click, a re-click and a right-click", async () => {
    const user = userEvent.setup();
    const seen = mount();
    await screen.findByText("alpha.txt");

    await user.click(screen.getByText("alpha.txt"));
    await waitFor(() => expect(seen.at(-1)).toEqual(["nd_a"]));
    await user.click(screen.getByText("bravo.md"));
    await waitFor(() => expect(seen.at(-1)).toEqual(["nd_b"]));
    // The right-click is the Share… path the click-through caught.
    await user.pointer({ keys: "[MouseRight]", target: screen.getByText("alpha.txt") });
    await waitFor(() => expect(seen.at(-1)).toEqual(["nd_a"]));

    expect(renderPhaseWarnings()).toEqual([]);
  });

  it("hands the page the ids it selected, and the count it renders", async () => {
    const user = userEvent.setup();
    const seen = mount();
    await screen.findByText("alpha.txt");

    // A fix that simply stopped calling the page back would silence the warning
    // too, so the notification itself is pinned.
    await user.click(screen.getByText("alpha.txt"));
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("1"));
    expect(seen.at(-1)).toEqual(["nd_a"]);

    await user.keyboard("{Control>}a{/Control}");
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
    expect(seen.at(-1)).toEqual(["nd_a", "nd_b"]);
  });
});

/**
 * Abandoning an inline rename hands the keyboard back to the row it was taken
 * from.
 *
 * It did not: when the field unmounted, `document.activeElement` fell back to
 * `BODY`, so the grid never saw the next keystroke. The one people hit is
 * select-all — and with nothing in the grid listening, the browser's own "select
 * all text" ran instead and highlighted the whole page, nav and details pane
 * included. Escape had worked; the keyboard had simply left.
 *
 * Restoring the focus has to happen without committing: the field commits on
 * blur, and taking the focus away IS a blur.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify({ id: "nd_a", name: "renamed.txt", etag: "e2" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    ),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const ITEM = {
  id: "nd_a",
  ino: 1,
  driveId: "drv_1",
  kind: "file",
  name: "alpha.txt",
  nameDisplay: "alpha.txt",
  etag: "e1",
  attrs: null,
  file: null,
  object: null,
  lease: null,
  capabilities: {},
  trashed: false,
} as unknown as Item;

/** The grid's own arrangement: the editor REPLACES the row's name cell and is
 *  unmounted when it is done. That unmount is where the focus was being lost, so
 *  a harness that leaves the field on screen would prove nothing. */
function Host({ onDone }: { onDone: () => void }) {
  const [renaming, setRenaming] = useState(true);
  return (
    <div role="treegrid">
      <div role="row" data-row-id={ITEM.id} tabIndex={0}>
        {renaming ? (
          <RenameInline
            driveId="drv_1"
            item={ITEM}
            onDone={() => {
              setRenaming(false);
              onDone();
            }}
          />
        ) : (
          <span>{ITEM.name}</span>
        )}
      </div>
      <button type="button">Elsewhere</button>
    </div>
  );
}

function mountInRow(onDone: () => void = () => {}) {
  const client = createQueryClient({ retry: false });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Host onDone={onDone} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const row = document.querySelector<HTMLElement>(`[data-row-id="${ITEM.id}"]`);
  if (row === null) throw new Error("the row did not render");
  return { row };
}

async function fieldFocused(): Promise<HTMLElement> {
  const field = await screen.findByRole("textbox");
  await waitFor(() => expect(document.activeElement).toBe(field));
  return field;
}

describe("escaping an inline rename", () => {
  it("puts the focus back on the row, not out on the document", async () => {
    const user = userEvent.setup();
    const onDone = vi.fn();
    const { row } = mountInRow(onDone);
    await fieldFocused();

    await user.keyboard("qa-SHOULD-NOT-STICK.txt{Escape}");

    expect(onDone).toHaveBeenCalled();
    expect(document.activeElement).toBe(row);
    expect(document.activeElement?.tagName).not.toBe("BODY");
  });

  it("leaves the grid owning select-all, so it never falls through to the browser", async () => {
    const user = userEvent.setup();
    const seen: KeyboardEvent[] = [];
    const { row } = mountInRow();
    // The grid's own handler — the thing the keystroke never reached.
    row.closest('[role="treegrid"]')?.addEventListener("keydown", (event) => {
      const key = event as KeyboardEvent;
      if (key.key === "a" && (key.metaKey || key.ctrlKey)) {
        seen.push(key);
        key.preventDefault();
      }
    });
    await fieldFocused();

    await user.keyboard("{Escape}");
    await user.keyboard("{Control>}a{/Control}");

    expect(seen).toHaveLength(1);
    // Honoured, so the browser does not also run its own select-all over the page.
    expect(seen[0]?.defaultPrevented).toBe(true);
  });

  it("abandons rather than commits, so the typed name never reaches the server", async () => {
    const user = userEvent.setup();
    mountInRow();
    await fieldFocused();

    await user.keyboard("qa-SHOULD-NOT-STICK.txt{Escape}");

    // Taking the focus back is a blur, and blur is where this field commits.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("still commits when the focus leaves for somewhere else", async () => {
    const user = userEvent.setup();
    mountInRow();
    const field = await fieldFocused();

    fireEvent.change(field, { target: { value: "renamed.txt" } });
    await user.click(screen.getByRole("button", { name: "Elsewhere" }));

    // Clicking away is a commit; only Escape abandons.
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const sent = fetchMock.mock.calls[0]?.[0] as Request;
    expect(sent.url).toContain("/items/nd_a");
    expect(sent.method).toBe("PATCH");
    await expect(sent.clone().text()).resolves.toContain("renamed.txt");
  });
});

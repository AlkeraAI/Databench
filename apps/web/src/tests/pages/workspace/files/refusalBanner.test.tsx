import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitForElementToBeRemoved } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { FILES_REFUSAL_MS, FILES_UNDO_TOAST_MS, LimitsProvider } from "@/lib/limits";
import { FilesActions } from "@/pages/workspace/files/FilesActions";
import { UndoToast } from "@/pages/workspace/files/UndoToast";
import { EMPTY_UNDO_STATE, type UndoController } from "@/pages/workspace/files/undo";

// A refused write says so in one line, and that line is the only place the person
// learns the write did not happen. As the last row of a full-height column it was
// drawn against the bottom edge of the window and cut in half, so it is anchored to
// the viewport instead — where no window height can clip it.

// The page's sheet and the listing's: the create form's rules live with the
// listing, which the chat's Files pane loads too.
const CSS = ["files-page.css", "treegrid.css"]
  .map((name) => readFileSync(resolve(process.cwd(), "src/pages/workspace/files", name), "utf8"))
  .join("\n");

/** One rule's declarations, by selector. */
function rule(selector: string): string {
  const at = CSS.indexOf(`\n${selector} {`);
  expect(at).toBeGreaterThan(-1);
  return CSS.slice(at, CSS.indexOf("}", at));
}

/** A step on the undo stack, so the offer this refusal is drawn beside is a real
 *  one rather than a stand-in. */
const OFFERING: UndoController = {
  state: EMPTY_UNDO_STATE,
  push: () => undefined,
  undo: () => Promise.resolve(),
  redo: () => Promise.resolve(),
  clear: () => undefined,
  canUndo: true,
  canRedo: false,
  pending: { operationId: "op_1", driveId: "dr_1", kind: "trash", label: "Moved 1 item to trash" },
  running: false,
};

/** The page, with a control that refuses. `refusalMs` is how long the sentence
 *  stays up — taken down to a tick where a case is about the clock; `undo` mounts
 *  the offer the refusal is drawn over. */
function mountRefusing(refusalMs = 60_000, undo = false): void {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <LimitsProvider overrides={{ filesRefusalMs: refusalMs }}>
        <MemoryRouter>
          <FilesActions canWriteHere driveId="dr_1" currentFolderId="nd_home" selection={[]}>
            {(api) => (
              <>
                <button type="button" onClick={() => api.refuse("Nothing was moved.", "Try again.")}>
                  refuse
                </button>
                {undo ? <UndoToast controller={OFFERING} timeoutMs={0} /> : null}
              </>
            )}
          </FilesActions>
        </MemoryRouter>
      </LimitsProvider>
    </QueryClientProvider>,
  );
}

describe("a refused write", () => {
  it("says why, in an alert the person can act on", async () => {
    mountRefusing();

    await userEvent.click(screen.getByRole("button", { name: "refuse" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Nothing was moved.");
    expect(alert).toHaveTextContent("Try again.");
    // The class carries the anchoring the rule below pins; an alert drawn as an
    // ordinary last row is the bug this replaced.
    expect(alert).toHaveClass("alk-files__refusal");
  });

  // It is drawn over the page, so while it is up it covers the selection bar, the
  // undo offer and the paging footer. Both ways out are the point.
  it("goes away when the reader dismisses it", async () => {
    mountRefusing();
    await userEvent.click(screen.getByRole("button", { name: "refuse" }));
    await screen.findByRole("alert");

    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("goes away on its own when nobody does", async () => {
    mountRefusing(20);
    await userEvent.click(screen.getByRole("button", { name: "refuse" }));

    await waitForElementToBeRemoved(() => screen.queryByRole("alert"));
  });
});

describe("a refusal beside an undo offer", () => {
  // The refusal is drawn over the page and the offer is drawn under it, in the
  // same bottom lane. An offer whose button cannot be pressed is not an offer.
  it("leaves the Undo button reachable while the refusal is up", async () => {
    mountRefusing(60_000, true);
    await userEvent.click(screen.getByRole("button", { name: "refuse" }));
    await screen.findByRole("alert");

    const undo = screen.getByRole("button", { name: "Undo" });
    expect(undo).toBeEnabled();
    expect(screen.getByRole("status")).toBeInTheDocument();
    undo.focus();
    expect(document.activeElement).toBe(undo);
  });

  it("does not outlive the offer it is drawn over", () => {
    // The layout keeps them apart; this keeps them apart if it ever does not.
    expect(FILES_REFUSAL_MS).toBeLessThan(FILES_UNDO_TOAST_MS);
  });

  it("stands a notice above the lane the offer sits in", () => {
    // The offer is sticky at the bottom lane; the refusal is fixed one notice
    // higher, so the opaque banner is not over the button.
    expect(rule(".alk-files__toast")).toMatch(/bottom:\s*var\(--alkSpace3\);/);
    expect(rule(".alk-files__refusal")).toMatch(
      /inset-block-end:\s*calc\(var\(--alkSpace4\)\s*\+\s*[\d.]+rem\)/,
    );
  });
});

describe("the refusal's layout", () => {
  it("is anchored to the viewport, off the bottom edge", () => {
    const refusal = rule(".alk-files__refusal");
    expect(refusal).toMatch(/position:\s*fixed/);
    // Held clear of the bottom edge rather than sitting on it.
    expect(refusal).toMatch(/inset-block-end:\s*calc\(var\(--alkSpace[2-9]/);
    expect(refusal).toMatch(/z-index:/);
  });

  it("keeps a gutter and a reading width, so a narrow window does not run it edge to edge", () => {
    const refusal = rule(".alk-files__refusal");
    expect(refusal).toMatch(/inset-inline:\s*var\(--alkSpace[2-9]/);
    expect(refusal).toMatch(/max-inline-size:/);
    expect(refusal).toMatch(/margin-inline:\s*auto/);
  });

  it("leaves the inline refusal under a name field where it is", () => {
    // The new-folder editor's own refusal hangs under the field it belongs to;
    // floating it would put it over the listing, away from what it is about.
    expect(rule(".alk-files__new-folder .alk-files__error")).toMatch(/position:\s*absolute/);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

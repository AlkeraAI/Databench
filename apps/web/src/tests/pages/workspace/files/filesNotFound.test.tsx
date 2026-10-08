// An address that cannot name a node in the drive.
//
// `/files/not-a-uuid` reached the drive anyway and came back as the server's
// validator — a 422 the landing rule had no reading for, so the page drew
// "Opening…" and stayed there for good. Two things had to change and both are
// pinned here: the shape is decided in front of the page, so a malformed id
// costs no request at all; and a 422 that still arrives (an older client's link,
// an id shape the server tightens later) is an ANSWER, landing where a 403 and a
// 404 already land rather than back on the spinner.

import { cleanup, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { FilesRoute, isFilesNodeId } from "@/pages/workspace/files/FilesRoute";
import { landingState } from "@/pages/workspace/files/landing";
import { ApiError } from "@/api/errors";

const NOT_FOUND = "This page doesn't exist";
const UUID = "6f1a1c2e-6d41-4e2a-9a77-2b1f0c9d4e55";

/** The Files screen stands in for "the page mounted" — every drive read the
 *  reader saw hang is started by it. It does not mount, they do not happen. */
function Screen() {
  return <div data-testid="files-screen">Opening…</div>;
}

function renderAt(path: string) {
  const spy = vi.fn(async () => new Response("{}", { status: 200 }));
  vi.stubGlobal("fetch", spy);
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/files/:nodeId"
            element={
              <FilesRoute>
                <Screen />
              </FilesRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return spy;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("an id that cannot name a node", () => {
  it("lands on the app's dead end without asking the drive anything", () => {
    const spy = renderAt("/files/not-a-uuid");

    expect(screen.getByText(NOT_FOUND)).toBeTruthy();
    expect(screen.queryByTestId("files-screen")).toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it("never prints the id it refused, or the validator's words", () => {
    renderAt("/files/not-a-uuid");

    const page = document.body.textContent ?? "";
    expect(page).not.toMatch(/not-a-uuid/);
    expect(page).not.toMatch(/uuid/i);
    expect(page).not.toMatch(/Opening/);
  });

  it.each([
    ["a name someone typed", "quarterlies"],
    ["a uuid with a character too many", `${UUID}0`],
    ["a uuid with a character missing", UUID.slice(0, -1)],
    ["a uuid with a letter outside hex", UUID.replace("6f1a", "6z1a")],
    ["the empty-looking one a trailing slash leaves", "%20"],
  ])("refuses %s", (_label, id) => {
    expect(isFilesNodeId(id)).toBe(false);
  });

  it("lets a real node id through to the page, in either case", () => {
    expect(isFilesNodeId(UUID)).toBe(true);
    expect(isFilesNodeId(UUID.toUpperCase())).toBe(true);

    renderAt(`/files/${UUID}`);
    expect(screen.getByTestId("files-screen")).toBeTruthy();
    expect(screen.queryByText(NOT_FOUND)).toBeNull();
  });
});

describe("a malformed id the drive answers anyway", () => {
  it("settles on the not-here surface instead of waiting for a node", () => {
    const validator = new ApiError(422, {
      code: "validation_error",
      message: "Input should be a valid UUID, invalid character: found `n` at 1",
    });

    expect(landingState({ node: undefined, nodeError: validator })).toBe("not-here");
  });
});

// The visual defects a customer read on the live portal, pinned where they can be pinned in
// jsdom: the trail spelling the drive's root the way the rail does, the rail's first group in
// the order a person meets it, and the upload tray's bar carrying the state its colour is
// chosen from. The pixels themselves are the Playwright baselines' job; these three are the
// markup those styles key on, so a regression here is caught on every PR.

import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { Breadcrumbs } from "@/pages/workspace/files/Breadcrumbs";
import { FILES_PLACES, Sidebar } from "@/pages/workspace/files/Sidebar";
import { UploadTray } from "@/pages/workspace/files/UploadTray";
import type { UploadRow } from "@/pages/workspace/files/useUploads";

function row(state: UploadRow["state"], sent: number): UploadRow {
  return {
    uploadId: `u-${state}`,
    name: `${state}.bin`,
    sent,
    total: 4096,
    partsDone: 1,
    partsTotal: 4,
    state,
  };
}

describe("the Files trail", () => {
  it("names the drive's root the way the rail does", () => {
    render(
      <Breadcrumbs
        segments={[
          { id: "r", name: "home" },
          { id: "c", name: "quarterlies" },
        ]}
      />,
    );

    expect(screen.getByRole("button", { name: "Home" })).toBeInTheDocument();
    expect(screen.queryByText("home")).not.toBeInTheDocument();
  });

  it("leaves a folder a person made alone, even one called home", () => {
    render(
      <Breadcrumbs
        segments={[
          { id: "r", name: "shared" },
          { id: "c", name: "home" },
        ]}
      />,
    );

    expect(screen.getByRole("button", { name: "Shared" })).toBeInTheDocument();
    expect(screen.getByText("home").closest(".alk-files-crumbs__current")).toHaveAttribute(
      "aria-current",
      "page",
    );
  });
});

describe("the Files rail", () => {
  it("reads Home first — the place /files lands in", () => {
    render(
      <MemoryRouter>
        <Sidebar current="home" />
      </MemoryRouter>,
    );

    const groups = screen.getByRole("navigation", { name: "Places" }).querySelectorAll("ul");
    const first = [...within(groups[0] as HTMLElement).getAllByRole("button")].map(
      (node) => node.textContent,
    );
    // Shared and Teams are off the rail for now.
    expect(first).toEqual(["Home"]);
  });

  it("still offers every place, so the phone strip has all of them to scroll", () => {
    const ids = FILES_PLACES.flatMap((group) => group.map((place) => place.id));
    expect(ids).toEqual([
      "home",
      // "shared" and "teams" are off the rail for now.
      "recent",
      // "starred" is retired.
      "sharedWithMe",
      "leases",
      "trash",
    ]);
  });
});

describe("the upload tray's bar", () => {
  it("carries the row's state, which is what its colour is chosen from", () => {
    render(
      <UploadTray
        rows={[row("uploading", 1024), row("done", 4096), row("failed", 512)]}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={1}
        refusal={null}
        conflicts={[]}
        resumable={[]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onAnswerConflict={() => {}}
      />,
    );

    const stateOf = (name: string): string | null =>
      screen.getByLabelText(`${name} progress`).getAttribute("data-state");

    expect(stateOf("uploading.bin")).toBe("uploading");
    // The one that mattered: a finished upload painted the browser's default bar, which is red.
    expect(stateOf("done.bin")).toBe("done");
    expect(stateOf("failed.bin")).toBe("failed");
  });

  it("keeps the same four cells on every row, so the bar's column never moves", () => {
    render(
      <UploadTray
        rows={[row("uploading", 1024), row("paused", 2048), row("done", 4096), row("failed", 512)]}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={1}
        refusal={null}
        conflicts={[]}
        resumable={[]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onAnswerConflict={() => {}}
      />,
    );

    // The bar slid sideways between a row that still had Pause and Cancel and one that
    // had finished, because the buttons were extra grid tracks. Now every row has a
    // name, a bar, a word and an actions cell in that order — the cell is there, empty,
    // on a finished row too — and the CSS gives each a fixed track.
    for (const item of screen.getAllByRole("listitem")) {
      const cells = Array.from(item.children).slice(0, 4);
      expect(cells.map((cell) => cell.tagName)).toEqual(["SPAN", "PROGRESS", "SPAN", "SPAN"]);
      expect(cells[3]).toHaveClass("alk-files-uploads__actions");
    }
    const finished = screen.getByLabelText("done.bin progress").closest("li")!;
    expect(finished.querySelector(".alk-files-uploads__actions")?.childElementCount).toBe(0);
    const moving = screen.getByLabelText("uploading.bin progress").closest("li")!;
    expect(moving.querySelector(".alk-files-uploads__actions")?.childElementCount).toBe(2);
  });
});

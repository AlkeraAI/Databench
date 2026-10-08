/**
 * The upload tray fits the pane it sits in.
 *
 * The tray's four tracks (name, bar, word, actions) were folded by a viewport
 * query, but the tray lives in a Files listing beside a details pane and in a
 * chat's side pane — both far narrower than the window. There the fixed tracks
 * came out wider than the tray, and the status word and the actions ran past
 * its right edge. The fold is now keyed to the tray's own width, and the name
 * and the word each give their width back inside their cell.
 *
 * The tray's rules also lived only in the Files page's sheet, so a chat's Files
 * tab showed an unstyled tray unless the Files page had been visited first.
 * The tray loads its own sheet now.
 *
 * jsdom neither lays out nor applies container queries, so the declarations the
 * layout rests on are read off the sheet that states them, and the markup is
 * rendered to prove the cells carry the classes those rules key on.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { NAME_BUDGET, UploadTray, headLine } from "@/pages/workspace/files/UploadTray";
import type { UploadRow } from "@/pages/workspace/files/useUploads";

const loaded = vi.hoisted(() => ({ traySheet: false }));
vi.mock("@/pages/workspace/files/upload-tray.css", () => {
  loaded.traySheet = true;
  return {};
});

const WEB = process.cwd();
const sheet = (path: string): string => readFileSync(path, "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const TRAY_CSS = sheet(join(WEB, "src/pages/workspace/files/upload-tray.css"));
const PAGE_CSS = sheet(join(WEB, "src/pages/workspace/files/files-page.css"));

/** Every top-level `prelude { body }` block, with nested blocks kept whole. */
function blocks(css: string): [prelude: string, body: string][] {
  const out: [string, string][] = [];
  let depth = 0;
  let head = 0;
  let start = 0;
  for (let i = 0; i < css.length; i += 1) {
    if (css[i] === "{") {
      if (depth === 0) {
        out.push([css.slice(head, i).trim(), ""]);
        start = i + 1;
      }
      depth += 1;
    } else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) {
        out[out.length - 1]![1] = css.slice(start, i);
        head = i + 1;
      }
    }
  }
  return out;
}

/** The body of the top-level rule whose prelude is exactly `selector`. */
function rule(css: string, selector: string): string {
  const found = blocks(css).find(([prelude]) => prelude === selector);
  expect(found, `no rule for \`${selector}\``).toBeDefined();
  return found![1];
}

const LONG_NAME = "quarterly-revenue-by-region-and-channel-final-v7-reviewed-by-finance.csv";

function row(overrides: Partial<UploadRow> = {}): UploadRow {
  return {
    uploadId: "u-1",
    name: LONG_NAME,
    sent: 512,
    total: 4096,
    partsDone: 1,
    partsTotal: 8,
    state: "waiting",
    ...overrides,
  };
}

function mountNarrow() {
  return render(
    <div className="alk-files-tray" style={{ width: 320 }}>
      <UploadTray
        rows={[row()]}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        refusal={null}
        conflicts={[]}
        resumable={[]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onAnswerConflict={() => {}}
      />
    </div>,
  );
}

describe("the upload tray in a narrow pane", () => {
  it("folds its rows against the tray's own width, not the window's", () => {
    expect(rule(TRAY_CSS, ".alk-files-uploads")).toMatch(/container:\s*alk-uploads\s*\/\s*inline-size/);
    const fold = blocks(TRAY_CSS).find(([prelude]) => /^@container alk-uploads\b/.test(prelude));
    expect(fold, "no container fold for the tray").toBeDefined();
    expect(rule(fold![1], ".alk-files-uploads__row")).toMatch(/grid-template-columns:/);
    // A viewport query is the bug: it never fires for a narrow pane in a wide window.
    for (const css of [TRAY_CSS, PAGE_CSS]) {
      const viewportFolds = blocks(css).filter(
        ([prelude, body]) => prelude.startsWith("@media") && body.includes(".alk-files-uploads__row"),
      );
      expect(viewportFolds).toEqual([]);
    }
  });

  it("keeps a long name and a long status inside their cells", () => {
    mountNarrow();
    const item = screen.getByRole("listitem");
    const [name, bar, status] = Array.from(item.children);

    expect(name).toHaveClass("alk-name");
    expect(name).toHaveAttribute("title", LONG_NAME);
    expect([...(name?.textContent ?? "")]).toHaveLength(NAME_BUDGET);
    expect(bar?.tagName).toBe("PROGRESS");
    expect(status).toHaveClass("alk-files-uploads__status");
    expect(status).toHaveTextContent("Waiting for the server…");

    expect(rule(TRAY_CSS, ".alk-files-uploads__row")).toMatch(/grid-template-columns:\s*minmax\(0,\s*1fr\)/);
    const nameCell = rule(TRAY_CSS, ".alk-files-uploads__row > .alk-name");
    expect(nameCell).toMatch(/min-width:\s*0/);
    expect(nameCell).toMatch(/overflow-wrap:\s*anywhere/);
    // The word never wraps beside the bar; in the fold it takes a column of its
    // own on the name's line and the bar spans the row beneath them.
    const statusCell = rule(TRAY_CSS, ".alk-files-uploads__status");
    expect(statusCell).toMatch(/min-width:\s*0/);
    expect(statusCell).toMatch(/white-space:\s*nowrap/);
    const fold = blocks(TRAY_CSS).find(([prelude]) => /^@container alk-uploads\b/.test(prelude));
    expect(rule(fold![1], ".alk-files-uploads__status")).toMatch(/grid-column:\s*2/);
    expect(rule(fold![1], ".alk-files-uploads__bar")).toMatch(/grid-column:\s*1 \/ -1/);
    expect(rule(TRAY_CSS, ".alk-files-uploads__row")).toMatch(/font-size:\s*var\(--alkTextSm\)/);
  });

  it("loads its own sheet, so a chat's Files tab gets the same layout", () => {
    expect(loaded.traySheet).toBe(true);
    expect(PAGE_CSS).not.toMatch(/\.alk-files-uploads/);
    expect(PAGE_CSS).not.toMatch(/\.alk-files-tray/);
  });
});

function mountAsking() {
  return render(
    <div className="alk-files-tray" style={{ width: 320 }}>
      <UploadTray
        rows={[row({ name: "notes.md", state: "completing" })]}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        refusal={null}
        conflicts={[{ uploadId: "u-1", name: "notes.md", parentId: "folder-1" }]}
        resumable={[]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onAnswerConflict={() => {}}
      />
    </div>,
  );
}

describe("the upload tray's question about a taken name", () => {
  it("keeps its three choices in view: the scroll cap is the section's, not the question's", () => {
    // The question block is a flex child of the capped section. A cap that also
    // matched the block let the section shrink it to one line — the sentence —
    // and scroll the hint and the three choices out of sight inside it.
    const capped = blocks(TRAY_CSS).filter(([, body]) => /overflow-y:\s*auto/.test(body));
    expect(capped.map(([prelude]) => prelude)).toEqual([".alk-files-tray > .alk-files-uploads"]);
    const [cap] = capped[0]!;

    mountAsking();
    const section = screen.getByRole("region", { name: "Uploads" });
    const question = screen.getByRole("group", { name: "Name taken: notes.md" });
    expect(section.matches(cap)).toBe(true);
    expect(question.matches(cap)).toBe(false);
    expect(question.querySelector(cap)).toBeNull();
    // The question sits under the row it is about, not above the list.
    expect(within(screen.getByRole("listitem")).getByRole("group", { name: "Name taken: notes.md" })).toBe(
      question,
    );

    const choices = question.querySelector(".alk-files-uploads__choices");
    expect(choices).not.toBeNull();
    for (const name of ["Replace", "Keep both", "Skip"]) {
      expect(within(choices as HTMLElement).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(rule(TRAY_CSS, ".alk-files-uploads__choices")).toMatch(/display:\s*flex/);
  });

  it("asks at the tray's own size, with the row it is about saying so", () => {
    mountAsking();
    const question = screen.getByRole("group", { name: "Name taken: notes.md" });
    expect(question).toHaveTextContent("notes.md already exists here.");
    expect(question.querySelector(".alk-body")).toBeNull();
    expect(rule(TRAY_CSS, ".alk-files-uploads__conflict")).toMatch(/font-size:\s*var\(--alkTextSm\)/);
    expect(screen.getByRole("listitem")).toHaveTextContent("Waiting for your answer");
  });
});

describe("the upload tray's head line", () => {
  const props = {
    skippedSidecars: 0,
    identicalCopies: 0,
    alreadyInFiles: 0,
    finished: 0,
    refusal: null,
    resumable: [],
    onPause: () => {},
    onResume: () => {},
    onCancel: () => {},
    onAnswerConflict: () => {},
  };

  it("counts every row the tray shows, not only the latest drop", () => {
    // Seven rows from earlier drops still listed, one from a drop of one file.
    const rows = [
      ...Array.from({ length: 7 }, (_, i) =>
        row({ uploadId: `old-${i}`, name: `old-${i}.txt`, state: "done", sent: 10, total: 10 }),
      ),
      row({ uploadId: "u-new", name: "f-beta.txt", state: "uploading", sent: 4, total: 10 }),
    ];
    const batch = { total: 1, done: 1, failed: 0, current: null, stopped: null };
    expect(headLine(rows, batch)).toBe("Uploading 7 of 8: f-beta.txt");
    render(
      <div className="alk-files-tray">
        <UploadTray {...props} rows={rows} batch={batch} conflicts={[]} />
      </div>,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Uploading 7 of 8: f-beta.txt");
  });

  // Three files whose bytes are all up, each waiting on its commit, read
  // "Uploading 0 of 3" over three full bars.
  it("counts a file whose bytes are all up as uploaded, and says the batch is finishing", () => {
    const finishing = (id: string) =>
      row({ uploadId: id, name: `${id}.csv`, state: "completing", sent: 10, total: 10 });
    expect(headLine([finishing("a"), finishing("b"), finishing("c")], null)).toBe("Finishing 3 files");
    expect(
      headLine(
        [
          row({ uploadId: "a", name: "a.csv", state: "done", sent: 10, total: 10 }),
          finishing("b"),
          row({ uploadId: "c", name: "c.csv", state: "uploading", sent: 3, total: 10 }),
        ],
        null,
      ),
    ).toBe("Uploading 2 of 3: c.csv");
    expect(
      headLine([row({ uploadId: "a", name: "a.csv", state: "done", sent: 10, total: 10 }), finishing("b")], null),
    ).toBe("Finishing 1 file");
  });

  it("ends on a count of the files that landed", () => {
    const done = (id: string) => row({ uploadId: id, name: `${id}.csv`, state: "done", sent: 10, total: 10 });
    expect(headLine([done("a"), done("b"), done("c")], null)).toBe("3 files uploaded");
  });

  it("still knows what only the drop knows: files it settled without a row, and why it stopped", () => {
    const rows = [row({ uploadId: "u-1", name: "a.txt", state: "done", sent: 10, total: 10 })];
    expect(headLine(rows, { total: 3, done: 2, failed: 0, current: null, stopped: null })).toBe(
      "Uploading 2 of 3",
    );
    expect(headLine(rows, { total: 3, done: 1, failed: 0, current: null, stopped: "full" })).toBe(
      "Stopped after 1 of 3, 2 not uploaded",
    );
    expect(headLine([], null)).toBeNull();
  });

  it("asks a question whose upload is not listed above the list, once", () => {
    render(
      <div className="alk-files-tray">
        <UploadTray
          {...props}
          rows={[]}
          batch={null}
          conflicts={[{ uploadId: "u-gone", name: "notes.md", parentId: "folder-1" }]}
        />
      </div>,
    );
    const question = screen.getByRole("group", { name: "Name taken: notes.md" });
    expect(question.closest("li")).toBeNull();
    expect(screen.getAllByRole("group")).toHaveLength(1);
    expect(screen.queryByRole("listitem")).toBeNull();
  });
});

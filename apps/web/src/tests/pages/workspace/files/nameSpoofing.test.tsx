/**
 * A name cannot pretend to be a different name.
 *
 * A right-to-left override in `qa-\u202egnp.exe` makes it read `qa-exe.png`. The
 * listing was safe because the server escapes the name it sends as
 * `nameDisplay`, but the details pane printed the raw path and the upload tray
 * the raw name the browser handed over, and both read `…exe.png` for a `.exe`.
 * Every name on screen now goes through one helper; this pins the helper against
 * Unicode's own Bidi_Control property (not a list spelled here), against the
 * invisibles and controls the server escapes, and pins the two surfaces that
 * leaked.
 */

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { displayNameOf, displayPath, locationOf } from "@/lib/files/columns";
import { RightPane } from "@/pages/workspace/files/RightPane";
import { BIDI_CONTROLS, shownName } from "@/lib/files/shownName";
import { UploadTray, headLine } from "@/pages/workspace/files/UploadTray";
import type { UploadRow } from "@/pages/workspace/files/useUploads";

const RLO = "\u202e";
const SPOOF = `qa-p2w9-${RLO}gnp.exe`;
const ESCAPED = "qa-p2w9-\\u202egnp.exe";

/** Every code point Unicode itself calls a bidi control, found by the regex
 *  property rather than listed — a control the list forgot still turns up. */
const UNICODE_BIDI: number[] = (() => {
  const found: number[] = [];
  for (let cp = 0; cp <= 0x10ffff; cp += 1) {
    if (cp >= 0xd800 && cp <= 0xdfff) continue;
    if (/\p{Bidi_Control}/u.test(String.fromCodePoint(cp))) found.push(cp);
  }
  return found;
})();

const hex = (cp: number): string => cp.toString(16).padStart(4, "0");

describe("the name escape", () => {
  it("neutralises the right-to-left override", () => {
    expect(shownName(SPOOF)).toBe(ESCAPED);
    expect(shownName(SPOOF)).not.toContain(RLO);
  });

  it("knows every bidi control Unicode has", () => {
    // The list the helper exports is the whole property, no more and no less.
    expect([...BIDI_CONTROLS].sort((a, b) => a - b)).toEqual(UNICODE_BIDI);
    expect(UNICODE_BIDI.length).toBeGreaterThanOrEqual(12);
  });

  it.each(UNICODE_BIDI.map((cp) => [`U+${hex(cp).toUpperCase()}`, cp] as const))(
    "escapes %s wherever it sits in a name",
    (_label, cp) => {
      const ch = String.fromCodePoint(cp);
      for (const name of [`${ch}a.txt`, `a${ch}b.txt`, `a.txt${ch}`]) {
        const shown = shownName(name);
        expect(shown).not.toContain(ch);
        expect(shown).toContain(`\\u${hex(cp)}`);
      }
    },
  );

  it.each([
    ["a tab", 0x09, "\\u0009"],
    ["a null", 0x00, "\\u0000"],
    ["DEL", 0x7f, "\\u007f"],
    ["a C1 control", 0x85, "\\u0085"],
    ["a zero-width space", 0x200b, "\\u200b"],
    ["a word joiner", 0x2060, "\\u2060"],
    ["a byte-order mark", 0xfeff, "\\ufeff"],
    ["the language tag", 0xe0001, "\\U000e0001"],
    ["a tag letter", 0xe0041, "\\U000e0041"],
  ])("escapes %s the way the server spells it", (_label, cp, spelled) => {
    const shown = shownName(`x${String.fromCodePoint(cp)}y`);
    expect(shown).toBe(`x${spelled}y`);
  });

  it.each([
    ["accents and CJK", "café-日本語-😀.md"],
    ["Cyrillic", "Привет.txt"],
    ["a decomposed accent", "café.md"],
    ["right-to-left letters", "שלום-مرحبا.txt"],
    ["a backslash", "back\\slash.txt"],
    ["spaces", "  leading and trailing  "],
  ])("leaves %s exactly as it is", (_label, name) => {
    expect(shownName(name)).toBe(name);
  });

  it("changes nothing the server already escaped, so running it twice is running it once", () => {
    const once = shownName(`${SPOOF}\u200b\t\u{e0001}`);
    expect(shownName(once)).toBe(once);
    // The server's own spelling of the override, as `nameDisplay` carries it.
    expect(shownName(ESCAPED)).toBe(ESCAPED);
  });
});

const FILE = {
  id: "nd_spoof",
  driveId: "drv_1",
  kind: "file",
  name: SPOOF,
  nameDisplay: ESCAPED,
  pathBytes: "",
  parentId: "nd_parent",
  parentName: "qa-p2w9-folder",
  path: `/home/qa-p2w9-owner/qa-p2w9-folder/${SPOOF}`,
  etag: "e1",
  ctag: "c1",
  attrs: { owner: "owner", mtime: "2026-03-04T10:00:00Z" },
  file: { size: 10 },
  trashed: false,
} as unknown as Item;

describe("the names the Files helpers hand to every surface", () => {
  it("escapes the path, which is the raw names joined", () => {
    expect(displayPath(FILE)).toBe(`/home/qa-p2w9-owner/qa-p2w9-folder/${ESCAPED}`);
    expect(displayPath({ path: undefined, pathBytes: `/a/${RLO}b` } as unknown as Item)).toBe(
      "/a/\\u202eb",
    );
  });

  it("escapes a chat's title, which is whatever a person typed", () => {
    const chat = { ...FILE, object: { type: "chat", title: `Plan ${RLO}fdp.exe` } } as unknown as Item;
    expect(displayNameOf(chat)).toBe("Plan \\u202efdp.exe");
  });

  it("escapes the name a row's location link shows", () => {
    const row = { ...FILE, parentName: `in${RLO}side` } as unknown as Item;
    expect(locationOf(row)?.name).toBe("in\\u202eside");
  });
});

describe("the details pane", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(JSON.stringify({ value: [], versions: [] }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }),
        ),
      ),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("never draws the override in the path a reader trusts", () => {
    const { container } = render(
      <QueryClientProvider client={createQueryClient({ retry: false })}>
        <MemoryRouter>
          <RightPane driveId="drv_1" item={FILE} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const path = screen.getByText("Path").nextElementSibling?.textContent ?? "";
    expect(path).toBe(`/home/qa-p2w9-owner/qa-p2w9-folder/${ESCAPED}`);
    expect(container.textContent).not.toContain(RLO);
    for (const el of Array.from(container.querySelectorAll("[title],[aria-label]"))) {
      expect(el.getAttribute("title") ?? "").not.toContain(RLO);
      expect(el.getAttribute("aria-label") ?? "").not.toContain(RLO);
    }
  });
});

describe("the upload tray", () => {
  const row: UploadRow = {
    uploadId: "u-1",
    name: SPOOF,
    sent: 1,
    total: 10,
    partsDone: 0,
    partsTotal: 1,
    state: "uploading",
  };

  it("shows a dropped file's name escaped before any server has seen it", () => {
    const { container } = render(
      <UploadTray
        rows={[row, { ...row, uploadId: "u-2", state: "failed", error: undefined }]}
        skippedSidecars={0}
        identicalCopies={0}
        alreadyInFiles={0}
        finished={0}
        refusal={null}
        conflicts={[{ uploadId: "u-3", name: SPOOF, parentId: "p" } as never]}
        resumable={[{ name: SPOOF } as never]}
        onPause={() => {}}
        onResume={() => {}}
        onCancel={() => {}}
        onAnswerConflict={() => {}}
        onRetry={() => {}}
      />,
    );
    expect(screen.getAllByText(ESCAPED).length).toBeGreaterThan(0);
    expect(container.textContent).not.toContain(RLO);
    for (const el of Array.from(container.querySelectorAll("[title],[aria-label]"))) {
      expect(el.getAttribute("title") ?? "").not.toContain(RLO);
      expect(el.getAttribute("aria-label") ?? "").not.toContain(RLO);
    }
  });

  it("names the file it is on in the head line escaped too", () => {
    expect(headLine([row], null)).toContain(ESCAPED);
    expect(headLine([row], null)).not.toContain(RLO);
  });
});

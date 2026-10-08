// What the tray does with the files that did NOT land.
//
// A batch of sixty that quietly delivers twelve is the worst outcome the Files
// page can produce: the person believes their work is uploaded. So every
// failure is a row with the file's own name and the server's own sentence, the
// batch ends with a line naming both halves of the count, and Retry re-sends
// the failures and ONLY the failures — a retry that re-sent the successes would
// ask for a name-collision answer per file that already landed.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import type { DropEntry, DropTarget } from "@/pages/workspace/files/dropHandlers";
import { middleEllipsis, UploadTray } from "@/pages/workspace/files/UploadTray";
import { useUploads, type DropTransfer } from "@/pages/workspace/files/useUploads";

const WRITABLE: DropTarget = {
  id: "folder-root",
  name: "Home",
  kind: "folder",
  capabilities: { can_write: true },
};

function fileEntry(name: string, body = "id,name\n1,ok\n"): DropEntry {
  const file = new File(body === "" ? [] : [body], name, { lastModified: Date.UTC(2020, 0, 1) });
  return { isFile: true, isDirectory: false, name, file: (onSuccess) => onSuccess(file) };
}

function transfer(entries: DropEntry[]): DropTransfer {
  return {
    items: entries.map((entry) => ({ kind: "file", webkitGetAsEntry: () => entry })),
    getData: () => "",
  };
}

/** How the scripted server answers one file, by the name the open call carries. */
interface Script {
  /** Refuse the session open with this status + code. */
  openStatus?: number;
  openCode?: string;
  openMessage?: string;
  /** Refuse the part upload with this status + code. */
  partStatus?: number;
  partCode?: string;
  partMessage?: string;
  /** Refuse the first completion with a 409, as a taken name does. */
  collide?: boolean;
  /** Reject the request outright, the way an offline tab does. */
  offline?: boolean;
}

interface Wire {
  /** Every request, as `METHOD path` plus the file it belonged to. */
  touched: { method: string; path: string; name: string }[];
  script: Map<string, Script>;
  /** Attempts per file name, counted from the session opens and part PUTs. */
  sends: (name: string) => number;
}

function stubFetch(): Wire {
  const sessions = new Map<string, string>();
  const collided = new Set<string>();
  let opened = 0;
  const wire: Wire = {
    touched: [],
    script: new Map(),
    sends: (name) =>
      wire.touched.filter(
        (call) =>
          call.name === name &&
          ((call.method === "POST" && call.path.endsWith("/api/v1/files/uploads")) ||
            call.method === "PUT"),
      ).length,
  };
  const json = (payload: unknown, status = 200) =>
    new Response(JSON.stringify(payload), {
      status,
      headers: { "content-type": "application/json" },
    });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const path = new URL(url, "http://localhost").pathname;
      const method = (init?.method ?? "GET").toUpperCase();
      const raw = typeof init?.body === "string" ? init.body : "";
      let body: Record<string, unknown> = {};
      try {
        body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      } catch {
        body = {};
      }

      if (method === "POST" && path.endsWith("/api/v1/files/uploads")) {
        const name = String(body.name ?? "");
        wire.touched.push({ method, path, name });
        const script = wire.script.get(name) ?? {};
        if (script.offline) throw new TypeError("Failed to fetch");
        if (script.openStatus) {
          return json(
            { code: script.openCode ?? "files.error", message: script.openMessage ?? "" },
            script.openStatus,
          );
        }
        opened += 1;
        const uploadId = `sess-${opened}`;
        sessions.set(uploadId, name);
        return json({ uploadId, partSize: 1024, partsTotal: 1, expiresAt: "" });
      }

      const sessionId = /\/uploads\/(sess-\d+)(\/|$)/.exec(path)?.[1];
      const name = sessionId ? (sessions.get(sessionId) ?? "") : "";
      wire.touched.push({ method, path, name });
      const script = wire.script.get(name) ?? {};
      if (script.offline) throw new TypeError("Failed to fetch");

      if (sessionId && method === "GET") {
        return json({
          uploadId: sessionId,
          state: "open",
          offset: 0,
          length: 1024,
          complete: false,
          partsDone: 0,
          partsTotal: 1,
          acceptedParts: [],
        });
      }
      if (sessionId && method === "PUT") {
        if (script.partStatus) {
          return json(
            { code: script.partCode ?? "files.error", message: script.partMessage ?? "" },
            script.partStatus,
          );
        }
        return json({ ok: true });
      }
      if (sessionId && path.endsWith("/complete")) {
        if (script.collide && !collided.has(name)) {
          collided.add(name);
          return json({ code: "files.exists", message: "that name is taken in this folder" }, 409);
        }
        return json({ item: { id: `node-${sessionId}`, etag: "etag-1" }, unchanged: false });
      }
      if (method === "PATCH") return json({ id: "node", etag: "etag-2" });
      if (method === "GET" && /\/items\//.test(path)) return json({ id: "node", name, etag: "etag-2" });
      if (method === "GET" && /\/children$/.test(path)) {
        return json({ value: [{ id: "n1", name, etag: "etag-9" }], nextMarker: null });
      }
      return json({ code: "unexpected", message: url }, 500);
    }),
  );
  return wire;
}

function memoryStorage() {
  const store = new Map<string, string>();
  return {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
  };
}

function Harness({ entries }: { entries: DropEntry[] }) {
  const uploads = useUploads({
    driveId: "drive-1",
    doneLingerMs: 60_000,
    clientOptions: { storage: memoryStorage(), digest: async () => "deadbeef" },
  });
  return (
    <div>
      <button type="button" onClick={() => void uploads.onDrop(WRITABLE, transfer(entries))}>
        drop
      </button>
      <UploadTray
        rows={uploads.rows}
        skippedSidecars={uploads.skippedSidecars}
        identicalCopies={uploads.identicalCopies}
        alreadyInFiles={uploads.alreadyInFiles}
        finished={uploads.finished}
        refusal={uploads.refusal}
        batch={uploads.batch}
        conflicts={uploads.conflicts}
        resumable={uploads.resumable}
        onPause={uploads.pause}
        onResume={uploads.resume}
        onCancel={uploads.cancel}
        onAnswerConflict={uploads.answerConflict}
        onRetry={(id) => void uploads.retry(id)}
        onRetryFailed={() => void uploads.retryFailed()}
        onDismiss={uploads.dismiss}
      />
    </div>
  );
}

function mount(entries: DropEntry[]) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter>
        <Harness entries={entries} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

let wire: Wire;

beforeEach(() => {
  wire = stubFetch();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function drop(entries: DropEntry[]) {
  const user = userEvent.setup();
  mount(entries);
  await user.click(screen.getByRole("button", { name: "drop" }));
  return user;
}

/** The tray row for one file, found by the `title` that carries its whole name. */
function rowFor(name: string): HTMLElement {
  const label = screen.getByTitle(name);
  const row = label.closest("li");
  if (!row) throw new Error(`no tray row for ${name}`);
  return row;
}

describe("a batch with failures", () => {
  const SIX = ["a.csv", "b.csv", "c.csv", "d.csv", "e.csv", "f.csv"].map((n) => fileEntry(n));

  it("names every failure with the server's sentence and counts them in the summary", async () => {
    wire.script.set("b.csv", {
      openStatus: 500,
      openCode: "files.error",
      openMessage: "the store is not answering",
    });
    wire.script.set("e.csv", {
      partStatus: 422,
      partCode: "files.part_checksum_mismatch",
      partMessage: "part checksum mismatch: declared 0xaa, computed 0xbb",
    });

    await drop(SIX);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("4 of 6 uploaded, 2 failed"),
    );
    // Both losses are on the tray, by name, with the reason the server gave —
    // neither is folded into a count or dropped for the next file's row.
    expect(within(rowFor("b.csv")).getByText("the store is not answering")).toBeInTheDocument();
    expect(
      within(rowFor("e.csv")).getByText("part checksum mismatch: declared 0xaa, computed 0xbb"),
    ).toBeInTheDocument();
    expect(within(rowFor("b.csv")).getByText("Failed")).toBeInTheDocument();
    expect(within(rowFor("e.csv")).getByText("Failed")).toBeInTheDocument();
  });

  it("re-sends the two failures and nothing else", async () => {
    wire.script.set("b.csv", { openStatus: 500, openCode: "files.error", openMessage: "no" });
    wire.script.set("e.csv", {
      partStatus: 422,
      partCode: "files.part_checksum_mismatch",
      partMessage: "mismatch",
    });

    const user = await drop(SIX);
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("4 of 6 uploaded, 2 failed"),
    );

    const before = Object.fromEntries(
      ["a.csv", "b.csv", "c.csv", "d.csv", "e.csv", "f.csv"].map((n) => [n, wire.sends(n)]),
    );
    // The retry only has to reach the server for the two that failed; let them
    // through this time so the batch can settle clean.
    wire.script.clear();
    await user.click(screen.getByRole("button", { name: "Retry 2 failed" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("6 files uploaded"),
    );
    for (const landed of ["a.csv", "c.csv", "d.csv", "f.csv"]) {
      expect(wire.sends(landed)).toBe(before[landed]);
    }
    expect(wire.sends("b.csv")).toBeGreaterThan(before["b.csv"] ?? 0);
    expect(wire.sends("e.csv")).toBeGreaterThan(before["e.csv"] ?? 0);
    expect(screen.queryByText("Failed")).toBeNull();
  });
});

describe("one upload that failed", () => {
  it("offers a Retry that sends it again after the connection came back", async () => {
    wire.script.set("solo.csv", { offline: true });
    const user = await drop([fileEntry("solo.csv")]);

    await waitFor(() => expect(within(rowFor("solo.csv")).getByText("Failed")).toBeInTheDocument());
    const sentWhileOffline = wire.sends("solo.csv");

    wire.script.clear();
    await user.click(screen.getByRole("button", { name: "Retry solo.csv" }));

    await waitFor(() =>
      expect(within(rowFor("solo.csv")).getByText("Uploaded")).toBeInTheDocument(),
    );
    expect(wire.sends("solo.csv")).toBeGreaterThan(sentWhileOffline);
  });

  it("says why a 0-byte file was refused, in words about the file", async () => {
    // The upload routes refuse a part with no bytes, so an empty file is never
    // stored — and the server's own sentence is about a part, not the file.
    wire.script.set("empty.txt", {
      partStatus: 400,
      partCode: "files.empty_part",
      partMessage: "a part may not be empty",
    });
    await drop([fileEntry("empty.txt", "")]);

    await waitFor(() =>
      expect(within(rowFor("empty.txt")).getByText(/An empty file can't be uploaded\./)).toBeInTheDocument(),
    );
    expect(screen.queryByText("a part may not be empty")).toBeNull();
  });
});

describe("a name with whitespace at either end", () => {
  const REFUSED: Script = {
    openStatus: 422,
    openCode: "files.invalid_name.surrounding_space",
    openMessage: "a name may not start or end with a space",
  };
  /** The row for a name whose spaces ARE the point, matched exactly (the default
   *  matcher trims them away). */
  const rowExactly = (name: string): HTMLElement => {
    const label = screen.getByTitle(name, { normalizer: (text) => text });
    const row = label.closest("li");
    if (!row) throw new Error(`no tray row for ${JSON.stringify(name)}`);
    return row;
  };
  const opensFor = (name: string) =>
    wire.touched.filter((call) => call.method === "POST" && call.path.endsWith("/api/v1/files/uploads") && call.name === name)
      .length;

  it("offers the trimmed name, and the retry sends that name rather than the refused one", async () => {
    // Sending " report.csv " again is refused the same way every time; the row
    // offers the one name that can land and the retry carries it.
    wire.script.set(" report.csv ", REFUSED);
    const user = await drop([fileEntry(" report.csv ")]);

    const row = await waitFor(() => {
      const found = rowExactly(" report.csv ");
      expect(within(found).getByText("Failed")).toBeInTheDocument();
      return found;
    });
    expect(within(row).getByText("A name cannot start or end with a space.")).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /^Retry/ })).toBeNull();
    const offer = within(row).getByRole("button", { name: "Upload as \u201creport.csv\u201d" });
    expect(opensFor(" report.csv ")).toBe(1);

    await user.click(offer);

    await waitFor(() => expect(within(rowFor("report.csv")).getByText("Uploaded")).toBeInTheDocument());
    expect(opensFor("report.csv")).toBe(1);
    expect(opensFor(" report.csv ")).toBe(1);
    expect(screen.queryByText("Failed")).toBeNull();
  });

  it("a name that is only spaces stays refused with the reason, and offers no retry", async () => {
    wire.script.set("   ", REFUSED);
    await drop([fileEntry("   ")]);

    const row = await waitFor(() => {
      const found = rowExactly("   ");
      expect(within(found).getByText("Failed")).toBeInTheDocument();
      return found;
    });
    expect(
      within(row).getByText("A name cannot start or end with a space. Rename the file and upload it again."),
    ).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: /Retry|Upload as/ })).toBeNull();
  });

  it("Retry all sends the trimmed names and leaves a whitespace-only name alone", async () => {
    wire.script.set(" a.csv", REFUSED);
    wire.script.set("b.csv ", REFUSED);
    wire.script.set(" ", REFUSED);
    const user = await drop([fileEntry(" a.csv"), fileEntry("b.csv "), fileEntry(" ")]);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("0 of 3 uploaded, 3 failed"));

    // Two of the three can land, so the count offered is two.
    await user.click(screen.getByRole("button", { name: "Retry 2 failed" }));

    await waitFor(() => expect(within(rowFor("a.csv")).getByText("Uploaded")).toBeInTheDocument());
    await waitFor(() => expect(within(rowFor("b.csv")).getByText("Uploaded")).toBeInTheDocument());
    expect(opensFor(" ")).toBe(1);
    expect(within(rowExactly(" ")).getByText("Failed")).toBeInTheDocument();
  });
});

describe("a name the server refuses for what it is", () => {
  // Deterministic refusals: the same name is refused again however often it is
  // sent, so the row offers no Retry and says what to do in the product's own
  // sentence — never the server's lowercase rule.
  const CASES = [
    [
      "a name too long",
      `${"n".repeat(300)}.txt`,
      "files.invalid_name.too_long",
      "a name may not exceed 243 bytes",
      "That name is too long. Rename the file and upload it again.",
    ],
    [
      "a reserved name",
      "..",
      "files.invalid_name.dot",
      "'.' and '..' are reserved by the filesystem",
      "That name is reserved. Rename the file and upload it again.",
    ],
    [
      "a control character",
      "tab\there.txt",
      "files.invalid_name.control",
      "a name may not contain a control character",
      "That name contains a hidden character that cannot be saved. Rename the file and upload it again.",
    ],
  ] as const;

  const opensFor = (name: string) =>
    wire.touched.filter((call) => call.method === "POST" && call.path.endsWith("/api/v1/files/uploads") && call.name === name)
      .length;

  it.each(CASES)("%s offers no retry and says what to do", async (_label, name, code, message, sentence) => {
    wire.script.set(name, { openStatus: 422, openCode: code, openMessage: message });
    await drop([fileEntry(name)]);

    const row = await waitFor(() => {
      const found = screen.getAllByRole("listitem").find((item) => within(item).queryByText("Failed"));
      expect(found).toBeDefined();
      return found!;
    });
    expect(within(row).getByText(sentence)).toBeInTheDocument();
    expect(within(row).queryByText(message)).toBeNull();
    expect(within(row).queryByRole("button", { name: /Retry|Upload as/ })).toBeNull();
    expect(opensFor(name)).toBe(1);
  });

  it("Retry all counts and resends only the failures a retry can land", async () => {
    wire.script.set("..", { openStatus: 422, openCode: "files.invalid_name.dot", openMessage: "reserved" });
    wire.script.set(" a.csv", {
      openStatus: 422,
      openCode: "files.invalid_name.surrounding_space",
      openMessage: "a name may not start or end with a space",
    });
    wire.script.set(" b.csv", {
      openStatus: 422,
      openCode: "files.invalid_name.surrounding_space",
      openMessage: "a name may not start or end with a space",
    });
    const user = await drop([fileEntry(".."), fileEntry(" a.csv"), fileEntry(" b.csv")]);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("0 of 3 uploaded, 3 failed"));

    await user.click(screen.getByRole("button", { name: "Retry 2 failed" }));

    await waitFor(() => expect(within(rowFor("a.csv")).getByText("Uploaded")).toBeInTheDocument());
    await waitFor(() => expect(within(rowFor("b.csv")).getByText("Uploaded")).toBeInTheDocument());
    expect(opensFor("..")).toBe(1);
  });
});

describe("a name already taken", () => {
  it("asks, then says on the row what the answer did", async () => {
    wire.script.set("notes.txt", { collide: true });
    const user = await drop([fileEntry("notes.txt")]);

    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Name taken: notes.txt" })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "Keep both" }));

    await waitFor(() =>
      expect(
        within(rowFor("notes.txt")).getByText("Kept both. This one was saved under a new name."),
      ).toBeInTheDocument(),
    );
  });
});

describe("the name on a tray row", () => {
  it("carries the whole filename in its title", async () => {
    const long = `${"deeply-nested-quarterly-".repeat(3)}report.csv`;
    await drop([fileEntry(long)]);

    await waitFor(() => expect(screen.getByTitle(long)).toBeInTheDocument());
    // Elided for the row, never at the end: the tail is what tells two files apart.
    expect(screen.getByTitle(long)).toHaveTextContent(/report\.csv$/);
  });

  it.each([
    ["short.csv", "short.csv"],
    [
      "qa-bulk-047-with-a-very-long-descriptive-name.txt",
      "qa-bulk-047-with-a-ver…-descriptive-name.txt",
    ],
  ])("elides %s in the middle", (name, expected) => {
    expect(middleEllipsis(name)).toBe(expected);
  });

  it("keeps the extension when the name is one long word", () => {
    const elided = middleEllipsis("x".repeat(200) + ".parquet");
    expect(elided).toHaveLength(44);
    expect(elided.endsWith(".parquet")).toBe(true);
    expect(elided).toContain("…");
  });
});

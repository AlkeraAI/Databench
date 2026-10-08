import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { FS_NAME_MAX_BYTES, NAME_MAX_BYTES, PULL_PART_SUFFIX } from "@alkera/chat-model";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { keys } from "@/api/keys";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

// Every field that takes a name refuses what the filesystem — and the sync onto
// the workspace machine — would refuse, BEFORE the request leaves. The proof in
// each case is the same pair: the sentence is on screen, and no write was sent.

const DRIVE = "dr_1";

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: DRIVE,
    kind: "file",
    name: "report.csv",
    nameDisplay: "report.csv",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/report.csv",
    path: "/home/dana/report.csv",
    etag: "et_1",
    ctag: "ct_1",
    stale: false,
    locked: false,
    held: false,
    shared: false,
    trashed: false,
    capabilities: { can_read: true, can_write: true, can_share: true, can_delete: true },
    ...over,
  } as Item;
}

const HOME = item({ id: "nd_home", kind: "folder", name: "home", nameDisplay: "home" });
const SHARED = item({ id: "nd_shared", kind: "folder", name: "shared", nameDisplay: "shared" });

interface Wire {
  method: string;
  url: string;
}

function stubApi(wire: Wire[]): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? init?.method ?? "GET";
      if (method !== "GET") wire.push({ method, url });
      const answer = (body: unknown) =>
        new Response(JSON.stringify(body), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (url.includes("/leases")) return answer([]);
      if (url.includes("/permissions")) return answer({ value: [] });
      if (url.includes("/versions")) return answer({ value: [] });
      if (url.includes("/search")) return answer({ value: [], nextMarker: null });
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) {
        return answer({ value: [HOME, SHARED], nextMarker: null });
      }
      if (url.includes("/children")) return answer({ value: [item()], nextMarker: null });
      if (url.includes("/items/")) return answer(HOME);
      return answer({});
    }),
  );
}

function stubViewport(): void {
  vi.stubGlobal(
    "matchMedia",
    (query: string): MediaQueryList =>
      ({
        media: query,
        matches: false,
        onchange: null,
        addEventListener: () => undefined,
        removeEventListener: () => undefined,
        addListener: () => undefined,
        removeListener: () => undefined,
        dispatchEvent: () => false,
      }) as unknown as MediaQueryList,
  );
}

function mountPage() {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mountRename(subject: Item) {
  const client = createQueryClient({ retry: false });
  client.setQueryData(keys.files.item(subject.id), subject);
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RenameInline driveId={DRIVE} item={subject} onDone={vi.fn()} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return screen.getByRole("textbox", { name: "New name" });
}

let wire: Wire[] = [];

beforeEach(() => {
  wire = [];
  stubViewport();
  stubApi(wire);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Each refused name, with the sentence the field has to put on screen. */
const REFUSED: ReadonlyArray<readonly [string, string, string]> = [
  ["a separator", "q3/q4.csv", "A name can’t contain “/”."],
  ["two dots", "..", "“.” and “..” are reserved by the filesystem."],
  [
    "one byte past the ceiling",
    "n".repeat(NAME_MAX_BYTES + 1),
    `A name is limited to ${NAME_MAX_BYTES} bytes.`,
  ],
  ["a trailing space", "notes.csv ", "A name can’t start or end with a space."],
  ["a tab", "q3\tplans.csv", "A name can’t contain control characters."],
];

describe("renaming a file", () => {
  it.each(REFUSED)("refuses %s without sending the rename", async (_label, name, message) => {
    const field = mountRename(item());
    await userEvent.clear(field);
    await userEvent.type(field, name);
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(wire).toEqual([]);
    // The editor stays open holding what was typed: a refusal is not a close.
    expect(field).toHaveValue(name);
  });

  it("sends a legal name, so the rule is a gate and not a wall", async () => {
    const field = mountRename(item());
    await userEvent.clear(field);
    await userEvent.type(field, "q3-report.csv");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(wire).toHaveLength(1));
    expect(wire[0]?.method).toBe("PATCH");
  });

  it("stores a name only Windows objects to, and says so without refusing", async () => {
    // One drive is shared by machines that disagree about what a name may be.
    // Refusing "NUL.txt" here would cost a Linux user a file they can see, so
    // the rename goes through and the field says what will happen instead.
    const field = mountRename(item());
    await userEvent.clear(field);
    await userEvent.type(field, "NUL.txt");

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText(/Windows cannot hold this name/)).toBeInTheDocument();

    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(wire).toHaveLength(1));
    expect(wire[0]?.method).toBe("PATCH");
  });

  it("drops the note the moment the name is one the field refuses", async () => {
    // Two sentences under one field contradict: the refusal is what the person
    // has to act on, and a note about Windows beside it is noise.
    const field = mountRename(item());
    await userEvent.clear(field);
    await userEvent.type(field, "CON");
    expect(screen.getByText(/Windows cannot hold this name/)).toBeInTheDocument();

    await userEvent.type(field, "/x");
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("A name can’t contain “/”.");
    expect(screen.queryByText(/Windows cannot hold this name/)).toBeNull();
    expect(wire).toEqual([]);
  });

  it("clears the refusal once the name becomes legal", async () => {
    const field = mountRename(item());
    await userEvent.clear(field);
    await userEvent.type(field, "a/b");
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByRole("alert")).toBeInTheDocument();

    await userEvent.clear(field);
    await userEvent.type(field, "ab");
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
  });
});

describe("renaming a chat", () => {
  const CHAT = item({
    id: "nd_chat",
    kind: "folder",
    name: "Plan.alkerachat",
    nameDisplay: "Plan.alkerachat",
    object: { id: "ch_1", type: "chat", title: "Plan" },
  } as Partial<Item>);

  it("takes a title a filesystem would refuse — a title is not a filename", async () => {
    const field = mountRename(CHAT);
    await userEvent.clear(field);
    await userEvent.type(field, "q3/q4 plan");
    await userEvent.keyboard("{Enter}");

    await waitFor(() => expect(wire).toHaveLength(1));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("refuses a title past 200 characters without sending it", async () => {
    const field = mountRename(CHAT);
    await userEvent.clear(field);
    // Typed in one paste: 201 keystrokes through userEvent is needlessly slow.
    await userEvent.click(field);
    await userEvent.paste("t".repeat(201));
    await userEvent.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A title is limited to 200 characters.",
    );
    expect(wire).toEqual([]);
  });
});

describe("the New folder form", () => {
  it.each(REFUSED)("refuses %s without creating anything", async (_label, name, message) => {
    mountPage();
    await screen.findByText("report.csv");
    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, name);
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(wire.filter((sent) => sent.method === "POST")).toEqual([]);
    expect(screen.getByRole("form", { name: "New folder" })).toBeInTheDocument();
  });

  // The same rule, the same sentence, in the field where the name is actually
  // chosen. A create that said nothing about `CON` while the rename beside it
  // did made the two fields disagree about one rule.
  it.each([
    ["a reserved Windows stem", "CON"],
    ["a trailing dot", "plans."],
  ])("says what Windows will do with %s, and creates it anyway", async (_label, name) => {
    mountPage();
    await screen.findByText("report.csv");
    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, name);

    expect(screen.getByText(/Windows cannot hold this name/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(field).toHaveAccessibleDescription(/Windows cannot hold this name/);

    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(wire.some((sent) => sent.method === "POST")).toBe(true));
  });

  it("says nothing about Windows for a name every machine can hold", async () => {
    mountPage();
    await screen.findByText("report.csv");
    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, "Q3 plans");
    expect(screen.queryByText(/Windows cannot hold this name/)).toBeNull();
  });

  it("drops the note once the field refuses the name outright", async () => {
    mountPage();
    await screen.findByText("report.csv");
    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, "CON");
    expect(screen.getByText(/Windows cannot hold this name/)).toBeInTheDocument();

    await userEvent.type(field, "/x");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A name can’t contain “/”.");
    expect(screen.queryByText(/Windows cannot hold this name/)).toBeNull();
  });

  it("creates a folder whose name is legal", async () => {
    mountPage();
    await screen.findByText("report.csv");
    await userEvent.click(
      within(screen.getByRole("group", { name: "Create" })).getByRole("button", {
        name: "New folder",
      }),
    );
    const field = await screen.findByRole("textbox", { name: "Folder name" });
    await userEvent.type(field, "Q3 plans");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => {
      expect(wire.some((sent) => sent.method === "POST")).toBe(true);
    });
  });
});

describe("the byte ceiling the field enforces", () => {
  /** The library's naming module, found by walking up from wherever vitest started. */
  function namesModule(): string {
    const relative = join("packages", "api-core", "alkera_core", "files", "names.py");
    let dir = process.cwd();
    for (;;) {
      const candidate = join(dir, relative);
      if (existsSync(candidate)) return readFileSync(candidate, "utf8");
      const parent = dirname(dir);
      if (parent === dir) throw new Error(`could not find ${relative} above ${process.cwd()}`);
      dir = parent;
    }
  }

  it("is the one the server derives, read out of the library rather than copied", () => {
    // Two languages, one decision. A field that took 255 bytes would send a
    // name the drive refuses; a field that stopped at 243 after the server had
    // moved on would block a name it takes. Both halves derive the ceiling
    // from the sidecar, so this reads the server's two inputs and checks the
    // client is working from the same pair.
    const source = namesModule();
    const fsMax = /^FS_NAME_MAX_BYTES: Final = (\d+)$/m.exec(source);
    const suffix = /^PULL_PART_SUFFIX: Final = b"([^"]+)"$/m.exec(source);
    expect(fsMax?.[1]).toBeDefined();
    expect(suffix?.[1]).toBeDefined();
    expect(Number(fsMax?.[1])).toBe(FS_NAME_MAX_BYTES);
    expect(suffix?.[1]).toBe(PULL_PART_SUFFIX);
    expect(NAME_MAX_BYTES).toBe(Number(fsMax?.[1]) - String(suffix?.[1]).length);
  });
});

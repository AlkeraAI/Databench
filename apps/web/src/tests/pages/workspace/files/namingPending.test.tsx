/**
 * A name in flight holds its field.
 *
 * A create or a rename can take seconds under load, and a field left editable
 * lets a second Enter ask for the same folder again and be told it already
 * exists. While the write is
 * out the field is read-only and busy, a second Enter sends nothing, and a
 * refusal hands the field back holding what was typed, next to the reason.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** The one write under test is answered only when the test says so. */
interface Held {
  writes: string[];
  answer: (response: Response) => void;
}

function stubApi(): Held {
  let release: ((response: Response) => void) | null = null;
  const held: Held = {
    writes: [],
    answer: (response) => release?.(response),
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = input instanceof Request ? input : null;
      const url = request ? request.url : String(input);
      const method = request?.method ?? init?.method ?? "GET";
      if (method !== "GET") {
        held.writes.push(`${method} ${url}`);
        return new Promise<Response>((resolve) => {
          release = resolve;
        });
      }
      if (url.includes("/leases")) return json([]);
      if (url.includes("/permissions")) return json({ value: [] });
      if (url.includes("/versions")) return json({ value: [] });
      if (url.includes("/search")) return json({ value: [], nextMarker: null });
      if (url.includes("/trash")) return json({ entries: [], nextMarker: null });
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (url.includes("/items/nd_root/children")) return json({ value: [HOME], nextMarker: null });
      if (url.includes("/children")) return json({ value: [item()], nextMarker: null });
      if (url.includes("/items/")) return json(HOME);
      return json({});
    }),
  );
  return held;
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

let held: Held;

beforeEach(() => {
  stubViewport();
  held = stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

async function openNewFolder(): Promise<HTMLInputElement> {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={["/files/nd_home"]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const creates = await screen.findByRole("group", { name: "Create" });
  await userEvent.click(within(creates).getByRole("button", { name: "New folder" }));
  return (await screen.findByRole("textbox", { name: "Folder name" })) as HTMLInputElement;
}

describe("a new folder in flight", () => {
  it("holds the field and sends one create however often Enter is pressed", async () => {
    const field = await openNewFolder();
    await userEvent.type(field, "plans{Enter}");
    await waitFor(() => expect(held.writes).toHaveLength(1));

    const form = screen.getByRole("form", { name: "New folder" });
    expect(form).toHaveAttribute("aria-busy", "true");
    expect(field).toHaveAttribute("readonly");
    expect(within(form).getByRole("button", { name: "Creating…" })).toBeDisabled();

    await userEvent.type(field, "x{Enter}");
    await userEvent.click(within(form).getByRole("button", { name: "Creating…" }));
    expect(held.writes).toHaveLength(1);
    expect(field).toHaveValue("plans");

    held.answer(json(item({ id: "nd_plans", kind: "folder", name: "plans" }), 201));
    await waitFor(() => expect(screen.queryByRole("form", { name: "New folder" })).toBeNull());
  });

  it("hands the field back with the name and the reason when the create is refused", async () => {
    const field = await openNewFolder();
    await userEvent.type(field, "plans{Enter}");
    await waitFor(() => expect(held.writes).toHaveLength(1));

    held.answer(
      json(
        {
          code: "db_lock_timeout",
          message: "Another change to the same data is still in progress. Please retry shortly.",
        },
        503,
      ),
    );

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(field).not.toHaveAttribute("readonly");
    expect(field).toHaveValue("plans");
    expect(screen.getByRole("form", { name: "New folder" })).not.toHaveAttribute("aria-busy");
  });
});

describe("a rename in flight", () => {
  function mountRename(): HTMLInputElement {
    const subject = item();
    const client = createQueryClient({ retry: false });
    client.setQueryData(keys.files.item(subject.id), subject);
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          <RenameInline driveId={DRIVE} item={subject} onDone={vi.fn()} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    return screen.getByRole("textbox", { name: "New name" }) as HTMLInputElement;
  }

  it("is busy, keeps the keyboard, and sends one rename for two Enters", async () => {
    const field = mountRename();
    await userEvent.clear(field);
    await userEvent.type(field, "q3.csv{Enter}");
    await waitFor(() => expect(held.writes).toHaveLength(1));

    expect(field).toHaveAttribute("aria-busy", "true");
    expect(field).toHaveAttribute("readonly");
    expect(field).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(held.writes).toHaveLength(1);
  });

  it("says why when the server is too busy to rename, and keeps what was typed", async () => {
    const field = mountRename();
    await userEvent.clear(field);
    await userEvent.type(field, "q3.csv{Enter}");
    await waitFor(() => expect(held.writes).toHaveLength(1));

    held.answer(
      json(
        {
          code: "db_lock_timeout",
          message: "Another change to the same data is still in progress. Please retry shortly.",
        },
        503,
      ),
    );

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(field).toHaveValue("q3.csv");
    expect(field).not.toHaveAttribute("readonly");
    expect(field).toHaveFocus();
  });
});

/** The declarations of the one rule whose selector is exactly `selector`. */
function ruleBody(css: string, selector: string): string {
  const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const at = plain.split("}").find((chunk) => chunk.split("{")[0]?.trim() === selector);
  return at?.split("{")[1] ?? "";
}

describe("the Windows note on a new folder's name", () => {
  // jsdom lays nothing out, so the rule the layout rests on is read off the
  // sheet, and the markup is rendered to prove the note sits where it keys.
  it("hangs below the form instead of standing in the browser bar's row", async () => {
    const field = await openNewFolder();
    await userEvent.type(field, "c:d");

    const note = screen.getByText(/Windows cannot hold this name/);
    expect(screen.getByRole("form", { name: "New folder" })).toContainElement(note);
    expect(note).toHaveClass("alk-files-rename__note");

    const sheet = readFileSync(
      join(process.cwd(), "src/pages/workspace/files/treegrid.css"),
      "utf8",
    );
    const body = ruleBody(sheet, ".alk-files__new-folder .alk-files-rename__note");
    expect(body).toMatch(/position:\s*absolute/);
    expect(body).toMatch(/top:\s*calc\(100%/);
    expect(body).toMatch(/inline-size:\s*max-content/);
  });
});

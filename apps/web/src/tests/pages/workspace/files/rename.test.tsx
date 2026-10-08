import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { keys } from "@/api/keys";
import { createQueryClient } from "@/api/queryClient";
import type { Item } from "@/api/files";
import { RenameInline } from "@/pages/workspace/files/RenameInline";

// Rename driven through the real `useRenameItem`: the cache is the subject. The
// optimistic patch has to be visible in the cached row BEFORE the response lands,
// and a 412 has to put the old name back — so each test reads `getQueryData` at
// both moments rather than asserting that a mock was called.

const DRIVE = "drv_1";
const ITEM: Item = {
  id: "nd_a",
  ino: 7,
  driveId: DRIVE,
  kind: "file",
  name: "notes.txt",
  nameDisplay: "notes.txt",
  nameEncoding: "utf-8",
  pathBytes: "",
  parentId: "nd_parent",
  path: "/home/dana/notes.txt",
  etag: "e1",
  ctag: "c1",
  stale: false,
  locked: false,
  held: false,
  shared: false,
  trashed: false,
} as Item;

/** Resolved by the test when it wants the PATCH to answer. */
let release: ((value: Response) => void) | null = null;
let sentBody: unknown = null;

function stubPatch(): void {
  release = null;
  sentBody = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (input instanceof Request) sentBody = JSON.parse(await input.clone().text());
      else if (typeof init?.body === "string") sentBody = JSON.parse(init.body);
      return new Promise<Response>((resolve) => {
        release = resolve;
      });
    }),
  );
}

function answerOk(name: string): void {
  release?.(
    new Response(JSON.stringify({ ...ITEM, name, nameDisplay: name, etag: "e2" }), {
      status: 200,
      headers: { "content-type": "application/json" },
    }),
  );
}

function answerRefusal(status: number, code: string, message: string): void {
  release?.(
    new Response(JSON.stringify({ error: { code, message } }), {
      status,
      headers: { "content-type": "application/json" },
    }),
  );
}

function mount() {
  const client = createQueryClient({ retry: false });
  // The row as the browser had it cached before the rename started.
  client.setQueryData(keys.files.item(ITEM.id), ITEM);
  const done = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RenameInline driveId={DRIVE} item={ITEM} onDone={done} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { client, done };
}

function cachedName(client: ReturnType<typeof createQueryClient>): string | undefined {
  return client.getQueryData<Item>(keys.files.item(ITEM.id))?.nameDisplay;
}

async function typeName(next: string): Promise<void> {
  const user = userEvent.setup();
  const field = screen.getByRole("textbox", { name: "New name" });
  await user.clear(field);
  await user.type(field, `${next}{Enter}`);
}

beforeEach(stubPatch);
afterEach(() => vi.unstubAllGlobals());

describe("inline rename", () => {
  it("puts the new name in the cache before the server answers, and keeps it on success", async () => {
    const { client, done } = mount();
    await typeName("plan.txt");

    // The optimistic half: the request has not answered yet.
    await waitFor(() => expect(cachedName(client)).toBe("plan.txt"));
    expect(sentBody).toEqual({ name: "plan.txt" });

    answerOk("plan.txt");
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(cachedName(client)).toBe("plan.txt");
  });

  it("rolls the name back on a 412 and shows the API's reason", async () => {
    const { client, done } = mount();
    await typeName("plan.txt");
    await waitFor(() => expect(cachedName(client)).toBe("plan.txt"));

    answerRefusal(412, "files.stale_etag", "Someone else renamed this file.");

    await waitFor(() => expect(cachedName(client)).toBe("notes.txt"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Someone else renamed this file.");
    // The editor stays open holding what was typed, so the refusal can be answered.
    expect(screen.getByRole("textbox", { name: "New name" })).toHaveValue("plan.txt");
    expect(done).not.toHaveBeenCalled();
  });

  it("rolls back and quotes the reason for a taken name too, not only a 412", async () => {
    const { client } = mount();
    await typeName("plan.txt");
    await waitFor(() => expect(cachedName(client)).toBe("plan.txt"));

    answerRefusal(409, "files.name_conflict", "A file called plan.txt is already here.");

    await waitFor(() => expect(cachedName(client)).toBe("notes.txt"));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A file called plan.txt is already here.",
    );
  });

  it("Escape abandons the edit without touching the cache or the wire", async () => {
    const user = userEvent.setup();
    const { client, done } = mount();
    const field = screen.getByRole("textbox", { name: "New name" });
    await user.clear(field);
    await user.type(field, "plan.txt{Escape}");

    expect(done).toHaveBeenCalled();
    expect(cachedName(client)).toBe("notes.txt");
    expect(sentBody).toBeNull();
  });

  it("committing the unchanged name asks the server for nothing", async () => {
    const { client, done } = mount();
    await typeName("notes.txt");

    expect(done).toHaveBeenCalled();
    expect(sentBody).toBeNull();
    expect(cachedName(client)).toBe("notes.txt");
  });

  it("selects the stem and leaves the extension, so a rename does not retype '.txt'", () => {
    mount();
    const field = screen.getByRole("textbox", { name: "New name" }) as HTMLInputElement;
    expect(field.selectionStart).toBe(0);
    expect(field.selectionEnd).toBe("notes".length);
  });
});

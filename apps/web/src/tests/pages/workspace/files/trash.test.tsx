import { readFileSync } from "node:fs";
import { join } from "node:path";

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { TrashPage, restoredName, timeLeftLabel, trashLocation } from "@/pages/workspace/files/TrashPage";

// Every action is asserted by the request it produced — the route, the id in the path
// and the method — because the id is the whole contract: restore names a trash
// OPERATION, purge names a NODE, and getting them the wrong way round is exactly the
// bug a page test has to catch.

interface Call {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
}

function item(over: Partial<Item> = {}): Item {
  return {
    id: "nd_1",
    ino: 7,
    driveId: "dr_1",
    kind: "folder",
    name: "designs",
    nameDisplay: "designs",
    nameEncoding: "utf-8",
    pathBytes: "/home/dana/designs",
    path: "/home/dana/designs",
    etag: "et_1",
    ctag: "ct_1",
    ...over,
  } as Item;
}

function entry(over: Record<string, unknown> = {}) {
  return {
    trashOpId: "top_1",
    originalParentId: "nd_home",
    originalPath: "/home/dana",
    deletedAt: "2026-09-08T10:00:00Z",
    purgeAfter: "2026-10-08T10:00:00Z",
    timeLeftSeconds: 29 * 86_400,
    item: item(),
    ...over,
  };
}

/** The trash listing, plus whatever each mutation should answer with. */
function stubApi(options: { entries?: unknown[]; answers?: Record<string, unknown>; status?: number } = {}) {
  const calls: Call[] = [];
  const entries = options.entries ?? [entry()];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // openapi-fetch hands the runtime a `Request`, so the url, the method and the body
      // are read off it rather than off the init the caller never sees.
      const req = input instanceof Request ? input : new Request(String(input), init);
      const url = req.url;
      const method = req.method;
      const text = await req.clone().text();
      calls.push({
        url,
        method,
        body: text ? JSON.parse(text) : undefined,
        headers: Object.fromEntries(req.headers.entries()),
      });

      const json = (payload: unknown) =>
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      if (method === "GET" && url.includes("/trash")) return json({ entries, nextMarker: null });
      // The page reads the drive and its root once, for the version Empty trash names.
      if (method === "GET" && /\/files\/drives\/?(\?|$)/.test(url)) {
        return json({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      if (method === "GET" && url.includes("/items/nd_root")) {
        return json(item({ id: "nd_root", name: "", nameDisplay: "", etag: "et_root" }));
      }
      const status = options.status ?? 200;
      const key = Object.keys(options.answers ?? {}).find((fragment) => url.includes(fragment));
      const body = key ? (options.answers ?? {})[key] : {};
      return new Response(JSON.stringify(body), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return calls;
}

function renderTrash(props: Partial<React.ComponentProps<typeof TrashPage>> = {}) {
  const client = createQueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/files/trash"]}>
        <TrashPage driveId="dr_1" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("timeLeftLabel", () => {
  it.each([
    [29 * 86_400, "29 days left"],
    [86_400, "1 day left"],
    [86_399, "23 hours left"],
    [3_600, "1 hour left"],
    [3_599, "59 minutes left"],
    [60, "1 minute left"],
    [59, "less than a minute left"],
    [0, "purging now"],
    [-10, "purging now"],
  ])("%i seconds reads as %s", (seconds, expected) => {
    expect(timeLeftLabel(seconds)).toBe(expected);
  });
});

describe("trashLocation", () => {
  it.each([
    ["/home/Alkera Dev Admin/Chats", "Home › Alkera Dev Admin › Chats"],
    ["/shared/Q3", "Shared › Q3"],
    ["/teams/Data/reports", "Teams › Data › reports"],
    // A folder a person named "home" deeper down keeps its own spelling.
    ["/home/dana/home", "Home › dana › home"],
    ["/", "Home"],
  ])("reads %s as %s", (path, shown) => {
    expect(trashLocation(path)).toBe(shown);
  });
});

describe("restoredName", () => {
  it("reads the display name off the restored row, wrapped or bare", () => {
    expect(restoredName({ item: { nameDisplay: "designs (2)" } })).toBe("designs (2)");
    expect(restoredName({ nameDisplay: "designs (2)" })).toBe("designs (2)");
    expect(restoredName({ name: "designs" })).toBe("designs");
  });

  it("is null when the answer carries no row", () => {
    expect(restoredName(null)).toBeNull();
    expect(restoredName({})).toBeNull();
    expect(restoredName({ item: { nameDisplay: "" } })).toBeNull();
  });
});

/** The Files page's sheet, comments stripped so one inside a rule cannot be read
 *  as a declaration. */
const PAGE_CSS = readFileSync(
  join(process.cwd(), "src/pages/workspace/files/files-page.css"),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/** The grid tracks the rule for `selector` lays down, one entry per column. */
function trackList(selector: string): string[] {
  // The selector may share its rule with a sibling, so it is found in the selector
  // LIST rather than immediately before the brace.
  const at = PAGE_CSS.indexOf(selector);
  expect(at, `no rule for \`${selector}\``).toBeGreaterThan(-1);
  const body = PAGE_CSS.slice(PAGE_CSS.indexOf("{", at) + 1, PAGE_CSS.indexOf("}", at));
  const declared = /grid-template-columns:([^;]+);/.exec(body)?.[1];
  expect(declared, `\`${selector}\` lays down no columns`).toBeDefined();
  // Split on the spaces BETWEEN tracks, never on one inside a `minmax()`.
  const tracks: string[] = [];
  let depth = 0;
  let current = "";
  for (const character of declared!.replace(/\s+/g, " ").trim()) {
    if (character === "(") depth += 1;
    if (character === ")") depth -= 1;
    if (character === " " && depth === 0) {
      if (current !== "") tracks.push(current);
      current = "";
      continue;
    }
    current += character;
  }
  if (current !== "") tracks.push(current);
  return tracks;
}

describe("the trash view", () => {
  it("lists each trashed root with its original location and time left", async () => {
    stubApi();
    renderTrash();

    const row = await screen.findByRole("row", { name: /designs/ });
    // The folder it was deleted FROM, as the server renders it — not the trashed
    // node's own path, which says where it is now.
    expect(within(row).getByText("Home › dana")).toBeInTheDocument();
    expect(within(row).queryByText("Home › dana › designs")).not.toBeInTheDocument();
    expect(within(row).getByText("29 days left")).toBeInTheDocument();
  });

  it("gives the name and the original location room to be read", async () => {
    stubApi();
    renderTrash();

    // Both cells are in the row, and both are the row's own — the buttons beside
    // them are not what a person is being asked to identify.
    const row = await screen.findByRole("row", { name: /designs/ });
    const cells = within(row).getAllByRole("gridcell");
    expect(cells[0]).toHaveTextContent("designs");
    expect(cells[1]).toHaveTextContent("Home › dana");
    expect(within(row).getByRole("button", { name: "Delete designs forever" })).toBeTruthy();

    // jsdom lays nothing out, so the width they are given is read off the sheet that
    // gives it. Both were `minmax(0, …)`, which against an actions cell three buttons
    // wide resolved to zero: the name of the row "Delete forever" was about to destroy
    // was in the DOM and invisible on screen.
    const tracks = trackList(".alk-files--trash .alk-files__row");
    expect(tracks).toHaveLength(4);
    for (const track of tracks.slice(0, 2)) {
      const floor = /^minmax\(\s*([^,]+),/.exec(track)?.[1]?.trim();
      expect(floor, `\`${track}\` may collapse to nothing`).toBeDefined();
      expect(Number.parseFloat(floor!)).toBeGreaterThan(0);
    }
    // The header was never the cell that collapsed; it has to keep matching the rows.
    expect(trackList(".alk-files--trash .alk-files__grid-head")).toEqual(tracks);
  });

  it("keeps every action on the row inside a pane too narrow for them on one line", async () => {
    stubApi();
    renderTrash();
    const row = await screen.findByRole("row", { name: /designs/ });
    const actions = within(row).getByRole("button", { name: "Delete designs forever" }).parentElement!;
    expect(actions).toHaveClass("alk-files__cell-actions");

    // An `auto` actions track took three buttons' width whatever the pane had, and
    // at 1440 px Delete forever sat past the pane's edge. The track may give back
    // down to its widest button, and the buttons wrap inside the row to fit it.
    const actionsTrack = trackList(".alk-files--trash .alk-files__row")[3];
    expect(actionsTrack?.replace(/\s/g, "")).toBe("minmax(min-content,max-content)");
    const at = PAGE_CSS.indexOf(".alk-files--trash .alk-files__cell-actions");
    expect(at).toBeGreaterThan(-1);
    const body = PAGE_CSS.slice(PAGE_CSS.indexOf("{", at) + 1, PAGE_CSS.indexOf("}", at));
    expect(body).toMatch(/flex-wrap:\s*wrap/);
  });

  it("insets the head by the rows' own padding, so the sentence and Empty trash line up with the cells", () => {
    // The page's head is flush (padding-inline 0) to line up with the rail, and the
    // trash has no rail: the sentence sat on the pane's left edge and Empty trash's
    // focus ring was cut off at the right one.
    const ruleBody = (selector: string): string => {
      const at = PAGE_CSS.indexOf(`${selector} {`);
      expect(at, `no rule for \`${selector}\``).toBeGreaterThan(-1);
      return PAGE_CSS.slice(PAGE_CSS.indexOf("{", at) + 1, PAGE_CSS.indexOf("}", at));
    };
    const rowInline = /padding:\s*[^\s;]+\s+([^;]+);/.exec(ruleBody("\n.alk-files__row"))?.[1]?.trim();
    expect(rowInline).toBe("var(--alkSpace4)");
    const headInline = /padding-inline:\s*([^;]+);/.exec(ruleBody(".alk-files--trash .alk-files__head"))?.[1]?.trim();
    expect(headInline).toBe(rowInline);
  });

  it("names a trashed workspace by its title, never by the folder the drive minted for it", async () => {
    stubApi({
      entries: [
        entry({
          originalPath: "/home/Alkera Dev Admin/Chats",
          item: item({
            name: "Doomed.alkeraworkspace",
            nameDisplay: "Doomed.alkeraworkspace",
            object: { id: "ws_1", type: "workspace", title: "Doomed" },
          } as Partial<Item>),
        }),
      ],
    });
    renderTrash();

    const row = await screen.findByRole("row", { name: /Doomed/ });
    expect(within(row).getByText("Doomed")).toBeInTheDocument();
    expect(within(row).queryByText(/alkeraworkspace/)).not.toBeInTheDocument();
    expect(within(row).getByText("Home › Alkera Dev Admin › Chats")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Restore Doomed" })).toBeInTheDocument();
  });

  it("says when the original folder is gone instead of printing Unknown", async () => {
    stubApi({ entries: [entry({ originalPath: null, originalParentId: null })] });
    renderTrash();
    const row = await screen.findByRole("row", { name: /designs/ });
    expect(within(row).getByText("Original folder no longer exists")).toBeInTheDocument();
    expect(within(row).queryByText("Unknown")).not.toBeInTheDocument();
  });

  it("a restore names the trashed row's version, so the server does not refuse it", async () => {
    // Every Files mutation must carry If-Match; without it the server answers 428,
    // which the page would show as "someone changed this".
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Restore designs" }));
    await waitFor(() => expect(calls.some((call) => call.url.includes("/restore"))).toBe(true));
    const restore = calls.find((call) => call.url.includes("/restore"))!;
    expect(restore.headers["if-match"]).toBe("et_1");
    expect(restore.headers["idempotency-key"]).toBeTruthy();
  });

  it("emptying names the drive root's version", async () => {
    const calls = stubApi();
    renderTrash();
    const button = await screen.findByRole("button", { name: "Empty trash" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await userEvent.click(within(await screen.findByRole("dialog", { name: "Empty the trash?" })).getByRole("button", { name: "Empty trash" }));
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/trash/empty") && call.method === "POST")).toBe(true),
    );
    const empty = calls.find((call) => call.url.includes("/trash/empty") && call.method === "POST")!;
    expect(empty.headers["if-match"]).toBe("et_root");
  });

  it("says the trash is empty only when the sweep left nothing", async () => {
    stubApi({ answers: { "/trash/empty": { removed: 2, skipped: 0, skippedReasons: [] } } });
    renderTrash();
    const button = await screen.findByRole("button", { name: "Empty trash" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await userEvent.click(
      within(await screen.findByRole("dialog", { name: "Empty the trash?" })).getByRole("button", { name: "Empty trash" }),
    );

    expect(await screen.findByText("The trash is empty.")).toBeInTheDocument();
  });

  it("names what the sweep could not delete instead of claiming the trash is empty", async () => {
    // The route decides per deletion: what the caller may not delete is still
    // here afterwards, and a notice that says otherwise is the only place a
    // person would have learned it.
    stubApi({
      answers: {
        "/trash/empty": { removed: 3, skipped: 2, skippedReasons: ["files.forbidden"] },
      },
    });
    renderTrash();
    const button = await screen.findByRole("button", { name: "Empty trash" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await userEvent.click(
      within(await screen.findByRole("dialog", { name: "Empty the trash?" })).getByRole("button", { name: "Empty trash" }),
    );

    expect(
      await screen.findByText(
        "Removed 3. 2 items stay in the trash because you cannot delete them.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("The trash is empty.")).not.toBeInTheDocument();
  });

  it("counts one left-behind root in the singular", async () => {
    stubApi({
      answers: { "/trash/empty": { removed: 0, skipped: 1, skippedReasons: ["files.forbidden"] } },
    });
    renderTrash();
    const button = await screen.findByRole("button", { name: "Empty trash" });
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);
    await userEvent.click(
      within(await screen.findByRole("dialog", { name: "Empty the trash?" })).getByRole("button", { name: "Empty trash" }),
    );

    expect(
      await screen.findByText("Removed 0. 1 item stays in the trash because you cannot delete it."),
    ).toBeInTheDocument();
  });

  it("shows a skeleton while the listing is in flight, never a spinner", async () => {
    stubApi();
    renderTrash();
    expect(screen.getByTestId("trash-skeleton")).toBeInTheDocument();
    await screen.findByRole("row", { name: /designs/ });
    expect(screen.queryByTestId("trash-skeleton")).not.toBeInTheDocument();
  });

  it("says what to do when nothing has been deleted", async () => {
    stubApi({ entries: [] });
    renderTrash();
    expect(await screen.findByText(/Nothing is in the trash/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Empty trash" })).toBeDisabled();
  });

  it("restores through the trash OPERATION id, at its original place", async () => {
    const calls = stubApi({ answers: { restore: { item: item() } } });
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Restore designs" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/trash/top_1/restore"))).toBe(true),
    );
    const restore = calls.find((call) => call.url.includes("/restore"));
    expect(restore?.method).toBe("POST");
    expect(restore?.body).toEqual({ parent_id: null });
    expect(await screen.findByText("Restored designs.")).toBeInTheDocument();
  });

  it("reports the renamed result when the original name is occupied", async () => {
    stubApi({ answers: { restore: { item: item({ nameDisplay: "designs (2)" }) } } });
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Restore designs" }));

    expect(await screen.findByText("Restored as designs (2) because that name was taken.")).toBeInTheDocument();
  });

  it("restores into the folder the picker chose", async () => {
    const calls = stubApi({ answers: { restore: { item: item() } } });
    renderTrash({ pickFolder: async () => "nd_other" });
    await userEvent.click(
      await screen.findByRole("button", { name: "Restore designs to another folder" }),
    );

    await waitFor(() => expect(calls.some((call) => call.url.includes("/restore"))).toBe(true));
    expect(calls.find((call) => call.url.includes("/restore"))?.body).toEqual({
      parent_id: "nd_other",
    });
  });

  it("does not restore when the folder picker is cancelled", async () => {
    const calls = stubApi();
    renderTrash({ pickFolder: async () => undefined });
    await userEvent.click(
      await screen.findByRole("button", { name: "Restore designs to another folder" }),
    );

    expect(calls.some((call) => call.url.includes("/restore"))).toBe(false);
  });

  it("asks before deleting forever, and purges the NODE with permanent=true", async () => {
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Delete designs forever" }));

    expect(calls.some((call) => call.method === "DELETE")).toBe(false);

    await userEvent.click(
      await screen.findByRole("button", { name: "Delete forever" }),
    );
    await waitFor(() => expect(calls.some((call) => call.method === "DELETE")).toBe(true));

    const purge = calls.find((call) => call.method === "DELETE");
    expect(purge?.url).toContain("/items/nd_1");
    expect(purge?.url).toContain("permanent=true");
  });

  it("keeps the item when the confirmation is declined", async () => {
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Delete designs forever" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep it" }));

    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: "Delete designs forever" })).toBeInTheDocument();
  });

  // Both prompts are the product's one confirmation surface, so they carry its guarantees —
  // a named modal dialog, and Escape as a way out that writes nothing.
  it("names the row in the delete-forever dialog, and Escape keeps the item", async () => {
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Delete designs forever" }));

    const dialog = await screen.findByRole("dialog", { name: "Delete designs forever?" });
    expect(dialog).toHaveTextContent(/cannot be brought back/i);

    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.some((call) => call.method === "DELETE")).toBe(false);
    expect(screen.getByRole("button", { name: "Delete designs forever" })).toBeInTheDocument();
  });

  it("names the sweep in the empty-trash dialog, and Escape keeps the trash", async () => {
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Empty trash" }));

    await screen.findByRole("dialog", { name: "Empty the trash?" });
    await userEvent.keyboard("{Escape}");

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(calls.some((call) => call.url.includes("/trash/empty"))).toBe(false);
  });

  it("empties the trash only after the confirmation", async () => {
    const calls = stubApi();
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Empty trash" }));

    expect(calls.some((call) => call.url.includes("/trash/empty"))).toBe(false);

    const dialog = await screen.findByRole("dialog", { name: "Empty the trash?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Empty trash" }));

    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("/trash/empty") && call.method === "POST")).toBe(
        true,
      ),
    );
  });

  it("renders the API's reason inline when a restore is refused", async () => {
    stubApi({
      status: 409,
      answers: { restore: { error: { code: "files.leased", message: "leased" } } },
    });
    renderTrash({ errorContext: { holder: "Dana Okoye", machine: "MacBook Pro" } });
    await userEvent.click(await screen.findByRole("button", { name: "Restore designs" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Dana Okoye has this folder for local use.");
    expect(alert).toHaveTextContent("MacBook Pro");
  });

  // A box registers under its allocation id and holds its own lease, so the raw
  // facet names the machine and the holder with the same uuid.
  const BOX = "6b1f0b1e-0000-4000-8000-00000000000b";

  it.each([
    [
      "the reader's own chat",
      { yours: "chat", chat_id: "c1", chat_title: "Kickoff", holder: BOX, machine: BOX },
      "Your chat Kickoff is using this folder.",
    ],
    [
      "the reader's own box",
      { yours: "box", holder: BOX, machine: BOX, machine_name: "demo-box" },
      "Your box demo-box is using this folder.",
    ],
    ["the reader themselves", { yours: "you", holder: "u1", machine: "MacBook Pro" }, "You have this folder for local use."],
  ])("names %s as the holder when Delete forever is refused", async (_who, lease, title) => {
    stubApi({
      status: 409,
      entries: [entry({ item: item({ lease } as unknown as Partial<Item>) })],
      answers: { "/items/nd_1": { code: "files.leased", message: "leased" } },
    });
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Delete designs forever" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete forever" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(title);
    expect(alert).not.toHaveTextContent(/someone/i);
    expect(alert).not.toHaveTextContent(BOX);
  });

  it("names a colleague's box by its chat, never by its id, when the refusal is theirs", async () => {
    stubApi({
      status: 409,
      entries: [
        entry({
          item: item({
            lease: { yours: "none", chat_id: "c2", chat_title: "Q3 review", holder: BOX, machine: BOX },
          } as unknown as Partial<Item>),
        }),
      ],
      answers: { "/items/nd_1": { code: "files.leased", message: "leased" } },
    });
    renderTrash();
    await userEvent.click(await screen.findByRole("button", { name: "Delete designs forever" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete forever" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The chat Q3 review has this folder for local use.");
    expect(alert).not.toHaveTextContent(/your/i);
    expect(alert).not.toHaveTextContent(BOX);
  });

  it("is a treegrid whose rows carry a roving tabindex the arrow keys move", async () => {
    stubApi({
      entries: [entry(), entry({ trashOpId: "top_2", item: item({ id: "nd_2", name: "notes", nameDisplay: "notes" }) })],
    });
    renderTrash();

    const grid = await screen.findByRole("treegrid", { name: "Trash" });
    const rows = within(grid).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveAttribute("tabindex", "0");
    expect(rows[1]).toHaveAttribute("tabindex", "-1");

    rows[0].focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(rows[1]).toHaveFocus();
    expect(rows[1]).toHaveAttribute("tabindex", "0");
    expect(rows[0]).toHaveAttribute("tabindex", "-1");

    await userEvent.keyboard("{ArrowUp}");
    expect(rows[0]).toHaveFocus();
  });
});

describe("a trash bigger than one page", () => {
  /** The server's own paging: 50 entries per page, newest deletion first, the
   *  marker naming where the next page starts. */
  function stubPagedTrash(total: number, pageSize = 50) {
    const all = Array.from({ length: total }, (_, index) =>
      entry({
        trashOpId: `top_${index}`,
        // index 0 is the newest, as the route now answers.
        deletedAt: new Date(Date.UTC(2026, 8, 20, 0, 0, total - index)).toISOString(),
        item: item({ id: `nd_${index}`, name: `doomed-${index}`, nameDisplay: `doomed-${index}` }),
      }),
    );
    const calls: Call[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const req = input instanceof Request ? input : new Request(String(input), init);
        const url = new URL(req.url);
        const text = await req.clone().text();
        calls.push({
          url: req.url,
          method: req.method,
          body: text ? JSON.parse(text) : undefined,
          headers: Object.fromEntries(req.headers.entries()),
        });
        const json = (payload: unknown) =>
          new Response(JSON.stringify(payload), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        if (req.method === "GET" && url.pathname.endsWith("/trash")) {
          const marker = url.searchParams.get("marker");
          const from = marker === null ? 0 : all.findIndex((row) => row.trashOpId === marker) + 1;
          const slice = all.slice(from, from + pageSize);
          const last = slice.at(-1);
          const more = from + pageSize < all.length;
          return json({ entries: slice, nextMarker: more && last ? last.trashOpId : null });
        }
        if (req.method === "GET" && /\/files\/drives\/?(\?|$)/.test(req.url)) {
          return json({ id: "dr_1", orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
        }
        if (req.method === "GET" && req.url.includes("/items/nd_root")) {
          return json(item({ id: "nd_root", name: "", nameDisplay: "", etag: "et_root" }));
        }
        return json({});
      }),
    );
    return calls;
  }

  it("opens on the newest deletion and reaches the ones past the first page", async () => {
    stubPagedTrash(60);
    renderTrash();

    // The first screen is what was deleted most recently — the item a person has
    // just thrown away is the one they come here for.
    const first = await screen.findByRole("row", { name: /doomed-0/ });
    expect(within(first).getByRole("button", { name: "Restore doomed-0" })).toBeTruthy();

    // Everything past the server's 50-entry page is reachable without a click:
    // the pager walks the markers the way the folder listing does.
    expect(await screen.findByRole("row", { name: /doomed-50/ })).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("row", { name: /doomed-59/ })).toBeInTheDocument(),
    );
    // And it stops: an exhausted listing offers nothing more to load.
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("restores a row that only the second page holds", async () => {
    const calls = stubPagedTrash(60);
    renderTrash();
    const row = await screen.findByRole("row", { name: /doomed-50/ });

    await userEvent.click(within(row).getByRole("button", { name: "Restore doomed-50" }));

    await waitFor(() =>
      expect(
        calls.some(
          (call) => call.method === "POST" && call.url.includes("/trash/top_50/restore"),
        ),
      ).toBe(true),
    );
  });
});

import { QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { elideScopeName, SearchBar, SearchResults } from "@/pages/workspace/files/SearchBar";
import {
  EMPTY_FILTER_STATE,
  type FilterState,
  type SearchScope,
} from "@/pages/workspace/files/filterState";
import {
  SEARCH_FOLDER_SCOPE_PREFIX,
  SEARCH_SCOPE_PARAM,
  SEARCH_TEXT_PARAM,
  enclosingPath,
  isReadable,
  searchPath,
  useSearchQuery,
} from "@/pages/workspace/files/useSearchQuery";

// The search field driven through its real hook: `fetch` is stubbed at the wire, so the
// debounce is asserted by how many requests were made, the cancellation by the abort the
// stub observed, and the scope by the query string the field actually asked for.

const DRIVE = "drv_1";
const FOLDER = "nd_folder";
/** A window no gap between two keystrokes can cross, however loaded the machine
 *  running them, and the default here for exactly that reason. A short window is
 *  a burst only on a fast laptop: a runner that takes longer than the window
 *  between two characters splits one typed word into a settled text per
 *  character — a request per character, and a render with no rows at all between
 *  each pair, because every settled text is its own query key and a fresh key
 *  has no data yet. Both faces of that split have been flaky here. A burst opens
 *  this window, types, then shortens it to watch the one request go out. */
const PATIENT_DEBOUNCE = 60_000;

function item(overrides: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    subtype: null,
    nameDisplay: overrides.name,
    nameEncoding: "utf-8",
    nameFlags: { windows_safe: true, macos_safe: true, display_warning: false },
    pathBytes: "",
    parentId: FOLDER,
    path: null,
    capabilities: {},
    ...overrides,
  } as unknown as Item;
}

interface Recorded {
  url: string;
  aborted: boolean;
}

/** The scope the field asked for, read off the wire the way the server reads it. */
function scopeParam(url: string): string | null {
  return new URL(url, "http://localhost").searchParams.get(SEARCH_SCOPE_PARAM);
}

let requests: Recorded[] = [];
/** Resolve every in-flight response at once, so a test decides when a request lands. */
let release: (() => void) | null = null;

function stubSearch(rows: Item[], options: { hold?: boolean } = {}): void {
  requests = [];
  release = null;
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const record: Recorded = { url: String(input), aborted: false };
      requests.push(record);
      const body = {
        ok: true,
        status: 200,
        json: async () => ({ value: rows, nextMarker: null }),
      } as unknown as Response;
      return new Promise<Response>((resolve, reject) => {
        const finish = () => resolve(body);
        if (!options.hold) {
          finish();
          return;
        }
        release = finish;
        init?.signal?.addEventListener("abort", () => {
          record.aborted = true;
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    }),
  );
}

/** Where an opened result went, reset per test. */
const onOpen = vi.fn();

/** The field and its results, wired the way the page wires them: the scope the
 *  toggle is on is the scope the hook reads. */
function Harness({
  initial,
  rows,
  debounceMs = PATIENT_DEBOUNCE,
  folderName = "Reports",
}: {
  initial?: Partial<FilterState>;
  rows: Item[];
  debounceMs?: number;
  /** `null` is "there is no folder to narrow to" — `undefined` would be the
   *  default this signature supplies. */
  folderName?: string | null;
}) {
  const [state, setState] = useState<FilterState>({ ...EMPTY_FILTER_STATE, ...initial });
  const result = useSearchQuery({
    driveId: DRIVE,
    folderId: FOLDER,
    scope: state.scope,
    text: state.text,
    debounceMs,
  });
  void rows;
  return (
    <>
      <SearchBar
        state={state}
        onChange={setState}
        folderName={folderName ?? undefined}
        platform="mac"
      />
      <SearchResults result={result} onOpen={onOpen} />
    </>
  );
}

function field(): HTMLElement {
  return screen.getByRole("searchbox", { name: /search/i });
}

/** The scope dropdown's closed trigger. */
function scopeTrigger(): HTMLElement {
  return screen.getByRole("button", { name: "Search scope" });
}

/** Open the scope dropdown and choose the named option, the way a hand does. */
async function pickScope(user: UserEvent, option: string): Promise<void> {
  await user.click(scopeTrigger());
  await user.click(await screen.findByRole("option", { name: option }));
}

function renderSearch(
  rows: Item[],
  initial?: Partial<FilterState>,
  debounceMs: number = PATIENT_DEBOUNCE,
  /** The folder the narrow option names. `null` means there is none to narrow to. */
  folderName: string | null = "Reports",
) {
  const client = createQueryClient();
  const tree = (ms: number | undefined) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <Harness rows={rows} initial={initial} debounceMs={ms} folderName={folderName} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(tree(debounceMs));
  const reprop = (ms: number) => view.rerender(tree(ms));
  return {
    ...view,
    reprop,
    /** Type `text` as one burst: the window is held open across the keystrokes,
     *  however slowly the runner delivers them, and closed once at the end. The
     *  settled text is then the whole word and nothing else — one query key, one
     *  request, and no row-less render in the middle for an assertion to fall
     *  into. */
    async burst(user: UserEvent, text: string) {
      reprop(PATIENT_DEBOUNCE);
      await user.type(field(), text);
      reprop(0);
    },
  };
}

const RESULTS: Item[] = [
  item({ id: "nd_a", name: "budget.csv", path: "/home/robin/Reports/budget.csv" }),
  item({ id: "nd_b", name: "budget.csv", path: "/home/robin/Archive/2024/budget.csv" }),
];

beforeEach(() => {
  vi.useRealTimers();
  onOpen.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("search", () => {
  it("debounces a burst of keystrokes into one request", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await user.type(field(), "budget");

    // Six characters in and the window still open: nothing has been asked for,
    // which is the half a count taken after the fact cannot tell from a burst
    // that was simply typed faster than the wire.
    expect(requests).toEqual([]);

    // Closing the window is how the end of the wait is watched without sitting
    // through it: one request, for the settled text, not one per keystroke.
    view.reprop(0);
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].url).toContain("q=budget");
  });

  it("aborts the request in flight when the text changes again", async () => {
    stubSearch(RESULTS, { hold: true });
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "bu");
    await waitFor(() => expect(requests).toHaveLength(1));

    await view.burst(user, "dget");
    await waitFor(() => expect(requests).toHaveLength(2));
    // The first request is cancelled on the wire, so a slow answer to "bu"
    // cannot land after the answer to "budget".
    await waitFor(() => expect(requests[0].aborted).toBe(true));
    expect(requests[1].aborted).toBe(false);
    act(() => release?.());
  });

  it("does not search at all below the minimum length", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    // The window is closed on the single character, so a search for "b" has
    // nothing left to wait for: the next character is what proves it never went
    // out, rather than a sleep long enough to hope it would have.
    await view.burst(user, "b");
    await user.type(field(), "u");

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].url).toContain("q=bu");
  });

  it("shows each result with the folder that contains it, and no per-row button", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "budget");

    // Two files of the same name: only the path tells them apart. Both are read
    // in the same tick — a row that is on screen only until the next settled
    // text lands must not be able to satisfy the first read and miss the second.
    await waitFor(() => {
      expect(screen.getByText("/home/robin/Reports")).toBeInTheDocument();
      expect(screen.getByText("/home/robin/Archive/2024")).toBeInTheDocument();
    });
    // No per-result "Open enclosing folder" button; a row opens on double-click or Enter.
    expect(screen.queryByRole("button", { name: /enclosing/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: /actions/i })).not.toBeInTheDocument();
  });

  it.each([
    ["a double-click", async (row: HTMLElement) => userEvent.setup().dblClick(row)],
    [
      "Enter on the focused row",
      async (row: HTMLElement) => {
        row.focus();
        await userEvent.setup().keyboard("{Enter}");
      },
    ],
  ])("%s opens a result", async (_how, open) => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "budget");
    // Wait on the rows themselves, then read them fresh: a handle taken before
    // the results settled would be detached by the time it is clicked.
    const resultRows = () =>
      screen.getAllByRole("row").filter((row) => row.getAttribute("aria-level") === "1");
    await waitFor(() => expect(resultRows()).toHaveLength(RESULTS.length));

    await open(resultRows()[1]);

    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ id: "nd_b" }));
  });

  it("names itself Search and offers the two scopes in one dropdown, and no submit button", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    renderSearch(RESULTS);

    expect(screen.getByRole("searchbox", { name: "Search" })).toHaveAttribute(
      "placeholder",
      "Search",
    );
    // A Search button submitted a form with nothing to submit — the results are
    // already on screen by the time a hand reaches it. The only control beside
    // the field is the scope dropdown, which says which scope it is on.
    const trigger = scopeTrigger();
    expect(screen.getAllByRole("button")).toEqual([trigger]);
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger).toHaveTextContent("Everywhere");

    // Both scopes live behind it, and exactly one of them is on.
    await user.click(trigger);
    expect(screen.getAllByRole("option").map((option) => option.textContent?.trim())).toEqual([
      "Reports",
      "Everywhere",
    ]);
    expect(screen.getByRole("option", { name: "Everywhere" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("option", { name: "Reports" })).toHaveAttribute(
      "aria-selected",
      "false",
    );
  });

  it.each([
    ["Reports", "folder" as SearchScope, `${SEARCH_FOLDER_SCOPE_PREFIX}${FOLDER}`],
    // The whole drive is the ABSENCE of the anchor, which is how the server reads it.
    ["Everywhere", "drive" as SearchScope, null],
  ])("choosing %s searches that scope on the wire", async (option, _scope, expected) => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    // Opened on the OTHER scope, so the pick is a real change: choosing the
    // option a control is already on commits nothing and asks for nothing.
    const opened: SearchScope = expected === null ? "folder" : "drive";
    const view = renderSearch(RESULTS, { scope: opened });

    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));

    await pickScope(user, option);

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(scopeParam(requests[1].url)).toBe(expected);
    expect(requests[1].url).toContain("q=budget");
    // And the closed control says which scope the results are from.
    expect(scopeTrigger()).toHaveTextContent(option);
  });

  it("narrows to the folder on screen and back, on the wire", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));
    // Opens on the whole drive, which the server reads as the ABSENCE of the anchor.
    expect(scopeParam(requests[0].url)).toBeNull();

    await pickScope(user, "Reports");
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(scopeParam(requests[1].url)).toBe(`${SEARCH_FOLDER_SCOPE_PREFIX}${FOLDER}`);

    // Back to the drive, and typed on so the wire is asked again: the answer to
    // the narrowed text is cached under its own key, so a request is only proof
    // of the scope when the key is new.
    await pickScope(user, "Everywhere");
    await view.burst(user, "s");
    await waitFor(() => expect(requests).toHaveLength(3));
    expect(requests[2].url).toContain("q=budgets");
    expect(scopeParam(requests[2].url)).toBeNull();
  });

  it("opens the dropdown and picks a scope from the keyboard alone", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));

    scopeTrigger().focus();
    // Down opens the list and walks it; Enter takes the row it is on. A control
    // reachable only by pointer would be unreachable for half the readers here.
    await user.keyboard("{ArrowDown}");
    expect(scopeTrigger()).toHaveAttribute("aria-expanded", "true");
    await user.keyboard("{ArrowUp}{Enter}");

    await waitFor(() => expect(requests).toHaveLength(2));
    expect(scopeParam(requests[1].url)).toBe(`${SEARCH_FOLDER_SCOPE_PREFIX}${FOLDER}`);
    expect(scopeTrigger()).toHaveAttribute("aria-expanded", "false");
    expect(scopeTrigger()).toHaveTextContent("Reports");
  });

  it("cuts a folder name too long for the control, and keeps the whole of it on hover", () => {
    // A chat's working directory is named after its first message, so this is an
    // ordinary name here, not a pathological one.
    const chat = "Hi, can you check the quarterly numbers and tell me what stands out";
    stubSearch(RESULTS);
    renderSearch(RESULTS, { scope: "folder" }, PATIENT_DEBOUNCE, chat);

    const trigger = scopeTrigger();
    // The control shows a cut name — the untouched one would set the width of
    // the whole toolbar row.
    expect(trigger).toHaveTextContent(elideScopeName(chat));
    expect(trigger.textContent).not.toContain("stands out");
    expect(trigger.textContent?.endsWith("…")).toBe(true);
    // Nothing that was cut is unreachable: the whole name is a hover away.
    expect(trigger.closest("[title]")).toHaveAttribute("title", chat);
  });

  it("opens narrowed when the link it was opened from carries the folder scope", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS, { scope: "folder" });

    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(scopeParam(requests[0].url)).toBe(`${SEARCH_FOLDER_SCOPE_PREFIX}${FOLDER}`);
  });

  it("offers no scope dropdown where there is no folder to narrow to", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS, undefined, PATIENT_DEBOUNCE, null);

    expect(screen.queryByRole("button", { name: "Search scope" })).toBeNull();
    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(scopeParam(requests[0].url)).toBeNull();
  });

  it("answers Enter with the results already on screen, and never reloads the page", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    const view = renderSearch(RESULTS);

    await view.burst(user, "budget");
    await waitFor(() => expect(requests).toHaveLength(1));

    // The form has no submit button left, so Enter in the field is the implicit
    // submit — which must be swallowed: a navigation here would drop the results.
    const form = field().closest("form")!;
    const submits: Event[] = [];
    form.addEventListener("submit", (event) => submits.push(event));
    await user.type(field(), "{Enter}");

    expect(submits).toHaveLength(1);
    // React's handler runs on the root container, above the form, so the verdict is
    // read after the dispatch rather than inside the listener.
    expect(submits[0]!.defaultPrevented).toBe(true);
    expect(field()).toHaveValue("budget");
    // A submit is not a second request: the results already answer the text. The
    // next request is made on purpose rather than waited out — had Enter asked for
    // anything, it would be sitting between these two.
    await user.type(field(), "s");
    await waitFor(() => expect(requests).toHaveLength(2));
    expect(requests[1].url).toContain("q=budgets");
  });

  it("never renders an item the API says the caller cannot read", async () => {
    const forbidden = item({
      id: "nd_secret",
      name: "payroll.csv",
      path: "/home/other/payroll.csv",
      capabilities: { can_read: false } as unknown as Item["capabilities"],
    });
    stubSearch([RESULTS[0], forbidden]);
    const user = userEvent.setup();
    const view = renderSearch([]);

    await view.burst(user, "budget");

    // The stub returned it; the API is the authority on what may be seen, and it
    // said no. Read in the same tick as the row that did arrive, so "not on
    // screen" can never be answered by a render that has no rows at all yet.
    await waitFor(() => {
      expect(screen.getByText("/home/robin/Reports")).toBeInTheDocument();
      expect(screen.queryByText("payroll.csv")).not.toBeInTheDocument();
      expect(screen.queryByText("/home/other")).not.toBeInTheDocument();
    });
  });

  it.each([
    ["Cmd+F", "{Meta>}f{/Meta}"],
    ["Cmd+Shift+F", "{Meta>}{Shift>}f{/Shift}{/Meta}"],
  ])("%s focuses the field", async (_name, keys) => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    renderSearch(RESULTS);

    await user.keyboard(keys);

    expect(field()).toHaveFocus();
  });

  it("Escape clears the field", async () => {
    stubSearch(RESULTS);
    const user = userEvent.setup();
    renderSearch(RESULTS);

    await user.type(field(), "budget");
    expect(field()).toHaveValue("budget");

    await user.type(field(), "{Escape}");
    expect(field()).toHaveValue("");
  });
});

describe("search helpers", () => {
  it.each([
    ["a name that fits is untouched", "Reports", 8, "Reports"],
    ["a name exactly as long as the ceiling is untouched", "Reports", 7, "Reports"],
    ["one character past it gives way", "Reports!", 7, "Report…"],
    ["the trailing space before the cut goes with it", "Report card", 8, "Report…"],
  ])("%s", (_case, name, max, expected) => {
    expect(elideScopeName(name, max)).toBe(expected);
    // Whatever it was handed, the result is something the control can hold.
    expect(elideScopeName(name, max).length).toBeLessThanOrEqual(max);
  });

  it("drops only an explicit refusal, never an item that says nothing", () => {
    expect(isReadable(item({ id: "a", name: "a" }))).toBe(true);
    expect(
      isReadable(
        item({
          id: "b",
          name: "b",
          capabilities: { can_read: true } as unknown as Item["capabilities"],
        }),
      ),
    ).toBe(true);
    expect(
      isReadable(
        item({
          id: "c",
          name: "c",
          capabilities: { can_read: false } as unknown as Item["capabilities"],
        }),
      ),
    ).toBe(false);
  });

  it("reads the enclosing folder off the path the server sends, and the root as /", () => {
    // The server fills `pathBytes` on every row and leaves `path` null; reading only
    // `path` put every hit "in /".
    expect(
      enclosingPath({ ...item({ id: "p", name: "p" }), path: null, pathBytes: "/home/me/old/p.txt" } as Item),
    ).toBe("/home/me/old");
    expect(enclosingPath(item({ id: "a", name: "a", path: "/home/me/a.txt" }))).toBe("/home/me");
    expect(enclosingPath(item({ id: "b", name: "b", path: "/b.txt" }))).toBe("/");
    expect(enclosingPath(item({ id: "c", name: "c" }))).toBe("/");
  });

  // The route is in the generated document now, so the path the client builds and the `q`
  // key are read off the SDK's own operation rather than assumed.
  it("asks on the path and with the text key the generated document declares", async () => {
    const fs = await import("node:fs");
    const nodePath = await import("node:path");
    const { fileURLToPath } = await import("node:url");

    const here = nodePath.dirname(fileURLToPath(import.meta.url));
    const document = JSON.parse(
      fs.readFileSync(
        nodePath.resolve(here, "../../../../../../..", "packages/shared-openapi/openapi.json"),
        "utf8",
      ),
    ) as {
      paths: Record<string, { get?: { parameters?: { name: string; in: string }[] } }>;
    };

    const [template, operation] =
      Object.entries(document.paths).find(([route]) =>
        /^\/api\/v1\/files\/drives\/\{[^}]+\}\/search$/.test(route),
      ) ?? [];
    expect(template).toBeTruthy();

    const driveParam = /\{([^}]+)\}/.exec(template ?? "")?.[1];
    expect(searchPath(DRIVE)).toBe(template?.replace(`{${driveParam}}`, DRIVE));

    const declared = (operation?.get?.parameters ?? [])
      .filter((parameter) => parameter.in === "query")
      .map((parameter) => parameter.name);
    expect(declared).toContain(SEARCH_TEXT_PARAM);
  });

  // `scope` is read off `request.query_params` in the handler, so FastAPI never declares it
  // and the document cannot carry it. Until the route takes it as a real `Query(...)`, the
  // handler's own code (never its prose) is the contract for the scope grammar.
  it("spells the scope the way the server's handler parses it", async () => {
    const fs = await import("node:fs");
    const nodePath = await import("node:path");
    const { fileURLToPath } = await import("node:url");

    const here = nodePath.dirname(fileURLToPath(import.meta.url));
    const feeds = nodePath.resolve(
      here,
      "../../../../../../..",
      "apps/backend/backend/api/routes/files/feeds.py",
    );
    // Prose can say anything, so docstrings and comments are removed before the grammar is
    // read off the handler: only executable code is the contract.
    const code = fs
      .readFileSync(feeds, "utf8")
      .replace(/"""[\s\S]*?"""/g, "")
      .replace(/'''[\s\S]*?'''/g, "")
      .replace(/#.*$/gm, "");
    const search = code.slice(code.indexOf("async def search("));
    const handler = search.slice(0, search.indexOf("@router.get"));

    const key = /scope_raw = request\.query_params\.get\("([^"]+)"\)/.exec(handler);
    const separator = /partition\("([^"]+)"\)/.exec(handler);
    const folderWord = /head != "([^"]+)"/.exec(handler);
    expect(key?.[1]).toBeTruthy();
    expect(separator?.[1]).toBeTruthy();
    expect(folderWord?.[1]).toBeTruthy();

    expect(SEARCH_SCOPE_PARAM).toBe(key?.[1]);
    expect(SEARCH_FOLDER_SCOPE_PREFIX).toBe(`${folderWord?.[1]}${separator?.[1]}`);
  });
});

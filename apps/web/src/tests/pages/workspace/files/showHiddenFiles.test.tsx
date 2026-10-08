import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearStorageMirror } from "@alkera/ui/storage";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilesBrowser } from "@/pages/workspace/files/FilesBrowser";
import { FilesScreen } from "@/pages/workspace/files/FilesPage";
import { chatRecordsRule, SHOW_HIDDEN_STORAGE_KEY } from "@/pages/workspace/files/hiddenEntries";

// A notebook's saved outputs live beside it in `__marimo__/`, and dot files
// ride along with any tree a person syncs. Both views of a folder keep them
// out of sight unless the viewer turns on "Show hidden files", and a link
// into one still lists it.

const DRIVE = "dr_1";

function item(over: Partial<Item> & { id: string; name: string }): Item {
  return {
    ino: 1,
    driveId: DRIVE,
    kind: "file",
    nameDisplay: over.name,
    nameEncoding: "utf-8",
    pathBytes: "",
    parentId: "nd_home",
    path: null,
    etag: "e1",
    ctag: "c1",
    object: null,
    lease: null,
    stale: false,
    locked: false,
    held: false,
    capabilities: { can_read: true, can_write: true },
    shared: false,
    trashed: false,
    ...over,
  } as unknown as Item;
}

const HOME = item({ id: "nd_home", name: "home", kind: "folder", parentId: "nd_root" });
const NOTEBOOK = item({ id: "nd_nb", name: "analysis.py" });
const MARIMO = item({ id: "nd_marimo", name: "__marimo__", kind: "folder" });
const GITIGNORE = item({ id: "nd_gitignore", name: ".gitignore" });
const PLAIN_MARIMO = item({ id: "nd_plain", name: "marimo", kind: "folder" });
const HOME_ROWS = [NOTEBOOK, MARIMO, GITIGNORE, PLAIN_MARIMO];

const SESSION = item({ id: "nd_session", name: "session", kind: "folder", parentId: "nd_marimo" });
const MARIMO_IGNORE = item({ id: "nd_marimo_ignore", name: ".gitignore", parentId: "nd_marimo" });
const SESSION_JSON = item({ id: "nd_session_json", name: "analysis.py.json", parentId: "nd_session" });
const SESSION_DOT = item({ id: "nd_session_dot", name: ".lock", parentId: "nd_session" });

const CHILDREN: Record<string, Item[]> = {
  nd_root: [HOME],
  nd_home: HOME_ROWS,
  nd_marimo: [SESSION, MARIMO_IGNORE],
  nd_session: [SESSION_JSON, SESSION_DOT],
};
const ITEMS: Record<string, Item> = Object.fromEntries(
  [HOME, NOTEBOOK, MARIMO, GITIGNORE, PLAIN_MARIMO, SESSION, MARIMO_IGNORE, SESSION_JSON].map(
    (row) => [row.id, row],
  ),
);

function answer(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function stubApi(children: Record<string, Item[]> = CHILDREN): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (/\/files\/drives\/?(\?|$)/.test(url)) {
        return answer({ id: DRIVE, orgId: "or_1", rootId: "nd_root", quotaBytes: 0 });
      }
      const listing = /\/items\/([^/?]+)\/children/.exec(url);
      if (listing) {
        return answer({ value: children[decodeURIComponent(listing[1] ?? "")] ?? [], nextMarker: null });
      }
      const one = /\/items\/([^/?]+)(?:\?|$)/.exec(url);
      if (one) {
        const found = ITEMS[decodeURIComponent(one[1] ?? "")];
        return found ? answer(found) : answer({ code: "files.not_found", message: "no" }, 404);
      }
      if (url.includes("/trash")) return answer({ entries: [], nextMarker: null });
      if (url.includes("/permissions")) return answer({ value: [] });
      return answer({ value: [], nextMarker: null });
    }),
  );
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

function paintedNames(): string[] {
  return Array.from(document.querySelectorAll("[data-row-id]")).map(
    (element) => element.querySelector(".alk-files-grid__name")?.textContent ?? "",
  );
}

function dimmedIds(): string[] {
  return Array.from(document.querySelectorAll('[data-hidden-entry="true"]')).map(
    (element) => element.getAttribute("data-row-id") ?? "",
  );
}

const toggle = () => screen.getByRole("button", { name: "Show hidden files" });

function mountBrowser(props: Partial<React.ComponentProps<typeof FilesBrowser>> = {}) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter>
        <FilesBrowser driveId={DRIVE} parentId="nd_home" platform="other" {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function mountPage(at: string) {
  return render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[at]}>
        <Routes>
          <Route path="/files/:nodeId" element={<FilesScreen />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  clearStorageMirror();
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  clearStorageMirror();
});

describe("the Files page", () => {
  it("hides dot files and __marimo__ by default, and shows them dimmed with the toggle", async () => {
    const user = userEvent.setup();
    mountPage("/files/nd_home");
    await waitFor(() => expect(paintedNames()).toEqual(["analysis.py", "marimo"]));
    expect(toggle()).toHaveAttribute("aria-pressed", "false");

    await user.click(toggle());

    await waitFor(() =>
      expect(paintedNames()).toEqual(["analysis.py", "__marimo__", ".gitignore", "marimo"]),
    );
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
    expect(dimmedIds().sort()).toEqual(["nd_gitignore", "nd_marimo"]);
  });

  it("counts the rows it shows, not the ones it keeps out of sight", async () => {
    const user = userEvent.setup();
    mountPage("/files/nd_home");
    await waitFor(() => expect(paintedNames()).toEqual(["analysis.py", "marimo"]));
    expect(await screen.findByText("2 of 4 shown · 2 hidden")).toBeInTheDocument();

    await user.click(toggle());

    await waitFor(() => expect(paintedNames()).toHaveLength(4));
    expect(await screen.findByText("4 of 4 shown")).toBeInTheDocument();
  });

  it("lists __marimo__ when a link opens it, its own dot files still hidden", async () => {
    mountPage("/files/nd_marimo");
    await waitFor(() => expect(paintedNames()).toEqual(["session"]));
  });

  it("hides a dot file two folders down", async () => {
    mountPage("/files/nd_session");
    await waitFor(() => expect(paintedNames()).toEqual(["analysis.py.json"]));
  });
});

describe("the preference", () => {
  it("is remembered for the next listing", async () => {
    const user = userEvent.setup();
    const first = mountBrowser();
    await waitFor(() => expect(paintedNames()).toHaveLength(2));
    await user.click(toggle());
    expect(window.localStorage.getItem(SHOW_HIDDEN_STORAGE_KEY)).toBe("true");
    first.unmount();

    mountBrowser();
    await waitFor(() => expect(paintedNames()).toHaveLength(4));
    expect(toggle()).toHaveAttribute("aria-pressed", "true");

    await user.click(toggle());
    await waitFor(() => expect(paintedNames()).toEqual(["analysis.py", "marimo"]));
    expect(window.localStorage.getItem(SHOW_HIDDEN_STORAGE_KEY)).toBe("false");
  });

  it("still works when storage refuses every read and write", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const user = userEvent.setup();
    mountBrowser();
    await waitFor(() => expect(paintedNames()).toEqual(["analysis.py", "marimo"]));

    await user.click(toggle());

    await waitFor(() => expect(paintedNames()).toHaveLength(4));
    expect(toggle()).toHaveAttribute("aria-pressed", "true");
  });

  it("does not reveal a chat folder's box records", async () => {
    const chat = item({
      id: "nd_chat",
      name: "c.alkerachat",
      kind: "folder",
      object: {
        type: "chat",
        id: "cht_1",
        title: "Warehouse spike",
        web_url: "/chat/cht_1",
        metadata: { files_node_id: "nd_work" },
      } as Item["object"],
    });
    stubApi({
      nd_chat: [
        item({ id: "nd_work", name: "scratch", kind: "folder", parentId: "nd_chat" }),
        item({ id: "nd_transcript", name: "chat.jsonl", parentId: "nd_chat" }),
        item({ id: "nd_runtime", name: ".runtime", kind: "folder", parentId: "nd_chat" }),
      ],
    });
    const user = userEvent.setup();
    mountBrowser({ parentId: "nd_chat", omit: chatRecordsRule(chat) });
    await waitFor(() => expect(paintedNames()).toEqual(["scratch"]));

    await user.click(toggle());

    expect(toggle()).toHaveAttribute("aria-pressed", "true");
    expect(paintedNames()).toEqual(["scratch"]);
  });
});

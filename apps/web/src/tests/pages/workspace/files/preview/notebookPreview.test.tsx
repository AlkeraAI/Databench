// @vitest-environment jsdom
//
// A notebook previews as a notebook: one renderer in the shared preview
// registry, chosen through the file-type owner, drawn read-only. The Files
// page modal and the page a shared link lands on (the same modal with nowhere
// to open the folder) both draw it, and neither may open a kernel, a live
// channel or a co-editing document: the preview reads the notebook once,
// through its stored route.

import { planPreview } from "@alkera/ui";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Item } from "@/api/files";
import { createQueryClient } from "@/api/queryClient";
import { FilePreviewModal } from "@/pages/workspace/files/preview/FilePreviewModal";
import "@/pages/workspace/files/preview/usePreviewContent";

// The renderer is a lazy chunk (the notebook editor's whole module graph).
// Loading it once up front keeps each case's wait about drawing the notebook,
// not about how long a busy machine takes to import that graph the first time.
beforeAll(async () => {
  await import("@/pages/workspace/files/preview/notebook/NotebookPreview");
}, 60_000);

const DRIVE = "d1";
const ITEM = "22222222-2222-4222-8222-222222222222";
const GRANT_URL = "http://files.localhost:8000/c/bm90ZWJvb2s.Y2xhaW0.c2ln";
const TABLE_MIME = "application/vnd.alkera.table+json";

const SOURCE = [
  "import marimo",
  "app = marimo.App()",
  "",
  '@app.cell(alkera_id="bbbbbbbbbb")',
  "def _():",
  "    frame = load()",
  "    frame",
  "    return",
  "",
].join("\n");

function cell(id: string, kind: string, source: string, outputs: unknown[] = []) {
  return { id, name: "_", kind, index: 0, status: "not_run", source, outputs, config: {}, meta: {}, extra: {}, defs: [], refs: [], graph_errors: [] };
}

const WITH_TABLE = {
  cells: [
    cell("aaaaaaaaaa", "markdown", "# Quarterly sales"),
    cell("bbbbbbbbbb", "python", "frame = load()\nframe", [
      {
        output_id: "bbbbbbbbbb/0",
        type: "display",
        data: {
          [TABLE_MIME]: { schema: { fields: [{ name: "region", type: "string" }] }, data: [{ region: "west" }], total_rows: 1 },
          "text/plain": "<table>",
        },
      },
    ]),
    cell("cccccccccc", "sql", "SELECT region FROM sales"),
  ],
  notices: [],
};

const WITHOUT_OUTPUTS = { cells: [cell("bbbbbbbbbb", "python", "frame = load()\nframe")], notices: [] };

function item(name: string): Item {
  return {
    id: ITEM,
    driveId: DRIVE,
    kind: "file",
    name,
    nameDisplay: name,
    etag: "etag-1",
    trashed: false,
    stale: false,
    file: { mime_type: "text/x-python", size: SOURCE.length, content_hash: "sha256-test", scan_state: "clean" },
    attrs: { mtime: "2026-09-01T10:00:00Z" },
    capabilities: { can_download: true },
  } as Item;
}

/** Every request the page made: the grant and the bytes the preview buys,
 *  and the notebook's stored route. Sockets are recorded too. */
function stubNetwork(stored: unknown = WITH_TABLE) {
  const urls: string[] = [];
  const sockets: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
      urls.push(url);
      if (url.includes("/content-grants")) {
        return Response.json({ url: GRANT_URL, expiresAt: new Date(Date.now() + 300_000).toISOString(), kind: "file", etag: "etag-1" });
      }
      if (url.endsWith(`/api/v1/notebooks/${DRIVE}/${ITEM}/stored`)) return Response.json(stored);
      if (url === GRANT_URL) return new Response(SOURCE, { status: 200, headers: { "content-type": "text/x-python" } });
      return new Response("unexpected", { status: 500 });
    }),
  );
  vi.stubGlobal(
    "WebSocket",
    class {
      constructor(url: string) {
        sockets.push(url);
      }
    },
  );
  return { urls, sockets };
}

function show(node: ReactNode) {
  return render(
    <MemoryRouter>
      <QueryClientProvider client={createQueryClient()}>{node}</QueryClientProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const SURFACES = [
  { surface: "the Files page modal", inFiles: true },
  { surface: "the shared-link page", inFiles: false },
] as const;

describe("a notebook in the file preview", () => {
  it.each(SURFACES)("draws cells, rendered Markdown and a saved table on $surface", async ({ inFiles }) => {
    stubNetwork();
    show(
      <FilePreviewModal
        driveId={DRIVE}
        item={item("new-notebook.alknb.py")}
        open
        onClose={() => {}}
        {...(inFiles ? { onOpenInFiles: () => {} } : {})}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Quarterly sales" }, { timeout: 5000 })).toBeInTheDocument();
    const reader = screen.getByTestId("notebook-reader");
    const cells = within(reader).getAllByRole("listitem");
    expect(cells).toHaveLength(3);
    expect(cells[1]!.textContent).toContain("frame = load()");
    expect(cells[2]!.textContent).toContain("SELECT region FROM sales");
    expect(within(cells[1]!).getByRole("cell", { name: "west" })).toBeInTheDocument();
    expect(document.querySelector("[data-renderer]")?.getAttribute("data-renderer")).toBe("notebook");
  });

  it("offers nothing that edits, runs or changes the notebook", async () => {
    stubNetwork();
    const { container } = show(<FilePreviewModal driveId={DRIVE} item={item("new-notebook.alknb.py")} open onClose={() => {}} />);
    await screen.findByTestId("notebook-reader", {}, { timeout: 5000 });

    expect(screen.queryByRole("button", { name: /run/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Cell actions" })).toBeNull();
    expect(screen.queryByRole("button", { name: /kernel|restart|interrupt/i })).toBeNull();
    expect(screen.queryByRole("toolbar")).toBeNull();
    const editors = container.ownerDocument.querySelectorAll(".cm-content");
    expect(editors.length).toBeGreaterThan(0);
    editors.forEach((editor) => expect(editor.getAttribute("contenteditable")).toBe("false"));
  });

  it("shows the raw source on the Source switch", async () => {
    stubNetwork();
    show(<FilePreviewModal driveId={DRIVE} item={item("new-notebook.alknb.py")} open onClose={() => {}} />);
    await screen.findByTestId("notebook-reader", {}, { timeout: 5000 });

    await userEvent.click(screen.getByRole("tab", { name: "Source" }));
    await waitFor(() => expect(screen.queryByTestId("notebook-reader")).toBeNull());
    expect(document.body.textContent).toContain('@app.cell(alkera_id="bbbbbbbbbb")');
  });

  it("says quietly when the notebook has no saved outputs", async () => {
    stubNetwork(WITHOUT_OUTPUTS);
    show(<FilePreviewModal driveId={DRIVE} item={item("new-notebook.alknb.py")} open onClose={() => {}} />);
    expect(await screen.findByText("No saved outputs", {}, { timeout: 5000 })).toBeInTheDocument();
  });

  it("opens no kernel, socket or live document: the stored route is its one notebook read", async () => {
    const net = stubNetwork();
    show(<FilePreviewModal driveId={DRIVE} item={item("new-notebook.alknb.py")} open onClose={() => {}} />);
    await screen.findByTestId("notebook-reader", {}, { timeout: 5000 });
    await userEvent.click(screen.getByRole("tab", { name: "Source" }));
    await userEvent.click(screen.getByRole("tab", { name: "Notebook" }));
    await screen.findByTestId("notebook-reader");

    const notebookReads = net.urls.filter((url) => url.includes("/api/v1/notebooks/"));
    expect(notebookReads.length).toBeGreaterThan(0);
    notebookReads.forEach((url) => expect(url.endsWith(`/api/v1/notebooks/${DRIVE}/${ITEM}/stored`)).toBe(true));
    expect(net.urls.some((url) => /realtime|crdt|\/kernel|\/run\b|\/frames|\/ops\b|\/events/.test(url))).toBe(false);
    expect(net.sockets).toEqual([]);
  });

  it("previews a plain Python file as its source, with no notebook read", async () => {
    const net = stubNetwork();
    show(<FilePreviewModal driveId={DRIVE} item={item("analysis.py")} open onClose={() => {}} />);
    await waitFor(() => expect(document.querySelector("[data-renderer]")?.getAttribute("data-renderer")).toBe("code"));
    await waitFor(() => expect(document.body.textContent).toContain("frame = load()"));
    expect(screen.queryByTestId("notebook-reader")).toBeNull();
    expect(screen.queryByRole("tab", { name: "Source" })).toBeNull();
    expect(net.urls.some((url) => url.includes("/api/v1/notebooks/"))).toBe(false);
  });
});

describe("the preview registry", () => {
  it.each([
    { name: "new-notebook.alknb.py", renderer: "notebook" },
    { name: "NEW-NOTEBOOK.ALKNB.PY", renderer: "notebook" },
    { name: "analysis.py", renderer: "code" },
    { name: ".alknb.py", renderer: "code" },
  ])("plans $name for the $renderer renderer", ({ name, renderer }) => {
    expect(planPreview({ name, mime: "text/x-python", size: 10 }).renderer.id).toBe(renderer);
  });
});

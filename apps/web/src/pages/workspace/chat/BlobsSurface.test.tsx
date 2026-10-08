import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BlobsSurface } from "./BlobsSurface";

// Stub the data layer so BlobsSurface renders without the real DaemonDataSource
// (which would need a host). `vi.hoisted` so the mocks exist before the hoisted
// `vi.mock` factory runs; `vi.mock` itself is hoisted above the import above.
const mocks = vi.hoisted(() => ({
  getChatTurns: vi.fn(),
  fetchBlob: vi.fn(),
  runCommand: vi.fn(),
  exportBlob: vi.fn(),
  kind: "vscode" as "vscode" | "browser",
}));
vi.mock("./data", () => ({
  chatHost: () => ({ kind: mocks.kind, runCommand: mocks.runCommand }),

  chatData: () => ({ getChatTurns: mocks.getChatTurns, fetchBlob: mocks.fetchBlob }),
  exportBlob: mocks.exportBlob,
  EXPORT_TRUNCATED_NOTICE:
    "The result was too large to save in full, so this file holds the first part of it.",
}));

function Where() {
  const location = useLocation();
  return <span data-testid="at">{`${location.pathname}${location.search}`}</span>;
}

function renderRoute(path = "/editor/blobs/chat-1") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/editor/blobs/:chatId" element={<BlobsSurface />} />
          <Route path="/chat/:chatId/results" element={<BlobsSurface />} />
          <Route path="/chat/:chatId/result/:handle" element={<p>A result</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mocks.exportBlob.mockResolvedValue({ truncated: false });
  mocks.fetchBlob.mockResolvedValue({
    kind: "rows",
    columns: ["id"],
    rows: [[1]],
    text: "",
    offset: 0,
    limit: 50,
    total: 1,
    returned: 1,
    hasMore: false,
    nextOffset: null,
  });
});

afterEach(() => {
  vi.clearAllMocks();
  mocks.kind = "vscode";
});

describe("BlobsSurface", () => {
  it("lists the chat's distinct blobs (deduped) as cards", async () => {
    // A result referenced twice (h1) must appear once; both h1 and h2 surface.
    mocks.getChatTurns.mockResolvedValue([
      { id: "t1", parts: [{ kind: "tool", references: [{ handle: "h1", name: "Revenue" }] }] },
      {
        id: "t2",
        parts: [
          { kind: "tool", references: [{ handle: "h2", name: "Failed logins" }, { handle: "h1", name: "Revenue" }] },
        ],
      },
    ]);
    renderRoute();
    await waitFor(() => expect(screen.getByText("Revenue")).toBeInTheDocument());
    expect(screen.getByText("Failed logins")).toBeInTheDocument();
    expect(screen.getAllByText("Revenue")).toHaveLength(1); // deduped
  });

  it("shows an empty state when the chat produced no results", async () => {
    mocks.getChatTurns.mockResolvedValue([{ id: "t1", parts: [{ kind: "text" }] }]);
    renderRoute();
    await waitFor(() => expect(screen.getByText("No results yet")).toBeInTheDocument());
    expect(screen.getByText(/collect here to open or download/i)).toBeInTheDocument();
  });

  it("opens a result in its own editor tab when its chip is activated", async () => {
    mocks.getChatTurns.mockResolvedValue([
      { id: "t1", parts: [{ kind: "tool", references: [{ handle: "h1", name: "Revenue", refType: "rows" }] }] },
    ]);
    renderRoute();
    await waitFor(() => expect(screen.getByText("Revenue")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Open Revenue in the editor/i }));
    expect(mocks.runCommand).toHaveBeenCalledWith({
      command: "alkera.openBlob",
      args: { chatId: "chat-1", handle: "h1", name: "Revenue", refType: "rows", mime: undefined },
    });
  });

  // A browser has no editor tabs to own, and the host command it would dispatch
  // is a no-op there — so the same surface stacks on this page instead, with the
  // descriptor in the query so it names itself before the first fetch.
  it("stacks the result as a page in a browser, where there is no tab to own", async () => {
    mocks.kind = "browser";
    mocks.getChatTurns.mockResolvedValue([
      { id: "t1", parts: [{ kind: "tool", references: [{ handle: "h1", name: "Revenue", refType: "rows" }] }] },
    ]);
    renderRoute("/chat/chat-1/results");
    await waitFor(() => expect(screen.getByText("Revenue")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Open Revenue in the editor/i }));
    await waitFor(() => expect(screen.getByText("A result")).toBeInTheDocument());
    const at = screen.getByTestId("at").textContent ?? "";
    expect(at.startsWith("/chat/chat-1/result/h1?")).toBe(true);
    expect(new URLSearchParams(at.split("?")[1]).get("name")).toBe("Revenue");
    expect(mocks.runCommand).not.toHaveBeenCalled();
  });

  // A 500k-row table saves as a prefix of itself. The reader who asked for it
  // opens that file in a spreadsheet and reads it as the whole answer unless
  // the surface that offered the download says otherwise — and this list is a
  // download affordance in its own right, not a shortcut to the one that does.
  it("says so when a downloaded result stopped short of the whole thing", async () => {
    mocks.exportBlob.mockResolvedValue({ truncated: true });
    mocks.getChatTurns.mockResolvedValue([
      { id: "t1", parts: [{ kind: "tool", references: [{ handle: "h1", name: "Revenue", refType: "rows" }] }] },
    ]);
    renderRoute();
    await waitFor(() => expect(screen.getByText("Revenue")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^Download Revenue$/i }));

    await waitFor(() => expect(screen.getByText(/holds the first part of it/i)).toBeInTheDocument());
    expect(mocks.exportBlob).toHaveBeenCalledWith(expect.objectContaining({ handle: "h1" }));
  });

  it("stays silent when the whole result was saved", async () => {
    mocks.getChatTurns.mockResolvedValue([
      { id: "t1", parts: [{ kind: "tool", references: [{ handle: "h1", name: "Revenue", refType: "rows" }] }] },
    ]);
    renderRoute();
    await waitFor(() => expect(screen.getByText("Revenue")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /^Download Revenue$/i }));

    await waitFor(() => expect(mocks.exportBlob).toHaveBeenCalled());
    expect(screen.queryByText(/holds the first part of it/i)).not.toBeInTheDocument();
  });
});

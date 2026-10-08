// `/objects/:objectId` for an object this reader cannot have.
//
// The server's 404 (and its 403, drawn the same so a guessed id learns nothing)
// is an answer: the page settles at once on one line and the way back to Files,
// asking once. A failed read is not an answer, and offers the retry.

import { QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { sectionTitleFor } from "@/app/documentTitle";
import { ObjectRoute } from "@/pages/workspace/objects/ObjectRoute";
import { OBJECT_UNAVAILABLE } from "@/pages/workspace/objects/ObjectUnavailable";

const ID = "0f0e0d0c-0b0a-4000-8000-000000000011";

function answer(status: number): string[] {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      urls.push(input instanceof Request ? input.url : String(input));
      return new Response(JSON.stringify({ error: { code: "x", message: "Object not found" } }), {
        status,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  return urls;
}

/** Answers the object read with a live object of `type` at `webUrl`. */
function object(type: string, webUrl: string): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      new Response(
        JSON.stringify({
          id: ID,
          logical_id: ID,
          namespace: "workspace",
          type,
          title: "Pricing",
          version: 1,
          status: "ready",
          spec: {},
          owner_user_id: ID,
          visibility_scope: "private",
          created_at: "2026-10-06T00:00:00Z",
          updated_at: "2026-10-06T00:00:00Z",
          content_updated_at: 0,
          web_url: webUrl,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    ),
  );
}

function Where() {
  const location = useLocation();
  return <output data-testid="where">{location.pathname}</output>;
}

function renderAt(path: string) {
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter initialEntries={[path]}>
        <Where />
        <Routes>
          <Route path="/objects/:objectId" element={<ObjectRoute />} />
          <Route path="/workspaces/:workspaceId" element={<p>the workspace</p>} />
          <Route path="/chat/:chatId" element={<p>the chat</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const objectReads = (urls: string[]) => urls.filter((u) => new URL(u).pathname.endsWith(`/objects/${ID}`));

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("an object the reader cannot have", () => {
  it.each([404, 410])("reads a %i as not here, with the way back to Files, asking once", async (status) => {
    const urls = answer(status);
    renderAt(`/objects/${ID}`);

    expect(await screen.findByText(OBJECT_UNAVAILABLE.gone)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: OBJECT_UNAVAILABLE.goneAction })).toHaveAttribute("href", "/files");
    expect(screen.queryByText(OBJECT_UNAVAILABLE.failed)).not.toBeInTheDocument();
    expect(screen.queryByText(/support/i)).not.toBeInTheDocument();
    expect(objectReads(urls)).toHaveLength(1);
  });

  it("reads a 403 exactly like a 404, so a guessed id learns nothing", async () => {
    answer(403);
    renderAt(`/objects/${ID}`);
    expect(await screen.findByText(OBJECT_UNAVAILABLE.gone, {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByText(OBJECT_UNAVAILABLE.failed)).not.toBeInTheDocument();
  }, 20_000);

  it("offers a retry for a read that failed rather than answered", async () => {
    const urls = answer(500);
    const user = userEvent.setup();
    renderAt(`/objects/${ID}`);

    expect(await screen.findByText(OBJECT_UNAVAILABLE.failed, {}, { timeout: 15_000 })).toBeInTheDocument();
    expect(screen.queryByText(OBJECT_UNAVAILABLE.gone)).not.toBeInTheDocument();
    const before = objectReads(urls).length;
    await user.click(screen.getByRole("button", { name: OBJECT_UNAVAILABLE.retry }));
    await waitFor(() => expect(objectReads(urls).length).toBeGreaterThan(before));
  }, 20_000);

  it("is titled as a page of Files, not Overview", () => {
    expect(sectionTitleFor(`/objects/${ID}`)).toBe("Files");
  });
});

describe("a live object reached by an old object link", () => {
  it.each([
    ["workspace", `/workspaces/${ID}`, "the workspace"],
    ["chat", `/chat/${ID}`, "the chat"],
  ])("sends a %s to its own page instead of calling it retired", async (type, page, shown) => {
    object(type, page);
    renderAt(`/objects/${ID}`);

    expect(await screen.findByText(shown)).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent(page);
    expect(screen.queryByText(/retired/)).not.toBeInTheDocument();
  });

  it("still says a saved query was retired", async () => {
    object("query", `/objects/${ID}`);
    renderAt(`/objects/${ID}`);

    expect(await screen.findByText(/This kind of object was retired/)).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent(`/objects/${ID}`);
  });
});

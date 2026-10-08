// A link into a chat that names a file opens it in the chat's pane: the Files
// page sends a notebook to `/chat/<id>?open=<node>` because the notebook editor
// lives only there. The file opens in a tab once the pane's layout has loaded,
// and the parameter is dropped so a reload does not open it again.

import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createQueryClient } from "@/api/queryClient";
import { chatFileHref, useOpenFileFromLink } from "@/pages/workspace/chat/openFileLink";
import { forgetChatPane, useWorkspaceStore } from "@/pages/workspace/chat/workspace/workspaceStore";

const CHAT = "22222222-2222-2222-2222-222222222222";
const DRIVE = "dr_1";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function stubItem(answer: Response): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input);
      if (url.includes("/items/nd_nb")) return answer.clone();
      return json({});
    }),
  );
}

function Probe() {
  const { chatId } = useParams<{ chatId: string }>();
  useOpenFileFromLink(chatId, DRIVE);
  const location = useLocation();
  return <p data-testid="where">{`${location.pathname}${location.search}`}</p>;
}

function renderAt(path: string) {
  render(
    <QueryClientProvider client={createQueryClient({ retry: false })}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/chat/:chatId" element={<Probe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const fileTabs = () =>
  (useWorkspaceStore.getState().chats[CHAT]?.tabs ?? []).filter((tab) => tab.kind === "file").map((tab) => tab.node_id);

afterEach(() => {
  vi.unstubAllGlobals();
  forgetChatPane(CHAT);
});

describe("a chat link that names a file", () => {
  it("is spelled with the file's node", () => {
    expect(chatFileHref(CHAT, "nd_nb")).toBe(`/chat/${CHAT}?open=nd_nb`);
  });

  it("opens the file in a tab of the chat's pane and drops the parameter", async () => {
    stubItem(json({ id: "nd_nb", driveId: DRIVE, kind: "file", name: "demo3.alknb.py", nameDisplay: "demo3.alknb.py", parentId: "nd_dir", path: "/w/demo3.alknb.py" }));
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });

    renderAt(chatFileHref(CHAT, "nd_nb"));

    await waitFor(() => expect(fileTabs()).toEqual(["nd_nb"]));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(new RegExp(`^/chat/${CHAT}$`)));
  });

  it("waits for the pane's layout before opening the file", async () => {
    stubItem(json({ id: "nd_nb", driveId: DRIVE, kind: "file", name: "demo3.alknb.py", nameDisplay: "demo3.alknb.py", parentId: "nd_dir", path: "/w/demo3.alknb.py" }));

    renderAt(chatFileHref(CHAT, "nd_nb"));
    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(new RegExp(`^/chat/${CHAT}$`)));
    expect(fileTabs()).toEqual([]);

    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });
    expect(fileTabs()).toEqual(["nd_nb"]);
  });

  it("opens nothing for a node that cannot be read, and still drops the parameter", async () => {
    stubItem(json({ error: { code: "files.not_found", message: "Not found." } }, 404));
    useWorkspaceStore.getState().hydrate(CHAT, { tabs: [], active_tab_id: null });

    renderAt(chatFileHref(CHAT, "nd_nb"));

    await waitFor(() => expect(screen.getByTestId("where")).toHaveTextContent(new RegExp(`^/chat/${CHAT}$`)));
    expect(fileTabs()).toEqual([]);
  });
});
